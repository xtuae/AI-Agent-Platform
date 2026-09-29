"""Listings and appointments modules (04_modules_appointments_listings.md).

* listings — properties for sale or rent (listings module).
* appointment_types, appointment_resources, availability_rules, availability_exceptions,
  appointments — bookable time (appointments module).
* All tenant tables: RLS forced, composite foreign keys inside the tenant, like every other.
* Double bookings are impossible even under a race: `app_appointment_no_overlap()` runs BEFORE
  INSERT/UPDATE on appointments, locks the resource row (so concurrent bookings of one resource
  queue up behind each other) and raises exclusion_violation (23P01) if a live appointment of that
  resource overlaps. An EXCLUDE USING gist constraint would say the same thing, but it needs the
  btree_gist extension, which is not on every Postgres build; the trigger needs nothing. Under READ
  COMMITTED each statement in the function takes a fresh snapshot, so the second of two racing
  bookings sees the first once it gets the lock.
* appointments and listings publish change events like orders (app_notify_change, 0004).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TENANT_TABLES = (
    "listings",
    "appointment_types",
    "appointment_resources",
    "availability_rules",
    "availability_exceptions",
    "appointments",
)
TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
OVERLAP_FN = """
CREATE FUNCTION app_appointment_no_overlap() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status NOT IN ('requested','confirmed') THEN
    RETURN NEW;
  END IF;
  PERFORM 1 FROM appointment_resources
    WHERE tenant_id = NEW.tenant_id AND id = NEW.resource_id
    FOR UPDATE;
  IF EXISTS (
    SELECT 1 FROM appointments
    WHERE tenant_id = NEW.tenant_id
      AND resource_id = NEW.resource_id
      AND id <> NEW.id
      AND status IN ('requested','confirmed')
      AND starts_at < NEW.ends_at
      AND ends_at > NEW.starts_at
  ) THEN
    RAISE EXCEPTION 'appointment_overlap' USING ERRCODE = 'exclusion_violation';
  END IF;
  RETURN NEW;
END
$$;
"""


def _id() -> sa.Column[Any]:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _tenant() -> sa.Column[Any]:
    return sa.Column(
        "tenant_id",
        pg.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _created() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _fk(table: str, col: str, ref: str, ondelete: str | None = None) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col],
        [f"{ref}.tenant_id", f"{ref}.id"],
        name=f"fk_{table}_tenant_id_{ref}",
        ondelete=ondelete,
    )


def _uq_id(table: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint("tenant_id", "id", name=f"uq_{table}_tenant_id")


def upgrade() -> None:
    op.create_table(
        "listings",
        _id(),
        _tenant(),
        sa.Column("ref", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("property_type", sa.Text, nullable=False),
        sa.Column("area", sa.Text),
        sa.Column("community", sa.Text),
        sa.Column("address_note", sa.Text),
        sa.Column("bedrooms", sa.SmallInteger),
        sa.Column("bathrooms", sa.SmallInteger),
        sa.Column("size_sqft", sa.Integer),
        sa.Column("price_aed", sa.Numeric(14, 2)),
        sa.Column("rent_period", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'draft'")),
        sa.Column("viewings_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        _created(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "ref", name="uq_listings_tenant_id_ref"),
        _uq_id("listings"),
        sa.CheckConstraint("purpose in ('sale','rent')", name="ck_listings_purpose"),
        sa.CheckConstraint(
            "property_type in ('apartment','villa','townhouse','office','retail','land','other')",
            name="ck_listings_property_type",
        ),
        sa.CheckConstraint(
            "status in ('draft','available','under_offer','let','sold','archived')",
            name="ck_listings_status",
        ),
        sa.CheckConstraint(
            "rent_period is null or rent_period in ('year','month')",
            name="ck_listings_rent_period",
        ),
        sa.CheckConstraint(
            "status <> 'available' or price_aed is not null", name="ck_listings_available_priced"
        ),
        sa.CheckConstraint(
            "coalesce(bedrooms, 0) >= 0 and coalesce(bathrooms, 0) >= 0 "
            "and coalesce(size_sqft, 0) >= 0 and coalesce(price_aed, 0) >= 0",
            name="ck_listings_non_negative",
        ),
    )
    op.create_index("ix_listings_tenant_status", "listings", ["tenant_id", "status"])

    op.create_table(
        "appointment_types",
        _id(),
        _tenant(),
        sa.Column("name_en", sa.Text, nullable=False),
        sa.Column("name_ar", sa.Text),
        sa.Column("duration_min", sa.SmallInteger, nullable=False),
        sa.Column("location_kind", sa.Text, nullable=False),
        sa.Column("needs_address", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        _created(),
        _uq_id("appointment_types"),
        sa.CheckConstraint("duration_min between 5 and 480", name="ck_appointment_types_duration"),
        sa.CheckConstraint(
            "location_kind in ('office','onsite','video','phone')",
            name="ck_appointment_types_location_kind",
        ),
    )

    op.create_table(
        "appointment_resources",
        _id(),
        _tenant(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("tenant_user_id", pg.UUID(as_uuid=True)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        _created(),
        _uq_id("appointment_resources"),
        _fk("appointment_resources", "tenant_user_id", "tenant_users"),
    )

    op.create_table(
        "availability_rules",
        _id(),
        _tenant(),
        sa.Column("resource_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("weekday", sa.SmallInteger, nullable=False),  # 0 = Monday … 6 = Sunday
        sa.Column("start_time", sa.Time, nullable=False),
        sa.Column("end_time", sa.Time, nullable=False),
        _fk("availability_rules", "resource_id", "appointment_resources", "CASCADE"),
        sa.CheckConstraint("weekday between 0 and 6", name="ck_availability_rules_weekday"),
        sa.CheckConstraint("start_time < end_time", name="ck_availability_rules_order"),
    )
    op.create_index(
        "ix_availability_rules_resource", "availability_rules", ["tenant_id", "resource_id"]
    )

    op.create_table(
        "availability_exceptions",
        _id(),
        _tenant(),
        sa.Column("resource_id", pg.UUID(as_uuid=True)),  # NULL = every resource
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("start_time", sa.Time),  # both NULL = the whole day
        sa.Column("end_time", sa.Time),
        sa.Column("reason", sa.Text),
        _fk("availability_exceptions", "resource_id", "appointment_resources", "CASCADE"),
        sa.CheckConstraint(
            "(start_time is null and end_time is null) or "
            "(start_time is not null and end_time is not null and start_time < end_time)",
            name="ck_availability_exceptions_span",
        ),
    )
    op.create_index(
        "ix_availability_exceptions_day", "availability_exceptions", ["tenant_id", "day"]
    )

    op.create_table(
        "appointments",
        _id(),
        _tenant(),
        sa.Column("ref", sa.Text, nullable=False),
        sa.Column("customer_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("type_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_module", sa.Text),
        sa.Column("subject_id", pg.UUID(as_uuid=True)),
        sa.Column("subject_label", sa.Text),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("location_note", sa.Text),
        sa.Column("notes", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("conversation_id", pg.UUID(as_uuid=True)),
        sa.Column("created_by", sa.Text),
        _created(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "ref", name="uq_appointments_tenant_id_ref"),
        _fk("appointments", "customer_id", "customers"),
        _fk("appointments", "type_id", "appointment_types"),
        _fk("appointments", "resource_id", "appointment_resources"),
        _fk("appointments", "conversation_id", "conversations"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_appointments_order"),
        sa.CheckConstraint(
            "status in ('requested','confirmed','cancelled','completed','no_show')",
            name="ck_appointments_status",
        ),
        sa.CheckConstraint("source in ('agent','dashboard')", name="ck_appointments_source"),
        sa.CheckConstraint(
            "(subject_module is null) = (subject_id is null)", name="ck_appointments_subject"
        ),
    )
    op.create_index(
        "ix_appointments_resource_time", "appointments", ["tenant_id", "resource_id", "starts_at"]
    )
    op.create_index("ix_appointments_time", "appointments", ["tenant_id", "starts_at"])
    op.create_index("ix_appointments_customer", "appointments", ["tenant_id", "customer_id"])

    for table in NEW_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
        )

    op.execute(OVERLAP_FN)
    op.execute(
        "CREATE TRIGGER trg_appointments_no_overlap BEFORE INSERT OR UPDATE ON appointments "
        "FOR EACH ROW EXECUTE FUNCTION app_appointment_no_overlap()"
    )
    op.execute(
        "CREATE TRIGGER trg_appointments_notify AFTER INSERT OR UPDATE ON appointments "
        "FOR EACH ROW EXECUTE FUNCTION app_notify_change('customer_id')"
    )
    op.execute(
        "CREATE TRIGGER trg_listings_notify AFTER INSERT OR UPDATE ON listings "
        "FOR EACH ROW EXECUTE FUNCTION app_notify_change()"
    )


def downgrade() -> None:
    for table in reversed(NEW_TENANT_TABLES):
        op.drop_table(table)  # drops its triggers too
    op.execute("DROP FUNCTION IF EXISTS app_appointment_no_overlap()")

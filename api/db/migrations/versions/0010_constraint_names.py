"""Check-constraint names as the models declare them.

Migrations 0001-0008 passed full names (e.g. `ck_orders_status`) to CheckConstraint without
`op.f()`, so the naming convention (`ck_%(table_name)s_%(constraint_name)s`) prefixed them a
second time: the database holds `ck_orders_ck_orders_status` while the model says
`ck_orders_status`. Harmless at runtime, but it makes any later migration that names one of them
fail (0009 hit exactly this). Rename every `ck_<table>_ck_<table>_<rest>` to `ck_<table>_<rest>`.

Metadata only: ALTER TABLE … RENAME CONSTRAINT takes a brief lock and rewrites nothing.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `ck_<t>_ck_<t>_<rest>` → `ck_<t>_<rest>` (upgrade), and back (downgrade), for every check
# constraint in public whose table name t matches both halves.
RENAME = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.conrelid::regclass AS tbl, c.conname, t.relname
    FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
    WHERE c.contype = 'c' AND c.connamespace = 'public'::regnamespace
      AND c.conname LIKE 'ck\\_' || t.relname || '\\_ck\\_' || t.relname || '\\_%'
  LOOP
    EXECUTE format(
      'ALTER TABLE %s RENAME CONSTRAINT %I TO %I', r.tbl, r.conname,
      'ck_' || r.relname
        || substr(r.conname, length('ck_' || r.relname || '_ck_' || r.relname) + 1)
    );
  END LOOP;
END $$;
"""

UNRENAME = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.conrelid::regclass AS tbl, c.conname, t.relname
    FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
    WHERE c.contype = 'c' AND c.connamespace = 'public'::regnamespace
      AND c.conname = ANY(:names)
  LOOP
    EXECUTE format('ALTER TABLE %s RENAME CONSTRAINT %I TO %I', r.tbl, r.conname,
                   'ck_' || r.relname || '_' || r.conname);
  END LOOP;
END $$;
"""

# What 0001-0008 created with doubled names (as the database held them before this revision),
# in their single-prefixed form. Constraints added later (0007's op.f() ones, 0009) are not here,
# so a downgrade puts back exactly what was there.
RENAMED = (
    "ck_appointments_order",
    "ck_appointments_source",
    "ck_appointments_status",
    "ck_appointments_subject",
    "ck_appointment_types_duration",
    "ck_appointment_types_location_kind",
    "ck_availability_exceptions_span",
    "ck_availability_rules_order",
    "ck_availability_rules_weekday",
    "ck_campaigns_approval_required",
    "ck_campaigns_status",
    "ck_coupon_books_bottles_remaining_non_negative",
    "ck_coupon_packages_active_needs_price",
    "ck_customers_opt_in_status",
    "ck_listings_available_priced",
    "ck_listings_non_negative",
    "ck_listings_property_type",
    "ck_listings_purpose",
    "ck_listings_rent_period",
    "ck_listings_status",
    "ck_messages_direction",
    "ck_meta_rates_category",
    "ck_meta_rates_non_negative",
    "ck_meta_statement_months_first_day",
    "ck_optin_links_code",
    "ck_optin_links_language",
    "ck_optin_links_prefill",
    "ck_optin_links_source",
    "ck_optin_links_wording",
    "ck_optin_visits_claim",
    "ck_orders_status",
    "ck_platform_users_role",
    "ck_reimbursements_amount",
    "ck_reimbursements_basis",
    "ck_reimbursements_month",
    "ck_tenant_modules_key",
    "ck_tenants_status",
    "ck_tenant_users_role",
)


def upgrade() -> None:
    op.execute(RENAME)


def downgrade() -> None:
    names = "ARRAY[" + ",".join(f"'{n}'" for n in RENAMED) + "]::name[]"
    op.execute(UNRENAME.replace(":names", names))

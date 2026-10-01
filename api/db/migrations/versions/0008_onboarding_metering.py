"""Phase 5: data onboarding, opt-in capture, cost reconciliation, the platform console.

* usage_daily gets spend per pricing category (the Costs screen shows spend by day AND category),
  backfilled from the cost stamped on each outbound message.
* optin_links (platform): a tenant's QR / short-URL consent pages. Platform table because the
  public page resolves a code to a tenant before any tenant context exists (like tenant_channels).
* optin_visits (tenant, RLS): one row per "Continue on WhatsApp" tap, holding the exact wording
  shown and the one-time code carried by the prefilled message — the TDRA/PDPL evidence trail.
* meta_statement_lines (tenant, RLS): Meta's own per-day figures (WABA pricing analytics), kept
  next to usage_daily so a month can be reconciled line by line; meta_statement_months records
  which months were pulled (a day with no traffic has no line) and whether the pull was final.
* reimbursements (platform): what HMH Labz has paid back for a borne-by-HMH service month.
* tenants.monthly_fee_aed (margin per tenant); platform_users gets name / is_active / timestamps.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
NEW_TENANT_TABLES = ("optin_visits", "meta_statement_lines", "meta_statement_months")
CATEGORIES = ("marketing", "utility", "service", "authentication")

# RLS is FORCED, so even the owner sees nothing without a tenant context: backfill per tenant.
BACKFILL = """
DO $$
DECLARE t uuid;
BEGIN
  FOR t IN SELECT id FROM tenants LOOP
    PERFORM set_config('app.tenant_id', t::text, true);
    UPDATE usage_daily u SET
      marketing_cost_aed = s.marketing, utility_cost_aed = s.utility,
      service_cost_aed = s.service, authentication_cost_aed = s.authentication
    FROM (
      SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
        coalesce(sum(cost_aed) FILTER (WHERE pricing_category = 'marketing'), 0) AS marketing,
        coalesce(sum(cost_aed) FILTER (WHERE pricing_category = 'utility'), 0) AS utility,
        coalesce(sum(cost_aed) FILTER (WHERE pricing_category = 'service'), 0) AS service,
        coalesce(sum(cost_aed) FILTER (WHERE pricing_category = 'authentication'), 0)
          AS authentication
      FROM messages WHERE direction = 'out' AND cost_aed IS NOT NULL
      GROUP BY 1
    ) s
    WHERE u.tenant_id = t AND u.day = s.day;
  END LOOP;
  PERFORM set_config('app.tenant_id', '', true);
END $$;
"""


def _ts(name: str, *, default: bool = False) -> sa.Column:  # type: ignore[type-arg]
    if default:
        return sa.Column(
            name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )
    return sa.Column(name, sa.DateTime(timezone=True))


def upgrade() -> None:
    # ---- metering: spend per category
    for c in CATEGORIES:
        op.add_column(
            "usage_daily",
            sa.Column(
                f"{c}_cost_aed", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")
            ),
        )
    op.execute(BACKFILL)

    # ---- platform: fee for margin, staff accounts
    op.add_column("tenants", sa.Column("monthly_fee_aed", sa.Numeric(10, 2)))
    op.add_column("platform_users", sa.Column("name", sa.Text))
    op.add_column(
        "platform_users",
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )
    op.add_column("platform_users", _ts("created_at", default=True))
    op.add_column("platform_users", _ts("last_login_at"))

    # ---- opt-in links (platform) and visits (tenant)
    op.create_table(
        "optin_links",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("code", sa.Text, nullable=False, unique=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False, server_default=sa.text("'en'")),
        sa.Column("heading", sa.Text, nullable=False),
        sa.Column("wording", sa.Text, nullable=False),
        sa.Column("prefill", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        _ts("created_at", default=True),
        _ts("updated_at", default=True),
        sa.Column("created_by", sa.Text),
        sa.UniqueConstraint("tenant_id", "id", name="uq_optin_links_tenant_id"),
        sa.CheckConstraint("code ~ '^[A-Za-z0-9]{6,16}$'", name="ck_optin_links_code"),
        sa.CheckConstraint("source ~ '^[a-z][a-z0-9_]{1,31}$'", name="ck_optin_links_source"),
        sa.CheckConstraint("language in ('en','ar')", name="ck_optin_links_language"),
        sa.CheckConstraint(
            "char_length(wording) BETWEEN 20 AND 1000", name="ck_optin_links_wording"
        ),
        sa.CheckConstraint("char_length(prefill) BETWEEN 2 AND 200", name="ck_optin_links_prefill"),
    )
    op.create_table(
        "optin_visits",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("link_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("heading", sa.Text, nullable=False),
        sa.Column("wording", sa.Text, nullable=False),
        _ts("created_at", default=True),
        _ts("claimed_at"),
        sa.Column("customer_id", pg.UUID(as_uuid=True)),
        sa.Column("wamid", sa.Text),
        sa.ForeignKeyConstraint(
            ["tenant_id", "link_id"],
            ["optin_links.tenant_id", "optin_links.id"],
            name="fk_optin_visits_link",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_optin_visits_customer",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "token", name="uq_optin_visits_tenant_id"),
        sa.CheckConstraint(
            "(claimed_at IS NULL) = (customer_id IS NULL)", name="ck_optin_visits_claim"
        ),
    )
    op.create_index("ix_optin_visits_link", "optin_visits", ["tenant_id", "link_id"])

    # ---- Meta's own figures
    op.create_table(
        "meta_statement_lines",
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            primary_key=True,
            index=True,
        ),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("category", sa.Text, primary_key=True),
        sa.Column("market", sa.Text, primary_key=True),
        sa.Column("pricing_type", sa.Text, primary_key=True),
        sa.Column("volume", sa.Integer, nullable=False),
        sa.Column("cost", sa.Numeric(12, 5), nullable=False),
        sa.Column("currency", sa.Text, nullable=False),
        _ts("pulled_at", default=True),
    )

    op.create_table(
        "meta_statement_months",
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            primary_key=True,
            index=True,
        ),
        sa.Column("month", sa.Date, primary_key=True),
        sa.Column("currency", sa.Text),
        sa.Column("complete", sa.Boolean, nullable=False),
        _ts("pulled_at", default=True),
        sa.CheckConstraint(
            "extract(day FROM month) = 1", name="ck_meta_statement_months_first_day"
        ),
    )

    # ---- reimbursements (platform)
    op.create_table(
        "reimbursements",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("service_month", sa.Integer, nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("amount_aed", sa.Numeric(10, 2), nullable=False),
        sa.Column("basis", sa.Text, nullable=False),
        sa.Column("reference", sa.Text),
        _ts("paid_at", default=True),
        sa.Column("recorded_by", sa.Text, nullable=False),
        sa.UniqueConstraint("tenant_id", "service_month", name="uq_reimbursements_tenant_id"),
        sa.CheckConstraint("service_month >= 1", name="ck_reimbursements_month"),
        sa.CheckConstraint("amount_aed >= 0", name="ck_reimbursements_amount"),
        sa.CheckConstraint("basis in ('meta_statement','metered')", name="ck_reimbursements_basis"),
    )

    for table in NEW_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
        )


def downgrade() -> None:
    op.drop_table("reimbursements")
    op.drop_table("meta_statement_months")
    op.drop_table("meta_statement_lines")
    op.drop_table("optin_visits")
    op.drop_table("optin_links")
    for col in ("last_login_at", "created_at", "is_active", "name"):
        op.drop_column("platform_users", col)
    op.drop_column("tenants", "monthly_fee_aed")
    for c in CATEGORIES:
        op.drop_column("usage_daily", f"{c}_cost_aed")

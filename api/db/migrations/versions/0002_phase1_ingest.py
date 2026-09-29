"""Phase 1: fills the gaps found in the spec during Phase 0.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

* coupon_packages (tenant, RLS) — the packages get_coupon_packages() reads and seed_tenant creates.
  coupon_books are books a customer bought; there was no table for what is on sale.
* webhook_events (tenant, RLS) — the raw payload persisted by the webhook (Phase 1 step 5). One
  row per `change` under the resolved tenant, so raw customer text is RLS-protected like
  everything else. Payloads for unknown phone_number_ids are never stored.
* meta_rates (platform, no RLS) — dated Meta price table, so a price change is an INSERT, not a
  deploy. Seeded with the rates in 03_build_prompt Phase 1.
* messages.prompt_version — 02_agent_prompts §5 requires it; missing from §5.2.
* customers.address_note — update_customer tool takes it; missing from §5.2.
* usage_daily.authentication_count — Meta's fourth pricing category had no counter.
* conversations: partial unique index on (tenant_id, customer_id, channel_id) WHERE state <>
  'closed', so two webhooks racing for a new customer cannot open two conversations — the ingest
  uses it as an ON CONFLICT target.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TENANT_TABLES = ("coupon_packages", "webhook_events")
TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "coupon_packages",
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
        ),
        sa.Column("sku", sa.Text, nullable=False),
        sa.Column("name_en", sa.Text),
        sa.Column("name_ar", sa.Text),
        sa.Column("price_aed", sa.Numeric(10, 2)),
        sa.Column("bottles_paid", sa.Integer),
        sa.Column("bottles_free", sa.Integer, nullable=False, server_default=sa.text("0")),
        # NULL = available in every emirate the tenant serves.
        sa.Column("emirate", sa.Text),
        sa.Column("validity_days", sa.Integer),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("tenant_id", "sku", name="uq_coupon_packages_tenant_id_sku"),
        # An active package must have a price — the agent may only quote what the DB holds.
        sa.CheckConstraint(
            "not is_active or (price_aed is not null and bottles_paid is not null)",
            name="ck_coupon_packages_active_needs_price",
        ),
    )

    op.create_table(
        "webhook_events",
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
        ),
        sa.Column(
            "field", sa.Text, nullable=False
        ),  # messages | message_template_status_update | …
        sa.Column("kind", sa.Text, nullable=False),  # messages | statuses | template_status | other
        sa.Column("phone_number_id", sa.Text),
        sa.Column("waba_id", sa.Text),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_webhook_events_tenant_id", "webhook_events", ["tenant_id"])
    op.create_index(
        "ix_webhook_events_unprocessed",
        "webhook_events",
        ["tenant_id", "received_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )

    op.create_table(
        "meta_rates",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("market", sa.Text, nullable=False),  # ISO country of the RECIPIENT, e.g. AE
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("rate_aed", sa.Numeric(10, 5), nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),  # exclusive; NULL = open-ended
        sa.Column("source", sa.Text),
        sa.CheckConstraint(
            "category in ('marketing','utility','service','authentication')",
            name="ck_meta_rates_category",
        ),
        sa.CheckConstraint("rate_aed >= 0", name="ck_meta_rates_non_negative"),
        sa.UniqueConstraint(
            "market", "category", "effective_from", name="uq_meta_rates_market_category_from"
        ),
    )
    rates = sa.table(
        "meta_rates",
        sa.column("market", sa.Text),
        sa.column("category", sa.Text),
        sa.column("rate_aed", sa.Numeric),
        sa.column("effective_from", sa.Date),
        sa.column("effective_to", sa.Date),
        sa.column("source", sa.Text),
    )
    src = "03_build_prompt.md Phase 1 — verify against Meta's published rate card"
    op.execute(
        rates.insert().values(
            [
                # '-infinity': the spec gives the rate without a start date.
                {
                    "market": "AE",
                    "category": "marketing",
                    "rate_aed": "0.18300",
                    "effective_from": sa.text("'-infinity'::date"),
                    "effective_to": None,
                    "source": src,
                },
                {
                    "market": "AE",
                    "category": "utility",
                    "rate_aed": "0.05800",
                    "effective_from": sa.text("'-infinity'::date"),
                    "effective_to": None,
                    "source": src,
                },
                {
                    "market": "AE",
                    "category": "service",
                    "rate_aed": "0",
                    "effective_from": sa.text("'-infinity'::date"),
                    "effective_to": sa.text("'2026-10-01'::date"),
                    "source": src,
                },
                {
                    "market": "AE",
                    "category": "service",
                    "rate_aed": "0.05800",
                    "effective_from": sa.text("'2026-10-01'::date"),
                    "effective_to": None,
                    "source": src,
                },
            ]
        )
    )

    op.add_column("messages", sa.Column("prompt_version", sa.Text))
    op.add_column("customers", sa.Column("address_note", sa.Text))
    op.add_column(
        "usage_daily",
        sa.Column("authentication_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "uq_conversations_open_per_customer_channel",
        "conversations",
        ["tenant_id", "customer_id", "channel_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'closed'"),
    )

    for table in NEW_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
        )


def downgrade() -> None:
    op.drop_index("uq_conversations_open_per_customer_channel", table_name="conversations")
    op.drop_column("usage_daily", "authentication_count")
    op.drop_column("customers", "address_note")
    op.drop_column("messages", "prompt_version")
    op.drop_table("meta_rates")
    op.drop_table("webhook_events")
    op.drop_table("coupon_packages")

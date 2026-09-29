"""Initial schema: platform tables, tenant-scoped tables, RLS on every tenant table.

Revision ID: 0001
Revises:
Create Date: 2026-09-28

Deviations from 01_architecture.md §4.3/§5, each deliberate:

* RLS policy reads `NULLIF(current_setting('app.tenant_id', true), '')::uuid` instead of
  `current_setting('app.tenant_id')::uuid`. The spec's form RAISES when the setting has never
  been defined on a connection, and raises again ('' is not a uuid) on a pooled connection
  after a SET LOCAL transaction has ended. The spec's own requirement is "missing context →
  zero rows"; this form delivers exactly that.
* Policies carry an explicit WITH CHECK, so a write cannot create a row for another tenant.
* tenant_id is NOT NULL + FK tenants(id) on every tenant table (constraint 1).
* Cross-table references inside a tenant use COMPOSITE foreign keys (tenant_id, x_id). FK checks
  bypass RLS, so a plain FK would let tenant A's order point at tenant B's customer.
* campaigns: CHECK that status cannot leave draft/cancelled without approved_by + approved_at
  (constraint 8, enforced by the database, not only the API).
* knowledge_chunks: HNSW index, not ivfflat. ivfflat computes its list centroids at CREATE INDEX
  time, and at migration time the table is empty — the index would be built on no data and give
  poor recall until manually rebuilt. HNSW has no training step and suits this table size.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen list — a new tenant table in a later migration must enable RLS in that migration.
TENANT_TABLES = (
    "customers",
    "coupon_books",
    "products",
    "orders",
    "conversations",
    "messages",
    "message_templates",
    "campaigns",
    "campaign_recipients",
    "knowledge_chunks",
    "usage_daily",
    "audit_log",
)

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _tenant_fk() -> sa.Column[Any]:
    return sa.Column(
        "tenant_id",
        pg.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _ts(name: str, **kw: Any) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _composite_fk(table: str, col: str, ref: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", col],
        [f"{ref}.tenant_id", f"{ref}.id"],
        name=f"fk_{table}_tenant_{col}_{ref}",
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ------------------------------------------------------------ platform tables (no RLS)
    op.create_table(
        "tenants",
        _uuid_pk(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("legal_name", sa.Text),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'trial'")),
        sa.Column("timezone", sa.Text, nullable=False, server_default=sa.text("'Asia/Dubai'")),
        sa.Column("locale_default", sa.Text, nullable=False, server_default=sa.text("'en'")),
        sa.Column("contract_ref", sa.Text),
        sa.Column("service_start", sa.Date),
        sa.Column("free_months_until", sa.Date),
        sa.Column("meta_charges_borne_by_us_until", sa.Date),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status in ('trial','active','suspended','churned')", name="ck_tenants_status"
        ),
    )

    op.create_table(
        "tenant_channels",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("waba_id", sa.Text),
        sa.Column("phone_number_id", sa.Text, nullable=False, unique=True),
        sa.Column("display_phone", sa.Text),
        sa.Column("access_token_encrypted", sa.LargeBinary),
        _ts("token_expires_at"),
        sa.Column("webhook_verify_token", sa.Text),
        sa.Column("quality_rating", sa.Text),
        sa.Column("messaging_limit_tier", sa.Text),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_channels_tenant_id_id"),
    )
    op.create_index("ix_tenant_channels_tenant_id", "tenant_channels", ["tenant_id"])

    op.create_table(
        "tenant_settings",
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("llm_provider", sa.Text, nullable=False, server_default=sa.text("'gemini'")),
        sa.Column(
            "llm_model_chat", sa.Text, nullable=False, server_default=sa.text("'gemini-3-flash'")
        ),
        sa.Column(
            "llm_model_classify",
            sa.Text,
            nullable=False,
            server_default=sa.text("'gemini-2.5-flash-lite'"),
        ),
        sa.Column("monthly_message_cap_aed", sa.Numeric(10, 2)),
        sa.Column("monthly_token_cap", sa.Integer),
        sa.Column("business_hours", pg.JSONB),
        sa.Column("escalation_phone", sa.Text),
        sa.Column("agent_persona", pg.JSONB),
        sa.Column("feature_flags", pg.JSONB),
    )

    op.create_table(
        "platform_users",
        _uuid_pk(),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("totp_secret", sa.Text),
        sa.CheckConstraint("role in ('owner','ops','support')", name="ck_platform_users_role"),
    )

    op.create_table(
        "tenant_users",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.CheckConstraint("role in ('admin','agent','viewer')", name="ck_tenant_users_role"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_tenant_users_tenant_id_email"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_users_tenant_id_id"),
    )
    op.create_index("ix_tenant_users_tenant_id", "tenant_users", ["tenant_id"])

    # ------------------------------------------------------------ tenant-scoped tables (RLS)
    op.create_table(
        "customers",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("wa_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text),
        sa.Column("area", sa.Text),
        sa.Column("emirate", sa.Text),
        sa.Column("language", sa.Text),
        sa.Column("source", sa.Text),
        sa.Column("opt_in_status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        _ts("opt_in_at"),
        sa.Column("opt_in_evidence", pg.JSONB),
        _ts("opt_out_at"),
        sa.Column("lifetime_orders", sa.Integer, nullable=False, server_default=sa.text("0")),
        _ts("last_order_at"),
        sa.Column("external_ref", sa.Text),
        sa.CheckConstraint(
            "opt_in_status in ('pending','opted_in','opted_out')",
            name="ck_customers_opt_in_status",
        ),
        sa.UniqueConstraint("tenant_id", "wa_id", name="uq_customers_tenant_id_wa_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),
    )

    op.create_table(
        "coupon_books",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("customer_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.Text),
        sa.Column("price_aed", sa.Numeric),
        sa.Column("bottles_total", sa.Integer),
        sa.Column("bottles_free", sa.Integer),
        sa.Column("bottles_remaining", sa.Integer),
        _ts("purchased_at"),
        sa.Column("expires_at", sa.Date),
        _composite_fk("coupon_books", "customer_id", "customers"),
        sa.CheckConstraint(
            "bottles_remaining >= 0", name="ck_coupon_books_bottles_remaining_non_negative"
        ),
    )
    op.create_index("ix_coupon_books_customer_id", "coupon_books", ["customer_id"])

    op.create_table(
        "products",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("sku", sa.Text, nullable=False),
        sa.Column("name_en", sa.Text),
        sa.Column("name_ar", sa.Text),
        sa.Column("category", sa.Text),
        sa.Column("brand", sa.Text),
        sa.Column("price_aed", sa.Numeric),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("cross_sell_priority", sa.Integer),
        sa.Column("stock_note", sa.Text),
        sa.UniqueConstraint("tenant_id", "sku", name="uq_products_tenant_id_sku"),
    )

    op.create_table(
        "orders",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("customer_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("order_no", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'draft'")),
        sa.Column("items", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("total_aed", sa.Numeric),
        sa.Column("source", sa.Text),
        sa.Column("area", sa.Text),
        sa.Column("delivery_slot", sa.Text),
        sa.Column("delivery_date", sa.Date),
        sa.Column("notes", sa.Text),
        sa.Column("created_by", sa.Text),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status in ('draft','confirmed','out_for_delivery','delivered','cancelled')",
            name="ck_orders_status",
        ),
        _composite_fk("orders", "customer_id", "customers"),
        sa.UniqueConstraint("tenant_id", "order_no", name="uq_orders_tenant_id_order_no"),
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
    op.create_index("ix_orders_tenant_created", "orders", ["tenant_id", "created_at"])

    op.create_table(
        "conversations",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("customer_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("assigned_to", pg.UUID(as_uuid=True)),
        _ts("last_inbound_at"),
        _ts("last_outbound_at"),
        _ts("service_window_expires_at"),
        sa.Column("summary", sa.Text),
        sa.Column("language", sa.Text),
        _composite_fk("conversations", "customer_id", "customers"),
        _composite_fk("conversations", "channel_id", "tenant_channels"),
        _composite_fk("conversations", "assigned_to", "tenant_users"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
    )
    op.create_index(
        "ix_conversations_tenant_customer", "conversations", ["tenant_id", "customer_id"]
    )

    op.create_table(
        "messages",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("conversation_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("wamid", sa.Text, unique=True),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("msg_type", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("media_url", sa.Text),
        sa.Column("transcript", sa.Text),
        sa.Column("template_name", sa.Text),
        sa.Column("pricing_category", sa.Text),
        sa.Column("cost_aed", sa.Numeric(8, 5)),
        sa.Column("status", sa.Text),
        sa.Column("error_code", sa.Text),
        sa.Column("error_detail", sa.Text),
        sa.Column("llm_model", sa.Text),
        sa.Column("prompt_tokens", sa.Integer),
        sa.Column("completion_tokens", sa.Integer),
        sa.Column("latency_ms", sa.Integer),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("direction in ('in','out')", name="ck_messages_direction"),
        _composite_fk("messages", "conversation_id", "conversations"),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["tenant_id", "conversation_id", "created_at"],
    )

    op.create_table(
        "message_templates",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("category", sa.Text),
        sa.Column("meta_status", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("variables", pg.JSONB),
        sa.Column("meta_template_id", sa.Text),
        sa.UniqueConstraint(
            "tenant_id", "name", "language", name="uq_message_templates_tenant_id_name_language"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_message_templates_tenant_id_id"),
    )

    op.create_table(
        "campaigns",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("template_id", pg.UUID(as_uuid=True)),
        sa.Column("segment_query", pg.JSONB),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'draft'")),
        sa.Column("approved_by", pg.UUID(as_uuid=True)),
        _ts("approved_at"),
        _ts("scheduled_for"),
        sa.Column("throttle_per_minute", sa.Integer, nullable=False, server_default=sa.text("60")),
        sa.Column("budget_cap_aed", sa.Numeric),
        sa.Column("sent_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("delivered_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("read_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("reply_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("order_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("spend_aed", sa.Numeric, nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "status in ('draft','approved','sending','paused','done','cancelled')",
            name="ck_campaigns_status",
        ),
        sa.CheckConstraint(
            "status in ('draft','cancelled') "
            "or (approved_by is not null and approved_at is not null)",
            name="ck_campaigns_approval_required",
        ),
        _composite_fk("campaigns", "template_id", "message_templates"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaigns_tenant_id_id"),
    )

    op.create_table(
        "campaign_recipients",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("campaign_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text),
        sa.Column("wamid", sa.Text),
        _ts("sent_at"),
        sa.Column("cost_aed", sa.Numeric),
        _composite_fk("campaign_recipients", "campaign_id", "campaigns"),
        _composite_fk("campaign_recipients", "customer_id", "customers"),
        sa.UniqueConstraint(
            "campaign_id", "customer_id", name="uq_campaign_recipients_campaign_id_customer_id"
        ),
    )

    op.create_table(
        "knowledge_chunks",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("source", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(384)),
        sa.Column("language", sa.Text),
    )
    op.create_index(
        "ix_knowledge_chunks_embedding_hnsw",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "usage_daily",
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("msgs_in", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("msgs_out", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("marketing_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("utility_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("service_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("meta_cost_aed", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("llm_prompt_tokens", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "llm_completion_tokens", sa.BigInteger, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("llm_cost_usd", sa.Numeric(10, 5), nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        _tenant_fk(),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("entity", sa.Text),
        sa.Column("entity_id", pg.UUID(as_uuid=True)),
        sa.Column("before", pg.JSONB),
        sa.Column("after", pg.JSONB),
        _ts("at", nullable=False, server_default=sa.func.now()),
    )

    # tenant_id indexes (leading column for every RLS-filtered scan)
    for table in TENANT_TABLES:
        if table != "usage_daily":  # PK already leads with tenant_id
            op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    # ------------------------------------------------------------ row-level security
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.drop_table(table)
    for table in (
        "tenant_users",
        "platform_users",
        "tenant_settings",
        "tenant_channels",
        "tenants",
    ):
        op.drop_table(table)
    # The vector extension is left installed: it was created by the superuser init script and
    # the owner role does not own it.

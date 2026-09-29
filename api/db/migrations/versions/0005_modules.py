"""Plug-and-play modules: tenant_modules + tenant_settings.contact_label.

* tenant_modules — platform table (no RLS, like tenant_settings): which capability modules
  (api/modules) each tenant has, and each module's per-tenant config. Every deployment has every
  module's tables; switching a module on for a tenant is a row here, not a deploy.
* Existing tenants get the water preset (catalog, orders, coupons) so nothing they use disappears.
  (No tenant is in production yet; this keeps local and staging data working.)
* tenant_settings.contact_label — what the dashboard calls contacts (Customers / Clients / Leads).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WATER_PRESET = ("catalog", "orders", "coupons")


def upgrade() -> None:
    op.create_table(
        "tenant_modules",
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("module_key", sa.Text, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("config", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "enabled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("module_key ~ '^[a-z][a-z0-9_]*$'", name="ck_tenant_modules_key"),
    )
    op.add_column("tenant_settings", sa.Column("contact_label", sa.Text))
    for key in WATER_PRESET:
        op.execute(
            sa.text(
                "INSERT INTO tenant_modules (tenant_id, module_key) "
                "SELECT id, :k FROM tenants ON CONFLICT DO NOTHING"
            ).bindparams(k=key)
        )


def downgrade() -> None:
    op.drop_column("tenant_settings", "contact_label")
    op.drop_table("tenant_modules")

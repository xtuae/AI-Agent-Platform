"""orders.conversation_id + orders.idempotency_key (Phase 2).

* conversation_id — which chat produced the order (composite FK, same tenant). Needed for the
  create_order idempotency guard, and for the dashboard / campaign attribution later.
* idempotency_key — sha256 of the (sku, qty) set; create_order refuses to place the same items
  twice in one conversation within 10 minutes (02 §3.1).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("conversation_id", pg.UUID(as_uuid=True)))
    op.add_column("orders", sa.Column("idempotency_key", sa.Text))
    op.create_foreign_key(
        "fk_orders_tenant_conversation_id_conversations",
        "orders",
        "conversations",
        ["tenant_id", "conversation_id"],
        ["tenant_id", "id"],
    )
    op.create_index(
        "ix_orders_idempotency",
        "orders",
        ["tenant_id", "conversation_id", "idempotency_key", "created_at"],
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_orders_idempotency", table_name="orders")
    op.drop_constraint(
        "fk_orders_tenant_conversation_id_conversations", "orders", type_="foreignkey"
    )
    op.drop_column("orders", "idempotency_key")
    op.drop_column("orders", "conversation_id")

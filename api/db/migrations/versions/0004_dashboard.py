"""Dashboard (Phase 3): team logins, refresh tokens, who-sent-it, and change notifications.

* tenant_users — name, is_active (logins are deactivated, never deleted, so audit and
  messages.sent_by keep pointing at a real row), created_at, last_login_at, and an index on
  lower(email) for the login lookup.
* auth_refresh_tokens — platform table (no RLS): a refresh arrives with only a cookie, before any
  tenant is known. Stores sha256 of the token, never the token. Rotated on every use; reuse of a
  rotated token revokes its whole family.
* messages.sent_by — the tenant_users.id of a person who replied from the dashboard (NULL for
  the agent and for inbound).
* app_notify_change() — AFTER INSERT/UPDATE row triggers on customers, orders, conversations and
  messages publish {tenant, entity, id, op, parent} on the `tenant_events` channel. NOTIFY is
  transactional, so the dashboard only hears about committed rows, from every writer (API, worker,
  future campaign sender) without each one remembering to publish. Ids only — never a body.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table → column sent as "parent" so the dashboard knows which thread / customer to refresh
NOTIFY_TABLES = {
    "customers": None,
    "orders": "customer_id",
    "conversations": "customer_id",
    "messages": "conversation_id",
}

NOTIFY_FN = """
CREATE FUNCTION app_notify_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify(
    'tenant_events',
    json_build_object(
      't', NEW.tenant_id,
      'e', TG_TABLE_NAME,
      'id', NEW.id,
      'op', lower(TG_OP),
      'p', CASE WHEN TG_NARGS > 0 THEN to_jsonb(NEW) ->> TG_ARGV[0] END
    )::text
  );
  RETURN NULL;
END
$$;
"""


def upgrade() -> None:
    op.add_column("tenant_users", sa.Column("name", sa.Text))
    op.add_column(
        "tenant_users",
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "tenant_users",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column("tenant_users", sa.Column("last_login_at", sa.DateTime(timezone=True)))
    op.create_index("ix_tenant_users_email_lower", "tenant_users", [sa.text("lower(email)")])

    op.create_table(
        "auth_refresh_tokens",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("replaced_by", pg.UUID(as_uuid=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_users.tenant_id", "tenant_users.id"],
            name="fk_auth_refresh_tokens_tenant_user_id_tenant_users",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_auth_refresh_tokens_family_id", "auth_refresh_tokens", ["family_id"])
    op.create_index("ix_auth_refresh_tokens_user_id", "auth_refresh_tokens", ["user_id"])

    op.add_column("messages", sa.Column("sent_by", pg.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_messages_tenant_sent_by_tenant_users",
        "messages",
        "tenant_users",
        ["tenant_id", "sent_by"],
        ["tenant_id", "id"],
    )

    op.execute(NOTIFY_FN)
    for table, parent in NOTIFY_TABLES.items():
        arg = f"'{parent}'" if parent else ""
        op.execute(
            f"CREATE TRIGGER trg_{table}_notify AFTER INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION app_notify_change({arg})"
        )


def downgrade() -> None:
    for table in NOTIFY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_notify ON {table}")
    op.execute("DROP FUNCTION IF EXISTS app_notify_change()")

    op.drop_constraint("fk_messages_tenant_sent_by_tenant_users", "messages", type_="foreignkey")
    op.drop_column("messages", "sent_by")

    op.drop_index("ix_auth_refresh_tokens_user_id", table_name="auth_refresh_tokens")
    op.drop_index("ix_auth_refresh_tokens_family_id", table_name="auth_refresh_tokens")
    op.drop_table("auth_refresh_tokens")

    op.drop_index("ix_tenant_users_email_lower", table_name="tenant_users")
    op.drop_column("tenant_users", "last_login_at")
    op.drop_column("tenant_users", "created_at")
    op.drop_column("tenant_users", "is_active")
    op.drop_column("tenant_users", "name")

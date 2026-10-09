"""Phase 8 (06_multichannel_telegram.md): a channel layer, and Telegram as the second channel.

* tenant_channels.kind ('whatsapp' | 'telegram'). A Telegram channel has no phone_number_id; it has
  telegram_bot_id / telegram_username, its bot token in access_token_encrypted (Fernet, like Meta
  tokens), and only the sha256 of the webhook secret_token we gave Telegram.
* tenant_channels.channel_key: unguessable, per channel, in the webhook URL
  (/webhook/telegram/{channel_key}). Backfilled from a CSPRNG, in Python, for every existing row.
* customer_identities (tenant, RLS): how each channel knows a customer — (kind, external_id).
  WhatsApp identities are kept in step with customers.wa_id by a trigger, so every path that
  creates or edits a customer (ingest, contacts, imports) stays correct untouched. Telegram
  identities are written by the Telegram ingest. Backfilled per tenant (RLS is forced).
* customers.wa_id becomes nullable: a customer known only on Telegram has no number.
* webhook_events.channel_id: which channel a stored raw event came in on (Telegram updates).
* campaigns.channel_kind + campaigns.body: a Telegram campaign is free text, not a Meta template.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09
"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

BACKFILL_IDENTITIES = """
DO $$
DECLARE t uuid;
BEGIN
  FOR t IN SELECT id FROM tenants LOOP
    PERFORM set_config('app.tenant_id', t::text, true);
    INSERT INTO customer_identities (tenant_id, customer_id, kind, external_id, display_name)
    SELECT tenant_id, id, 'whatsapp', wa_id, name FROM customers WHERE wa_id IS NOT NULL
    ON CONFLICT DO NOTHING;
  END LOOP;
  PERFORM set_config('app.tenant_id', '', true);
END $$;
"""

# customers.wa_id → the customer's WhatsApp identity. Runs as the caller, so RLS applies: the row
# it writes carries NEW.tenant_id, which is the session's tenant (WITH CHECK holds).
SYNC_FUNCTION = """
CREATE FUNCTION app_sync_whatsapp_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.wa_id IS NOT DISTINCT FROM OLD.wa_id THEN
    RETURN NEW;
  END IF;
  IF NEW.wa_id IS NULL THEN
    DELETE FROM customer_identities
     WHERE tenant_id = NEW.tenant_id AND customer_id = NEW.id AND kind = 'whatsapp';
    RETURN NEW;
  END IF;
  INSERT INTO customer_identities (tenant_id, customer_id, kind, external_id, display_name)
  VALUES (NEW.tenant_id, NEW.id, 'whatsapp', NEW.wa_id, NEW.name)
  ON CONFLICT (tenant_id, customer_id, kind) DO UPDATE SET external_id = EXCLUDED.external_id;
  RETURN NEW;
END $$;
"""


def upgrade() -> None:
    # ---- tenant_channels
    op.add_column(
        "tenant_channels",
        sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("'whatsapp'")),
    )
    op.add_column("tenant_channels", sa.Column("channel_key", sa.Text))
    op.add_column("tenant_channels", sa.Column("telegram_bot_id", sa.Text))
    op.add_column("tenant_channels", sa.Column("telegram_username", sa.Text))
    op.add_column("tenant_channels", sa.Column("webhook_secret_hash", sa.LargeBinary))
    op.add_column("tenant_channels", sa.Column("webhook_set_at", sa.DateTime(timezone=True)))
    op.add_column("tenant_channels", sa.Column("webhook_error", sa.Text))
    op.alter_column("tenant_channels", "phone_number_id", nullable=True)

    conn = op.get_bind()
    ids = conn.execute(sa.text("SELECT id FROM tenant_channels")).scalars().all()
    for channel_id in ids:  # a CSPRNG value per row, never a SQL random()
        conn.execute(
            sa.text("UPDATE tenant_channels SET channel_key = :k WHERE id = :id"),
            {"k": secrets.token_urlsafe(24), "id": channel_id},
        )
    op.alter_column("tenant_channels", "channel_key", nullable=False)
    op.create_unique_constraint(
        op.f("uq_tenant_channels_channel_key"), "tenant_channels", ["channel_key"]
    )
    op.create_unique_constraint(
        op.f("uq_tenant_channels_telegram_bot_id"), "tenant_channels", ["telegram_bot_id"]
    )
    op.create_check_constraint(
        op.f("ck_tenant_channels_kind"), "tenant_channels", "kind in ('whatsapp','telegram')"
    )
    op.create_check_constraint(
        op.f("ck_tenant_channels_kind_fields"),
        "tenant_channels",
        "(kind = 'whatsapp' AND phone_number_id IS NOT NULL AND telegram_bot_id IS NULL)"
        " OR (kind = 'telegram' AND phone_number_id IS NULL AND telegram_bot_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_tenant_channels_channel_key"),
        "tenant_channels",
        "channel_key ~ '^[A-Za-z0-9_-]{16,64}$'",
    )

    # ---- customer_identities (tenant, RLS)
    op.create_table(
        "customer_identities",
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
        sa.Column("customer_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("username", sa.Text),
        sa.Column("display_name", sa.Text),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name=op.f("fk_customer_identities_customer"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id", "kind", "external_id", name=op.f("uq_customer_identities_external")
        ),
        sa.UniqueConstraint(
            "tenant_id", "customer_id", "kind", name=op.f("uq_customer_identities_customer_kind")
        ),
        sa.CheckConstraint(
            "kind in ('whatsapp','telegram')", name=op.f("ck_customer_identities_kind")
        ),
        sa.CheckConstraint(
            "external_id ~ '^[0-9]{1,32}$'", name=op.f("ck_customer_identities_external_id")
        ),
    )
    op.execute("ALTER TABLE customer_identities ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE customer_identities FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON customer_identities "
        f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
    )
    op.execute(BACKFILL_IDENTITIES)
    op.execute(SYNC_FUNCTION)
    op.execute(
        "CREATE TRIGGER customers_sync_whatsapp_identity "
        "AFTER INSERT OR UPDATE OF wa_id ON customers "
        "FOR EACH ROW EXECUTE FUNCTION app_sync_whatsapp_identity()"
    )
    op.alter_column("customers", "wa_id", nullable=True)

    # ---- webhook_events: which channel a raw event came in on
    op.add_column("webhook_events", sa.Column("channel_id", pg.UUID(as_uuid=True)))
    op.create_foreign_key(
        op.f("fk_webhook_events_channel"),
        "webhook_events",
        "tenant_channels",
        ["tenant_id", "channel_id"],
        ["tenant_id", "id"],
    )

    # ---- campaigns: the channel, and a free-text body for channels without templates
    op.add_column(
        "campaigns",
        sa.Column("channel_kind", sa.Text, nullable=False, server_default=sa.text("'whatsapp'")),
    )
    op.add_column("campaigns", sa.Column("body", sa.Text))
    op.create_check_constraint(
        op.f("ck_campaigns_channel_kind"), "campaigns", "channel_kind in ('whatsapp','telegram')"
    )
    op.create_check_constraint(
        op.f("ck_campaigns_telegram_body"),
        "campaigns",
        "channel_kind = 'whatsapp' OR (template_id IS NULL AND char_length(body) <= 4000)",
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.scalar(sa.text("SELECT count(*) FROM tenant_channels WHERE kind <> 'whatsapp'")):
        raise RuntimeError("non-WhatsApp channels exist: remove them before downgrading 0009")
    # RLS is forced: a NULL-number customer is only visible inside its tenant's context.
    op.execute(
        """
        DO $$
        DECLARE t uuid;
        BEGIN
          FOR t IN SELECT id FROM tenants LOOP
            PERFORM set_config('app.tenant_id', t::text, true);
            IF EXISTS (SELECT 1 FROM customers WHERE wa_id IS NULL) THEN
              RAISE EXCEPTION 'customers without a WhatsApp number exist: cannot downgrade';
            END IF;
          END LOOP;
          PERFORM set_config('app.tenant_id', '', true);
        END $$;
        """
    )

    op.drop_constraint(op.f("ck_campaigns_telegram_body"), "campaigns")
    op.drop_constraint(op.f("ck_campaigns_channel_kind"), "campaigns")
    op.drop_column("campaigns", "body")
    op.drop_column("campaigns", "channel_kind")
    op.drop_constraint(op.f("fk_webhook_events_channel"), "webhook_events")
    op.drop_column("webhook_events", "channel_id")
    op.alter_column("customers", "wa_id", nullable=False)
    op.execute("DROP TRIGGER customers_sync_whatsapp_identity ON customers")
    op.execute("DROP FUNCTION app_sync_whatsapp_identity()")
    op.drop_table("customer_identities")
    for ck in ("ck_tenant_channels_channel_key", "ck_tenant_channels_kind_fields",
               "ck_tenant_channels_kind"):  # fmt: skip
        op.drop_constraint(op.f(ck), "tenant_channels")
    op.drop_constraint(op.f("uq_tenant_channels_telegram_bot_id"), "tenant_channels")
    op.drop_constraint(op.f("uq_tenant_channels_channel_key"), "tenant_channels")
    op.alter_column("tenant_channels", "phone_number_id", nullable=False)
    for col in ("webhook_error", "webhook_set_at", "webhook_secret_hash", "telegram_username",
                "telegram_bot_id", "channel_key", "kind"):  # fmt: skip
        op.drop_column("tenant_channels", col)

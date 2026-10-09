"""`alembic upgrade head` → `downgrade base` → `upgrade head`, cleanly (Phase 0 acceptance).

Sync tests on purpose: Alembic's env.py drives its own event loop.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from api.config import Settings
from api.tests.conftest import alembic_config


def _public_tables(url: str) -> set[str]:
    async def run() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                rows = await conn.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
                return set(rows.all())
        finally:
            await engine.dispose()

    return asyncio.run(run())


# 0009's downgrade refuses while Telegram channels or number-less customers exist (it would orphan
# them). Earlier tests in the session leave some; remove exactly those before going down.
WITHOUT_TELEGRAM = """
DO $$
DECLARE t uuid;
BEGIN
  FOR t IN SELECT id FROM tenants LOOP
    PERFORM set_config('app.tenant_id', t::text, true);
    DELETE FROM campaign_recipients WHERE campaign_id IN
      (SELECT id FROM campaigns WHERE channel_kind <> 'whatsapp')
      OR customer_id IN (SELECT id FROM customers WHERE wa_id IS NULL);
    DELETE FROM campaigns WHERE channel_kind <> 'whatsapp';
    DELETE FROM messages WHERE conversation_id IN (
      SELECT c.id FROM conversations c
      LEFT JOIN customers u ON u.id = c.customer_id
      WHERE u.wa_id IS NULL
         OR c.channel_id IN (SELECT id FROM tenant_channels WHERE kind <> 'whatsapp'));
    DELETE FROM conversations WHERE customer_id IN (SELECT id FROM customers WHERE wa_id IS NULL)
      OR channel_id IN (SELECT id FROM tenant_channels WHERE kind <> 'whatsapp');
    DELETE FROM webhook_events WHERE channel_id IS NOT NULL;
    DELETE FROM optin_visits WHERE customer_id IN (SELECT id FROM customers WHERE wa_id IS NULL);
    DELETE FROM customers WHERE wa_id IS NULL;
  END LOOP;
  PERFORM set_config('app.tenant_id', '', true);
END $$;
"""


def _sql(url: str, *statements: str, params: dict[str, Any] | None = None) -> list[Any]:
    async def run() -> list[Any]:
        engine = create_async_engine(url)
        try:
            out: list[Any] = []
            async with engine.begin() as conn:
                for st in statements:
                    result = await conn.execute(text(st), params or {})
                    out.append(result.all() if result.returns_rows else None)
            return out
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _without_telegram(url: str) -> None:
    _sql(url, WITHOUT_TELEGRAM, "DELETE FROM tenant_channels WHERE kind <> 'whatsapp'")


def test_upgrade_downgrade_upgrade(settings: Settings, migrated: None) -> None:
    assert settings.migrations_database_url is not None
    url = str(settings.migrations_database_url)
    cfg = alembic_config()
    _without_telegram(url)

    at_head = _public_tables(url)
    assert {"tenants", "tenant_channels", "customers", "messages", "usage_daily"} <= at_head

    command.downgrade(cfg, "base")
    assert _public_tables(url) == {"alembic_version"}

    command.upgrade(cfg, "head")
    assert _public_tables(url) == at_head


def test_0009_backfills_identities_and_refuses_to_orphan_telegram(
    settings: Settings, migrated: None
) -> None:
    """Every existing customer gets its WhatsApp identity and every channel a key; going back
    down is refused while a Telegram channel exists — it would leave its customers orphaned."""
    assert settings.migrations_database_url is not None
    url = str(settings.migrations_database_url)
    cfg = alembic_config()
    _without_telegram(url)
    command.downgrade(cfg, "0008")
    tenant, wa = uuid.uuid4(), f"9715{uuid.uuid4().int % 10**8:08d}"
    _sql(
        url,
        "INSERT INTO tenants (id, name, slug) VALUES (:t, 'Pre-0009', :slug)",
        "INSERT INTO tenant_channels (tenant_id, phone_number_id) VALUES (:t, :pnid)",
        "SELECT set_config('app.tenant_id', :ts, true)",
        "INSERT INTO customers (tenant_id, wa_id, name) VALUES (:t, :wa, 'Old Customer')",
        params={
            "t": tenant,
            "slug": f"pre-{tenant.hex[:8]}",
            "pnid": str(10**12 + secrets.randbelow(10**12)),
            "ts": str(tenant),
            "wa": wa,
        },
    )
    command.upgrade(cfg, "head")

    _, identities, keys = _sql(
        url,
        "SELECT set_config('app.tenant_id', :ts, true)",
        "SELECT kind, external_id FROM customer_identities",
        "SELECT count(*) FROM tenant_channels WHERE channel_key IS NULL OR kind <> 'whatsapp'",
        params={"ts": str(tenant)},
    )
    assert [tuple(r) for r in identities] == [("whatsapp", wa)]
    assert keys[0][0] == 0

    _sql(
        url,
        "INSERT INTO tenant_channels (tenant_id, kind, channel_key, telegram_bot_id) "
        "VALUES (:t, 'telegram', :key, :bot)",
        params={
            "t": tenant,
            "key": secrets.token_urlsafe(24),
            "bot": str(secrets.randbelow(10**9)),
        },
    )
    with pytest.raises(RuntimeError, match="non-WhatsApp channels exist"):
        command.downgrade(cfg, "0008")
    assert "customer_identities" in _public_tables(url)  # nothing was dropped
    _without_telegram(url)


def test_single_head() -> None:
    assert len(ScriptDirectory.from_config(alembic_config()).get_heads()) == 1

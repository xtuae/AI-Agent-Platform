"""`alembic upgrade head` → `downgrade base` → `upgrade head`, cleanly (Phase 0 acceptance).

Sync tests on purpose: Alembic's env.py drives its own event loop.
"""

from __future__ import annotations

import asyncio

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


def test_upgrade_downgrade_upgrade(settings: Settings, migrated: None) -> None:
    assert settings.migrations_database_url is not None
    url = str(settings.migrations_database_url)
    cfg = alembic_config()

    at_head = _public_tables(url)
    assert {"tenants", "tenant_channels", "customers", "messages", "usage_daily"} <= at_head

    command.downgrade(cfg, "base")
    assert _public_tables(url) == {"alembic_version"}

    command.upgrade(cfg, "head")
    assert _public_tables(url) == at_head


def test_single_head() -> None:
    assert len(ScriptDirectory.from_config(alembic_config()).get_heads()) == 1

"""Alembic async environment. Runs as the OWNER role (MIGRATIONS_DATABASE_URL), never as `app`."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import api.db.models  # noqa: F401 — populate metadata
from api.config import get_settings
from api.db.base import Base

target_metadata = Base.metadata


def _url() -> str:
    override = context.config.attributes.get("url")
    if isinstance(override, str):
        return override
    settings = get_settings()
    if settings.migrations_database_url is None:
        raise RuntimeError("MIGRATIONS_DATABASE_URL is not set (the owner role runs migrations)")
    return str(settings.migrations_database_url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

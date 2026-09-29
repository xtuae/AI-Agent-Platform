"""Async engine and tenant-scoped sessions.

Two layers of isolation:

1. Database (authoritative): every tenant table has RLS ENABLED + FORCED with a policy on
   `app.tenant_id`. The app connects as a role that owns nothing, so it cannot bypass RLS.
   No tenant context → zero rows.

2. Application (fail loudly): a session opened without a tenant refuses ORM queries or writes
   against tenant-scoped models with `TenantContextMissingError`, so a forgotten context is a
   crash in testing rather than a silently empty screen in production.

The tenant is applied with `set_config('app.tenant_id', <uuid>, true)` — identical semantics to
`SET LOCAL` (transaction-scoped) but parameterised, so a tenant id can never be spliced into SQL.
It is re-applied on every transaction the session begins (`after_begin`), so a session that commits
and continues keeps its tenant, and a pooled connection never carries one to the next borrower.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapper, ORMExecuteState, Session, SessionTransaction

from api.config import Settings

TENANT_KEY = "tenant_id"
_SET_TENANT_SQL = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


class TenantContextMissingError(RuntimeError):
    """A tenant-scoped model was queried or written without a tenant context."""


class TenantMismatchError(RuntimeError):
    """An object's tenant_id differs from the session's tenant context."""


def _is_tenant_scoped(obj_or_mapper: Any) -> bool:
    cls = obj_or_mapper.class_ if isinstance(obj_or_mapper, Mapper) else type(obj_or_mapper)
    return bool(getattr(cls, "__tenant_scoped__", False))


# ---------------------------------------------------------------- session events (global)


@event.listens_for(Session, "after_begin")
def _apply_tenant(session: Session, _tx: SessionTransaction, connection: Connection) -> None:
    tenant_id = session.info.get(TENANT_KEY)
    if tenant_id is not None:
        connection.execute(_SET_TENANT_SQL, {"tenant_id": str(tenant_id)})


@event.listens_for(Session, "do_orm_execute")
def _guard_queries(state: ORMExecuteState) -> None:
    if state.session.info.get(TENANT_KEY) is None and any(
        _is_tenant_scoped(m) for m in state.all_mappers
    ):
        names = sorted(m.class_.__name__ for m in state.all_mappers if _is_tenant_scoped(m))
        raise TenantContextMissingError(f"query on tenant-scoped {names} without a tenant context")


@event.listens_for(Session, "before_flush")
def _guard_writes(session: Session, _ctx: Any, _instances: Any) -> None:
    tenant_id = session.info.get(TENANT_KEY)
    for obj in (*session.new, *session.dirty, *session.deleted):
        if not _is_tenant_scoped(obj):
            continue
        if tenant_id is None:
            raise TenantContextMissingError(
                f"write to tenant-scoped {type(obj).__name__} without a tenant context"
            )
        if obj in session.new and obj.tenant_id is None:
            obj.tenant_id = tenant_id
        elif obj.tenant_id != tenant_id:
            raise TenantMismatchError(f"{type(obj).__name__} belongs to another tenant")


# ---------------------------------------------------------------- engine + factory


class Database:
    def __init__(self, settings: Settings, *, url: str | None = None) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url or str(settings.database_url),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            # Never render bound parameters (message bodies, tokens) into exception text/logs.
            hide_parameters=True,
            connect_args={
                "timeout": settings.db_connect_timeout_s,
                "command_timeout": settings.db_statement_timeout_ms / 1000 + 1,
                "server_settings": {
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "application_name": "hmh-agents",
                },
            },
        )
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def tenant_session(self, tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """One transaction scoped to `tenant_id`. Commits on success, rolls back on error."""
        if not isinstance(tenant_id, uuid.UUID):
            raise TypeError("tenant_id must be a uuid.UUID")
        async with self._sessions() as session:
            session.info[TENANT_KEY] = tenant_id
            async with session.begin():
                yield session

    @asynccontextmanager
    async def platform_session(self) -> AsyncIterator[AsyncSession]:
        """A session with NO tenant context — platform tables only (tenants, tenant_channels…).

        Tenant-scoped ORM access raises TenantContextMissingError; raw SQL against tenant tables
        returns zero rows because of RLS.
        """
        async with self._sessions() as session, session.begin():
            yield session

    async def ping(self) -> bool:
        async with self.engine.connect() as conn:
            result = await conn.scalar(text("SELECT 1"))
            return bool(result == 1)

    async def dispose(self) -> None:
        await self.engine.dispose()

"""FastAPI dependencies for database and Redis access."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import Database


def get_database(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


async def tenant_session(
    request: Request, db: Annotated[Database, Depends(get_database)]
) -> AsyncIterator[AsyncSession]:
    """Tenant-scoped session. The tenant comes from `request.state.tenant_id`, which is set only
    by trusted server-side code (JWT auth from Phase 3, webhook routing from Phase 1) — never from
    the URL or a request body."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not isinstance(tenant_id, uuid.UUID):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="no tenant context")
    async with db.tenant_session(tenant_id) as session:
        yield session


async def platform_session(
    db: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    async with db.platform_session() as session:
        yield session


TenantSession = Annotated[AsyncSession, Depends(tenant_session)]
PlatformSession = Annotated[AsyncSession, Depends(platform_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]
DatabaseDep = Annotated[Database, Depends(get_database)]

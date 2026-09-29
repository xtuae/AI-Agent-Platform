"""Request authentication and role gating for /api/v1.

`Auth` is the only way a dashboard route learns its tenant: the verified access token's `tid`.
Routes open their own tenant transaction with `ctx.tx()` so the commit happens before the
response is sent — a write the client saw succeed has committed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.tokens import (
    AuthNotConfiguredError,
    InvalidTokenError,
    Principal,
    Role,
    decode_access,
)
from api.config import Settings, get_settings
from api.core.logging import get_logger
from api.db.session import Database
from api.deps import get_database, get_redis
from api.modules import registry
from api.modules.registry import Enabled

log = get_logger(__name__)

REVOKED_KEY = "auth:revoked-before:{user_id}"


def unauthorized(detail: str = "not authenticated") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def revoke_access(redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    """Invalidate every access token issued to `user_id` up to now (deactivation, role change,
    password change). Kept for one access-token lifetime — after that they have expired anyway."""
    await redis.set(
        REVOKED_KEY.format(user_id=user_id),
        str(int(datetime.now(UTC).timestamp() * 1000)),
        ex=settings.jwt_access_ttl_s + 60,
    )


async def _revoked(redis: Redis, principal: Principal) -> bool:
    try:
        raw = await redis.get(REVOKED_KEY.format(user_id=principal.user_id))
    except (RedisError, OSError) as exc:
        # Fail open: tokens are signed and expire in minutes, and during a Redis outage the
        # dashboard is how orders still get taken.
        log.warning("auth_revocation_check_failed", error=type(exc).__name__)
        return False
    # strict: a token minted in the same millisecond as the revocation is the replacement
    return raw is not None and principal.issued_at_ms < int(raw)


async def current_principal(
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized()
    settings = get_settings()
    try:
        principal = decode_access(settings, token.strip())
    except AuthNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "auth not configured") from exc
    except InvalidTokenError as exc:
        raise unauthorized("invalid or expired token") from exc
    if await _revoked(redis, principal):
        raise unauthorized("session revoked")
    request.state.tenant_id = principal.tenant_id  # for logging; never read back for scoping
    return principal


@dataclass(frozen=True)
class Ctx:
    """Everything a dashboard route needs, with the tenant fixed by the token."""

    principal: Principal
    db: Database
    redis: Redis

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.principal.tenant_id

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncSession]:
        async with self.db.tenant_session(self.principal.tenant_id) as session:
            yield session

    async def modules(self) -> Enabled:
        """The tenant's enabled modules (one platform query)."""
        async with self.db.platform_session() as s:
            return await registry.enabled_for(s, self.principal.tenant_id)

    @asynccontextmanager
    async def platform(self) -> AsyncIterator[AsyncSession]:
        """Platform tables (tenant_users, tenant_settings…). NO RLS — every query MUST filter
        on `self.tenant_id` explicitly."""
        async with self.db.platform_session() as session:
            yield session


def require(role: Role) -> Callable[..., Coroutine[Any, Any, Ctx]]:
    async def dep(
        principal: Annotated[Principal, Depends(current_principal)],
        db: Annotated[Database, Depends(get_database)],
        redis: Annotated[Redis, Depends(get_redis)],
    ) -> Ctx:
        if not principal.at_least(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires the {role} role")
        return Ctx(principal=principal, db=db, redis=redis)

    return dep


def module_enabled(key: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """Gate for a module's router: a tenant without the module gets 404, exactly as if the route
    did not exist (no hint about which modules exist)."""

    async def dep(
        principal: Annotated[Principal, Depends(current_principal)],
        db: Annotated[Database, Depends(get_database)],
    ) -> None:
        async with db.platform_session() as s:
            enabled = await registry.enabled_for(s, principal.tenant_id)
        if not enabled.has(key):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")

    return dep


Viewer = Annotated[Ctx, Depends(require("viewer"))]
Agent = Annotated[Ctx, Depends(require("agent"))]
Admin = Annotated[Ctx, Depends(require("admin"))]

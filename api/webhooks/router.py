"""phone_number_id → tenant. The highest-risk lookup in the platform (01_architecture §4.1).

If this returns the wrong tenant, Client A's customers receive Client B's answers. Rules:

* The ONLY input is the phone_number_id from the change's own `value.metadata` — never
  entry[0], never a value from another change, never a guess.
* A route exists only for a channel that is active AND whose tenant is trial/active.
  Unknown, inactive or suspended → None, and the caller drops the change.
* Results are cached in Redis for 5 minutes (negative results for 60 s). The cache is an
  optimisation only: any Redis failure falls through to Postgres, never to a guess.
* Template status updates carry no phone_number_id, only the WABA id (entry.id). A WABA
  resolves only if every active channel on it belongs to one routable tenant.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

from api.core.logging import get_logger
from api.db.session import Database

log = get_logger(__name__)

# Meta ids are numeric strings. Anything else is not a channel we could own — reject early.
_META_ID = re.compile(r"^[0-9]{5,32}$")
ROUTABLE_TENANT_STATUSES = ("trial", "active")

_CHANNEL_SQL = text(
    """
    SELECT ch.id AS channel_id, ch.tenant_id, ch.is_active, t.status AS tenant_status
    FROM tenant_channels ch JOIN tenants t ON t.id = ch.tenant_id
    WHERE ch.phone_number_id = :pnid
    """
)
_WABA_SQL = text(
    """
    SELECT DISTINCT ch.tenant_id
    FROM tenant_channels ch JOIN tenants t ON t.id = ch.tenant_id
    WHERE ch.waba_id = :waba AND ch.is_active AND t.status IN ('trial', 'active')
    """
)


@dataclass(frozen=True)
class Route:
    tenant_id: uuid.UUID
    channel_id: uuid.UUID
    phone_number_id: str


def _pnid_key(pnid: str) -> str:
    return f"route:pnid:{pnid}"


def _waba_key(waba: str) -> str:
    return f"route:waba:{waba}"


class TenantRouter:
    def __init__(
        self, db: Database, redis: Redis, *, ttl_s: int = 300, negative_ttl_s: int = 60
    ) -> None:
        self._db = db
        self._redis = redis
        self._ttl = ttl_s
        self._neg_ttl = negative_ttl_s

    # ------------------------------------------------------------ phone_number_id

    async def resolve(self, phone_number_id: str | None) -> Route | None:
        if not phone_number_id or not _META_ID.match(phone_number_id):
            log.warning("route_invalid_phone_number_id")
            return None

        cached = await self._cache_get(_pnid_key(phone_number_id))
        if cached is not None:
            if "t" not in cached or "c" not in cached:
                return None  # cached negative result
            return Route(
                tenant_id=uuid.UUID(cached["t"]),
                channel_id=uuid.UUID(cached["c"]),
                phone_number_id=phone_number_id,
            )

        route, reason = await self._lookup(phone_number_id)
        if route is None:
            log.warning("route_unresolved", phone_number_id=phone_number_id, reason=reason)
            await self._cache_set(_pnid_key(phone_number_id), {"x": reason}, self._neg_ttl)
            return None
        await self._cache_set(
            _pnid_key(phone_number_id),
            {"t": str(route.tenant_id), "c": str(route.channel_id)},
            self._ttl,
        )
        return route

    async def _lookup(self, pnid: str) -> tuple[Route | None, str]:
        async with self._db.platform_session() as s:
            row = (await s.execute(_CHANNEL_SQL, {"pnid": pnid})).one_or_none()
        if row is None:
            return None, "unknown"
        if not row.is_active:
            return None, "channel_inactive"
        if row.tenant_status not in ROUTABLE_TENANT_STATUSES:
            return None, f"tenant_{row.tenant_status}"
        return Route(tenant_id=row.tenant_id, channel_id=row.channel_id, phone_number_id=pnid), "ok"

    # ------------------------------------------------------------ WABA (template updates)

    async def resolve_waba(self, waba_id: str | None) -> uuid.UUID | None:
        if not waba_id or not _META_ID.match(waba_id):
            return None
        cached = await self._cache_get(_waba_key(waba_id))
        if cached is not None:
            t = cached.get("t")
            return uuid.UUID(t) if t else None

        async with self._db.platform_session() as s:
            tenant_ids = list((await s.scalars(_WABA_SQL, {"waba": waba_id})).all())
        if len(tenant_ids) != 1:
            reason = "unknown" if not tenant_ids else "ambiguous"
            log.warning("route_waba_unresolved", waba_id=waba_id, reason=reason)
            await self._cache_set(_waba_key(waba_id), {"x": reason}, self._neg_ttl)
            return None
        tenant_id: uuid.UUID = tenant_ids[0]
        await self._cache_set(_waba_key(waba_id), {"t": str(tenant_id)}, self._ttl)
        return tenant_id

    # ------------------------------------------------------------ cache

    async def invalidate(
        self, *, phone_number_id: str | None = None, waba_id: str | None = None
    ) -> None:
        """Call after any change to tenant_channels or tenant status."""
        keys = [
            k
            for k in (
                _pnid_key(phone_number_id) if phone_number_id else None,
                _waba_key(waba_id) if waba_id else None,
            )
            if k
        ]
        if keys:
            try:
                await self._redis.delete(*keys)
            except (RedisError, OSError) as exc:
                log.error("route_invalidate_failed", error=type(exc).__name__)

    async def _cache_get(self, key: str) -> dict[str, str] | None:
        try:
            raw = await self._redis.get(key)
        except (RedisError, OSError) as exc:
            log.warning("route_cache_unavailable", error=type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            return None
        return value

    async def _cache_set(self, key: str, value: dict[str, str], ttl: int) -> None:
        try:
            await self._redis.set(key, json.dumps(value), ex=ttl)
        except (RedisError, OSError) as exc:
            log.warning("route_cache_unavailable", error=type(exc).__name__)

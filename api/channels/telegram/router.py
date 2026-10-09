"""channel_key → Telegram channel + tenant, with TenantRouter's discipline (api/webhooks/router.py).

* The key's shape is checked first; a malformed key never reaches Redis or Postgres.
* A route exists only for an active Telegram channel whose tenant is trial/active.
* Cached in Redis (positive 5 min, negative 60 s). The cache holds the sha256 of the webhook
  secret, never the secret (we do not keep it at all) and never the bot token. Any Redis failure
  falls through to Postgres, never to a guess.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

from api.core.logging import get_logger
from api.db.session import Database
from api.webhooks.router import ROUTABLE_TENANT_STATUSES

log = get_logger(__name__)

CHANNEL_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_SQL = text(
    """
    SELECT ch.id AS channel_id, ch.tenant_id, ch.kind, ch.is_active, ch.telegram_bot_id,
           ch.webhook_secret_hash, t.status AS tenant_status
    FROM tenant_channels ch JOIN tenants t ON t.id = ch.tenant_id
    WHERE ch.channel_key = :key
    """
)


@dataclass(frozen=True)
class TelegramRoute:
    tenant_id: uuid.UUID
    channel_id: uuid.UUID
    bot_id: str
    secret_hash: bytes | None  # None: no webhook registered → every request is refused


def _key(channel_key: str) -> str:
    return f"route:tgkey:{channel_key}"


class TelegramRouter:
    def __init__(
        self, db: Database, redis: Redis, *, ttl_s: int = 300, negative_ttl_s: int = 60
    ) -> None:
        self._db = db
        self._redis = redis
        self._ttl = ttl_s
        self._neg_ttl = negative_ttl_s

    async def resolve(self, channel_key: str) -> TelegramRoute | None:
        if not CHANNEL_KEY_RE.match(channel_key):
            return None
        cached = await self._cache_get(_key(channel_key))
        if cached is not None:
            if "t" not in cached:
                return None  # cached negative
            return TelegramRoute(
                tenant_id=uuid.UUID(cached["t"]),
                channel_id=uuid.UUID(cached["c"]),
                bot_id=cached["b"],
                secret_hash=bytes.fromhex(cached["h"]) if cached.get("h") else None,
            )
        async with self._db.platform_session() as s:
            row = (await s.execute(_SQL, {"key": channel_key})).one_or_none()
        reason = "ok"
        if row is None:
            reason = "unknown"
        elif row.kind != "telegram":
            reason = "not_telegram"
        elif not row.is_active:
            reason = "channel_inactive"
        elif row.tenant_status not in ROUTABLE_TENANT_STATUSES:
            reason = f"tenant_{row.tenant_status}"
        if row is None or reason != "ok":
            # never log the key: it is what makes the endpoint unguessable
            log.warning("telegram_route_unresolved", reason=reason)
            await self._cache_set(_key(channel_key), {"x": reason}, self._neg_ttl)
            return None
        route = TelegramRoute(
            tenant_id=row.tenant_id,
            channel_id=row.channel_id,
            bot_id=str(row.telegram_bot_id),
            secret_hash=bytes(row.webhook_secret_hash) if row.webhook_secret_hash else None,
        )
        await self._cache_set(
            _key(channel_key),
            {
                "t": str(route.tenant_id),
                "c": str(route.channel_id),
                "b": route.bot_id,
                "h": route.secret_hash.hex() if route.secret_hash else "",
            },
            self._ttl,
        )
        return route

    async def invalidate(self, channel_key: str) -> None:
        """Call after any change to the channel (token, webhook secret, active) or its tenant."""
        try:
            await self._redis.delete(_key(channel_key))
        except (RedisError, OSError) as exc:
            log.error("telegram_route_invalidate_failed", error=type(exc).__name__)

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
        if "t" in value and not all(k in value for k in ("c", "b", "h")):
            return None
        return value

    async def _cache_set(self, key: str, value: dict[str, str], ttl: int) -> None:
        try:
            await self._redis.set(key, json.dumps(value), ex=ttl)
        except (RedisError, OSError) as exc:
            log.warning("route_cache_unavailable", error=type(exc).__name__)

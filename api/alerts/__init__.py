"""Operational alerts to HMH Labz's own WhatsApp, sent through the platform itself (Phase 6).

Anything — the API, a worker job, the backup script — raises an alert with one cheap Redis call
(`raise_alert`). Nothing on a request path talks to Meta: the scheduler's `send_alerts` job
delivers the outbox once a minute.

* Cooldown per alert key, so a persistent problem alerts once per window, not once per minute.
* Delivery: an approved template on HMH Labz's own tenant (ALERT_TENANT_SLUG), one body variable
  ({{1}} = the alert text), to each number in ALERT_TO. A template is required because the ops
  phone is rarely inside a 24-hour service window. Metered like any other send.
* Not configured, or Meta refuses → the alert is still logged at CRITICAL (log shipping and the
  status page see it); delivery is retried for an hour, then given up.
* Alert text is operational only — tenant names, counts, percentages. Never a customer's number
  or a message body.
"""

from __future__ import annotations

import json
import time
from typing import Any, Final

from redis.asyncio import Redis

from api.core.logging import get_logger

log = get_logger(__name__)

OUTBOX: Final = "alerts:outbox"
COOLDOWN_PREFIX: Final = "alerts:cooldown:"
DEFAULT_COOLDOWN_S: Final = 1800
MAX_TEXT: Final = 900  # template variables are capped by Meta; keep it short anyway


async def raise_alert(
    redis: Redis, key: str, text: str, *, cooldown_s: int = DEFAULT_COOLDOWN_S
) -> bool:
    """Queue an alert unless the same key fired within `cooldown_s`. True if queued."""
    log.critical("platform_alert", key=key, text=text)
    try:
        fresh = await redis.set(COOLDOWN_PREFIX + key, "1", nx=True, ex=cooldown_s)
        if not fresh:
            return False
        await redis.rpush(  # type: ignore[misc]
            OUTBOX, json.dumps({"key": key, "text": text[:MAX_TEXT], "at": time.time(), "tries": 0})
        )
    except Exception as exc:  # noqa: BLE001 — raising an alert must never break its caller
        log.critical("platform_alert_unqueued", key=key, error=type(exc).__name__)
        return False
    return True


async def pending(redis: Redis) -> list[dict[str, Any]]:
    return [json.loads(x) for x in await redis.lrange(OUTBOX, 0, -1)]  # type: ignore[misc]

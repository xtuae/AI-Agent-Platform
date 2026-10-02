"""Webhook buffer: when Postgres is unreachable, a signature-verified webhook body is parked in
Redis and the webhook still answers 200 (01 §6.1; Phase 6 "Postgres down → webhook still returns
200 and buffers to Redis"). A worker drains the buffer once the database is back.

* Only bodies that passed X-Hub-Signature-256 are buffered, raw, exactly as Meta sent them; the
  drain re-parses and re-ingests them through the normal path (same routing, same dedup).
* Re-ingest is idempotent: a change committed before the failure kept its wamid claim, so its
  messages count as duplicates; the failed change released its claims and is processed now.
* If Redis cannot take the body either, the handler answers 500 so Meta itself retries — the
  message is then held by Meta, never dropped by us.
* Redis runs with AOF (appendfsync everysec): a simultaneous Redis crash can lose at most about
  a second of buffered webhooks. Meta does not re-send a webhook it got a 200 for.
* Circuit breaker: after a database failure the webhook skips the database for a few seconds and
  buffers straight away, so a down database costs Meta one slow request, not one per message.

Bodies are message content: they are never logged, and live in Redis only until drained.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from api.core.logging import get_logger
from api.webhooks.ingest import IngestFailedError, WebhookIngestor
from api.webhooks.payloads import WebhookPayload

log = get_logger(__name__)

BUFFER_KEY: Final = "webhook:buffer"
PROCESSING_KEY: Final = "webhook:buffer:processing"
BREAKER_OPEN_S: Final = 5.0


class Breaker:
    """In-process: after a DB failure, skip the DB for BREAKER_OPEN_S seconds."""

    def __init__(self, open_s: float = BREAKER_OPEN_S) -> None:
        self._open_s = open_s
        self._until = 0.0

    def trip(self) -> None:
        self._until = time.monotonic() + self._open_s

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self._until


async def push(redis: Redis, body: bytes) -> bool:
    """Park a verified body. False → Redis is down too; the caller must answer non-2xx."""
    try:
        await redis.lpush(BUFFER_KEY, body)  # type: ignore[misc]
    except (RedisError, OSError) as exc:
        log.critical("webhook_buffer_failed", error=type(exc).__name__, size=len(body))
        return False
    log.error("webhook_buffered", size=len(body))
    return True


async def depth(redis: Redis) -> int:
    pending = await redis.llen(BUFFER_KEY)  # type: ignore[misc]
    in_flight = await redis.llen(PROCESSING_KEY)  # type: ignore[misc]
    return int(pending) + int(in_flight)


@dataclass
class DrainReport:
    replayed: int = 0
    dropped: int = 0
    remaining: int = 0
    stopped_on_failure: bool = False


async def drain(redis: Redis, ingestor: WebhookIngestor, *, limit: int = 500) -> DrainReport:
    """Replay buffered bodies oldest first. Stops at the first database failure (the body goes
    back to the front of the line) so order is kept and nothing is lost."""
    report = DrainReport()
    # a body left in PROCESSING by a crashed drain goes back first
    while await redis.lmove(PROCESSING_KEY, BUFFER_KEY, "RIGHT", "RIGHT") is not None:
        pass
    for _ in range(limit):
        body = await redis.lmove(BUFFER_KEY, PROCESSING_KEY, "RIGHT", "LEFT")  # oldest first
        if body is None:
            break
        try:
            payload = WebhookPayload.model_validate_json(body)
        except ValidationError:
            report.dropped += 1  # it parsed once (it was buffered after parsing): cannot happen
            await redis.lrem(PROCESSING_KEY, 1, body)  # type: ignore[misc]
            continue
        try:
            await ingestor.ingest(payload)
        except IngestFailedError:
            await redis.lmove(PROCESSING_KEY, BUFFER_KEY, "LEFT", "RIGHT")  # back to the oldest end
            report.stopped_on_failure = True
            break
        await redis.lrem(PROCESSING_KEY, 1, body)  # type: ignore[misc]
        report.replayed += 1
    report.remaining = await depth(redis)
    if report.replayed or report.stopped_on_failure:
        log.info(
            "webhook_buffer_drained",
            replayed=report.replayed,
            remaining=report.remaining,
            stopped_on_failure=report.stopped_on_failure,
        )
    return report

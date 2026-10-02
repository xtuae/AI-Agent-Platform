"""Scheduler jobs that put work back on track after an outage.

* drain_webhook_buffer — replay webhooks parked in Redis while Postgres was down
  (api.webhooks.buffer). Every 30 seconds; a no-op when the buffer is empty.
* requeue_stranded — an inbound message stored but never answered because its job was lost
  (Redis hiccup at enqueue time, a worker killed mid-job past its retries) is queued again. The
  job id is the same one ingest uses (`in:<wamid>`), so a job already queued is not doubled, and
  the turn itself skips a message already answered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select

from api.config import get_settings
from api.core.logging import get_logger
from api.db.models import Conversation, Message, Tenant
from api.db.session import Database
from api.webhooks import buffer
from api.webhooks.ingest import JOB_INBOUND, WebhookIngestor
from api.webhooks.router import TenantRouter

log = get_logger(__name__)

STRANDED_AFTER: Final = timedelta(minutes=3)  # comfortably past debounce + a normal turn
STRANDED_UNTIL: Final = timedelta(hours=24)  # older than the service window: replying is moot
LIVE_TENANT_STATUSES: Final = ("trial", "active")


class MainQueue:
    """The scheduler's Redis pool defaults to the scheduler queue; work goes to the worker's."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis
        self._queue = get_settings().arq_queue_name

    async def enqueue_job(
        self, function: str, *args: Any, _job_id: str | None = None, _defer_by: float | None = None
    ) -> Any:
        return await self._redis.enqueue_job(
            function, *args, _job_id=_job_id, _queue_name=self._queue, _defer_by=_defer_by
        )


async def drain_webhook_buffer(ctx: dict[str, Any]) -> dict[str, int]:
    redis = ctx["redis"]
    db: Database = ctx["db"]
    settings = get_settings()
    ingestor = WebhookIngestor(
        db,
        redis,
        TenantRouter(db, redis),
        MainQueue(redis),
        dedup_ttl_s=settings.webhook_dedup_ttl_s,
        inbound_defer_s=settings.turn_debounce_s,
    )
    report = await buffer.drain(redis, ingestor)
    return {"replayed": report.replayed, "remaining": report.remaining}


async def requeue_stranded(ctx: dict[str, Any]) -> dict[str, int]:
    db: Database = ctx["db"]
    queue = MainQueue(ctx["redis"])
    now: datetime = ctx.get("clock", lambda: datetime.now(UTC))()
    async with db.platform_session() as s:
        tenant_ids = (
            await s.scalars(select(Tenant.id).where(Tenant.status.in_(LIVE_TENANT_STATUSES)))
        ).all()
    requeued = 0
    for tid in tenant_ids:
        async with db.tenant_session(tid) as s:
            rows = (
                await s.execute(
                    select(Message.id, Message.wamid)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(
                        Message.direction == "in",
                        Message.status == "received",
                        Message.created_at < now - STRANDED_AFTER,
                        Message.created_at > now - STRANDED_UNTIL,
                        Conversation.state != "awaiting_human",
                    )
                )
            ).all()
        for message_id, wamid in rows:
            job = await queue.enqueue_job(
                JOB_INBOUND, str(tid), str(message_id), _job_id=f"in:{wamid or message_id}"
            )
            if job is not None:
                requeued += 1
    if requeued:
        log.warning("stranded_messages_requeued", count=requeued)
    return {"requeued": requeued}

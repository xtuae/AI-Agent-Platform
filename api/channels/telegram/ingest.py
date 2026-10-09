"""Telegram ingest: dedup → normalise → persist → enqueue, for one verified update.

The tenant and channel come from the route (the URL's channel_key, proven by the secret_token);
nothing in the update can redirect it. Same failure semantics as the Meta ingest, except there is
no Redis buffer: on a database failure the claim is released and IngestFailedError raised, the
webhook answers 503, and Telegram redelivers the update (it keeps undelivered updates ~24 h).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from api.channels.inbound import persist_inbound
from api.channels.telegram.payloads import ChatMemberUpdated, Update, normalise
from api.channels.telegram.router import TelegramRoute
from api.core.logging import get_logger
from api.db.models import CustomerIdentity, WebhookEvent
from api.db.session import Database
from api.webhooks.ingest import JOB_INBOUND, Enqueuer, IngestFailedError

log = get_logger(__name__)

BLOCKED_STATUSES = frozenset({"kicked"})  # the user blocked the bot
UNBLOCKED_STATUSES = frozenset({"member"})  # …and unblocked / restarted it


@dataclass
class TelegramIngestReport:
    messages_new: int = 0
    duplicate: bool = False
    ignored: str | None = None
    blocked_changes: int = 0
    jobs_enqueued: int = 0
    tenants: set[uuid.UUID] = field(default_factory=set)


class TelegramIngestor:
    def __init__(
        self,
        db: Database,
        redis: Redis,
        enqueuer: Enqueuer,
        *,
        dedup_ttl_s: int,
        inbound_defer_s: float = 0.0,
    ) -> None:
        self._db = db
        self._redis = redis
        self._enqueuer = enqueuer
        self._dedup_ttl = dedup_ttl_s
        self._inbound_defer = inbound_defer_s or None

    async def ingest(self, route: TelegramRoute, upd: Update) -> TelegramIngestReport:
        report = TelegramIngestReport()
        if upd.my_chat_member is not None:
            await self._member(route, upd.my_chat_member, report)
            return report
        if upd.message is None:
            report.ignored = "edited" if upd.edited_message is not None else "other"
            return report
        inbound = normalise(route.bot_id, upd.message)
        if inbound is None:
            report.ignored = "not_private"
            return report

        dedup_key = f"dedup:tg:{route.channel_id}:{upd.update_id}"
        if not await self._claim(dedup_key):
            report.duplicate = True
            log.info("webhook_duplicate", tenant_id=str(route.tenant_id), channel="telegram")
            return report

        message_id: uuid.UUID | None = None
        committed = False
        try:
            async with self._db.tenant_session(route.tenant_id) as s:
                s.add(
                    WebhookEvent(
                        field="message",
                        kind="messages",
                        channel_id=route.channel_id,
                        payload=upd.model_dump(mode="json", by_alias=True, exclude_none=True),
                        processed_at=datetime.now(UTC),  # the messages row IS the processed form
                    )
                )
                message_id = await persist_inbound(s, route.tenant_id, route.channel_id, inbound)
            committed = True
        except (SQLAlchemyError, OSError) as exc:
            # Type only: DB errors carry bound parameters, i.e. message bodies.
            log.error(
                "webhook_persist_failed", tenant_id=str(route.tenant_id), channel="telegram",
                error=type(exc).__name__,
            )  # fmt: skip
            raise IngestFailedError("persist failed") from None
        finally:
            if not committed:
                await self._release(dedup_key)

        report.tenants.add(route.tenant_id)
        if message_id is None:
            report.duplicate = True
            return report
        report.messages_new = 1
        try:
            if self._inbound_defer:
                await self._enqueuer.enqueue_job(
                    JOB_INBOUND,
                    str(route.tenant_id),
                    str(message_id),
                    _job_id=f"in:{inbound.external_message_id}",
                    _defer_by=self._inbound_defer,
                )
            else:
                await self._enqueuer.enqueue_job(
                    JOB_INBOUND,
                    str(route.tenant_id),
                    str(message_id),
                    _job_id=f"in:{inbound.external_message_id}",
                )
            report.jobs_enqueued = 1
        except (RedisError, OSError) as exc:  # the row is committed; the sweeper re-enqueues it
            log.error("enqueue_failed", job=JOB_INBOUND, error=type(exc).__name__)
        return report

    async def _member(
        self, route: TelegramRoute, change: ChatMemberUpdated, report: TelegramIngestReport
    ) -> None:
        status = change.new_chat_member.status
        if change.chat.type != "private" or (
            status not in BLOCKED_STATUSES and status not in UNBLOCKED_STATUSES
        ):
            report.ignored = "member_other"
            return
        blocked = status in BLOCKED_STATUSES
        try:
            async with self._db.tenant_session(route.tenant_id) as s:
                result = await s.execute(
                    update(CustomerIdentity)
                    .where(
                        CustomerIdentity.kind == "telegram",
                        CustomerIdentity.external_id == str(change.from_.id),
                    )
                    .values(blocked_at=datetime.now(UTC) if blocked else None)
                )
                report.blocked_changes = int(getattr(result, "rowcount", 0) or 0)
        except (SQLAlchemyError, OSError) as exc:
            log.error(
                "webhook_persist_failed", tenant_id=str(route.tenant_id), channel="telegram",
                error=type(exc).__name__,
            )  # fmt: skip
            raise IngestFailedError("persist failed") from None
        report.tenants.add(route.tenant_id)
        log.info(
            "telegram_member_update", tenant_id=str(route.tenant_id), blocked=blocked,
            known=bool(report.blocked_changes),
        )  # fmt: skip

    async def _claim(self, key: str) -> bool:
        try:
            ok = await self._redis.set(key, "1", nx=True, ex=self._dedup_ttl)
        except (RedisError, OSError) as exc:
            log.warning("dedup_unavailable", error=type(exc).__name__)
            return True  # messages.wamid UNIQUE is the backstop
        return bool(ok)

    async def _release(self, key: str) -> None:
        try:
            await self._redis.delete(key)
        except (RedisError, OSError) as exc:
            log.error("dedup_release_failed", error=type(exc).__name__)

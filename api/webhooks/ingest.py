"""Webhook ingest: route → dedup → persist → meter → enqueue, one Meta `change` at a time.

Each change is routed on ITS OWN metadata and written in ITS OWN tenant transaction, so a payload
carrying changes for two tenants can never mix them. Nothing here calls an LLM or the Graph API.

Failure semantics:
* Unknown/inactive/suspended routing, invalid change → logged, dropped, 200 (Meta must not retry).
* Database failure → the Redis dedup claims made for that change are released and
  IngestFailedError is raised; the handler returns 500 so Meta retries, and the retry is not
  mistaken for a duplicate. Changes already committed keep their claims, so they are not
  re-processed on the retry.
* Redis unavailable for dedup → proceed; messages.wamid UNIQUE is the second dedup layer.
* Enqueue failure → logged; the rows are committed and a sweeper re-enqueues (Phase 6).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.logging import get_logger
from api.db.models import Conversation, Customer, Message, WebhookEvent
from api.db.session import Database
from api.metering import record_usage
from api.webhooks.payloads import (
    Change,
    Entry,
    InboundMessage,
    MessagesValue,
    TemplateStatusValue,
    WebhookPayload,
)
from api.webhooks.router import Route, TenantRouter

log = get_logger(__name__)

SERVICE_WINDOW = timedelta(hours=24)
_WA_ID = re.compile(r"^[1-9][0-9]{6,14}$")  # E.164 digits, no '+'

JOB_INBOUND = "handle_inbound_message"
JOB_STATUS = "apply_status_event"
JOB_TEMPLATE = "apply_template_status_event"


class Enqueuer(Protocol):
    async def enqueue_job(self, function: str, *args: Any, _job_id: str | None = None) -> Any: ...


class IngestFailedError(Exception):
    """A change could not be persisted; the webhook must return non-2xx so Meta retries."""


@dataclass
class IngestReport:
    messages_new: int = 0
    messages_duplicate: int = 0
    statuses_new: int = 0
    statuses_duplicate: int = 0
    events_stored: int = 0
    changes_dropped: int = 0
    jobs_enqueued: int = 0
    tenants: set[uuid.UUID] = field(default_factory=set)


@dataclass(frozen=True)
class _Job:
    function: str
    args: tuple[str, ...]
    job_id: str


class WebhookIngestor:
    def __init__(
        self,
        db: Database,
        redis: Redis,
        router: TenantRouter,
        enqueuer: Enqueuer,
        *,
        dedup_ttl_s: int,
    ) -> None:
        self._db = db
        self._redis = redis
        self._router = router
        self._enqueuer = enqueuer
        self._dedup_ttl = dedup_ttl_s

    async def ingest(self, payload: WebhookPayload) -> IngestReport:
        report = IngestReport()
        for entry in payload.entry:
            for change in entry.changes:
                if change.field == "messages":
                    await self._messages_change(entry, change, report)
                else:
                    await self._waba_change(entry, change, report)
        return report

    # ------------------------------------------------------------ field = messages

    async def _messages_change(self, entry: Entry, change: Change, report: IngestReport) -> None:
        if "metadata" not in change.value:
            log.warning("webhook_change_dropped", field=change.field, reason="missing_metadata")
            report.changes_dropped += 1
            return
        try:
            value = MessagesValue.model_validate(change.value)
        except ValidationError as exc:
            log.warning(
                "webhook_change_dropped",
                field=change.field,
                reason="invalid",
                errors=exc.error_count(),
            )
            report.changes_dropped += 1
            return

        route = await self._router.resolve(value.metadata.phone_number_id)
        if route is None:
            report.changes_dropped += 1
            return

        claimed: list[str] = []
        new_messages: list[InboundMessage] = []
        for m in value.messages:
            if await self._claim(f"dedup:wamid:{m.id}", claimed):
                new_messages.append(m)
            else:
                report.messages_duplicate += 1
                log.info("webhook_duplicate", wamid=m.id, tenant_id=str(route.tenant_id))
        new_statuses = 0
        for st in value.statuses:
            if await self._claim(f"dedup:status:{st.id}:{st.status}", claimed):
                new_statuses += 1
            else:
                report.statuses_duplicate += 1
        if not new_messages and not new_statuses:
            return

        jobs: list[_Job] = []
        committed = False
        try:
            async with self._db.tenant_session(route.tenant_id) as s:
                now = datetime.now(UTC)
                if new_messages:
                    s.add(
                        WebhookEvent(
                            field=change.field,
                            kind="messages",
                            phone_number_id=route.phone_number_id,
                            waba_id=entry.id,
                            payload=change.value,
                            processed_at=now,  # the messages rows below ARE the processed form
                        )
                    )
                    report.events_stored += 1
                    for m in new_messages:
                        message_id = await _persist_inbound(s, route, value, m)
                        if message_id is not None:
                            report.messages_new += 1
                            jobs.append(
                                _Job(
                                    JOB_INBOUND,
                                    (str(route.tenant_id), str(message_id)),
                                    f"in:{m.id}",
                                )
                            )
                if new_statuses:
                    event = WebhookEvent(
                        field=change.field,
                        kind="statuses",
                        phone_number_id=route.phone_number_id,
                        waba_id=entry.id,
                        payload=change.value,
                    )
                    s.add(event)
                    await s.flush()
                    report.events_stored += 1
                    report.statuses_new += new_statuses
                    jobs.append(
                        _Job(JOB_STATUS, (str(route.tenant_id), str(event.id)), f"ev:{event.id}")
                    )
            committed = True
        except (SQLAlchemyError, OSError) as exc:
            # Type only: DB errors carry bound parameters / failing rows, i.e. message bodies.
            log.error(
                "webhook_persist_failed", tenant_id=str(route.tenant_id), error=type(exc).__name__
            )
            raise IngestFailedError("persist failed") from None
        finally:
            if not committed:  # any failure: let Meta's retry through the dedup gate
                await self._release(claimed)

        report.tenants.add(route.tenant_id)
        await self._enqueue(jobs, report)

    # ------------------------------------------------------------ WABA-level fields

    async def _waba_change(self, entry: Entry, change: Change, report: IngestReport) -> None:
        """Template status updates, quality updates, account updates: keyed by WABA (entry.id)."""
        kind = "template_status" if change.field == "message_template_status_update" else "other"
        if kind == "template_status":
            try:
                TemplateStatusValue.model_validate(change.value)
            except ValidationError:
                log.warning("webhook_change_dropped", field=change.field, reason="invalid")
                report.changes_dropped += 1
                return

        tenant_id = await self._router.resolve_waba(entry.id)
        if tenant_id is None:
            report.changes_dropped += 1
            return

        digest = hashlib.sha256(
            json.dumps({"f": change.field, "v": change.value}, sort_keys=True).encode()
        ).hexdigest()
        claimed: list[str] = []
        if not await self._claim(f"dedup:waba:{entry.id}:{digest}", claimed):
            return

        jobs: list[_Job] = []
        committed = False
        try:
            async with self._db.tenant_session(tenant_id) as s:
                event = WebhookEvent(
                    field=change.field,
                    kind=kind,
                    waba_id=entry.id,
                    payload=change.value,
                    # 'other' fields are stored for later phases (quality guard is Phase 4).
                    processed_at=None if kind == "template_status" else datetime.now(UTC),
                )
                s.add(event)
                await s.flush()
                report.events_stored += 1
                if kind == "template_status":
                    jobs.append(
                        _Job(JOB_TEMPLATE, (str(tenant_id), str(event.id)), f"ev:{event.id}")
                    )
            committed = True
        except (SQLAlchemyError, OSError) as exc:
            # Type only: DB errors carry bound parameters / failing rows, i.e. message bodies.
            log.error("webhook_persist_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
            raise IngestFailedError("persist failed") from None
        finally:
            if not committed:  # any failure: let Meta's retry through the dedup gate
                await self._release(claimed)

        report.tenants.add(tenant_id)
        await self._enqueue(jobs, report)

    # ------------------------------------------------------------ helpers

    async def _claim(self, key: str, claimed: list[str]) -> bool:
        """SET NX with TTL. True = first sighting. Redis down → True (DB unique is the backstop)."""
        try:
            ok = await self._redis.set(key, "1", nx=True, ex=self._dedup_ttl)
        except (RedisError, OSError) as exc:
            log.warning("dedup_unavailable", error=type(exc).__name__)
            return True
        if ok:
            claimed.append(key)
        return bool(ok)

    async def _release(self, keys: list[str]) -> None:
        if not keys:
            return
        try:
            await self._redis.delete(*keys)
        except (RedisError, OSError) as exc:
            log.error("dedup_release_failed", error=type(exc).__name__, count=len(keys))

    async def _enqueue(self, jobs: list[_Job], report: IngestReport) -> None:
        for job in jobs:
            try:
                await self._enqueuer.enqueue_job(job.function, *job.args, _job_id=job.job_id)
                report.jobs_enqueued += 1
            except (RedisError, OSError) as exc:
                log.error(
                    "enqueue_failed", job=job.function, job_id=job.job_id, error=type(exc).__name__
                )


async def _persist_inbound(
    s: AsyncSession, route: Route, value: MessagesValue, m: InboundMessage
) -> uuid.UUID | None:
    """Customer upsert → conversation upsert → message insert → usage. Returns the new message
    id, or None if the wamid already exists (DB-level dedup) or the sender id is invalid."""
    if not _WA_ID.match(m.from_):
        log.warning("webhook_message_dropped", wamid=m.id, reason="invalid_sender")
        return None

    cust_ins = insert(Customer).values(
        tenant_id=route.tenant_id,
        wa_id=m.from_,
        name=value.profile_name(m.from_),
        source="inbound",
    )
    cust = cust_ins.on_conflict_do_update(
        index_elements=[Customer.tenant_id, Customer.wa_id],
        set_={"name": func.coalesce(Customer.name, cust_ins.excluded.name)},
    ).returning(Customer.id)
    customer_id = (await s.execute(cust)).scalar_one()

    sent_at = m.sent_at
    conv_ins = insert(Conversation).values(
        tenant_id=route.tenant_id,
        customer_id=customer_id,
        channel_id=route.channel_id,
        state="open",
        last_inbound_at=sent_at,
        service_window_expires_at=sent_at + SERVICE_WINDOW,
    )
    conv = conv_ins.on_conflict_do_update(
        index_elements=[Conversation.tenant_id, Conversation.customer_id, Conversation.channel_id],
        index_where=text("state <> 'closed'"),
        set_={
            "last_inbound_at": func.greatest(
                Conversation.last_inbound_at, conv_ins.excluded.last_inbound_at
            ),
            "service_window_expires_at": func.greatest(
                Conversation.service_window_expires_at, conv_ins.excluded.service_window_expires_at
            ),
        },
    ).returning(Conversation.id)
    conversation_id = (await s.execute(conv)).scalar_one()

    media = m.media
    msg = (
        insert(Message)
        .values(
            tenant_id=route.tenant_id,
            conversation_id=conversation_id,
            wamid=m.id,
            direction="in",
            msg_type=m.type,
            body=m.body_text,
            media_url=f"meta-media:{media.id}" if media else None,
            status="received",
        )
        .on_conflict_do_nothing(index_elements=[Message.wamid])
        .returning(Message.id)
    )
    message_id = (await s.execute(msg)).scalar_one_or_none()
    if message_id is None:
        log.info("webhook_duplicate", wamid=m.id, tenant_id=str(route.tenant_id), layer="db")
        return None

    await record_usage(s, route.tenant_id, msgs_in=1)
    log.info("inbound_persisted", wamid=m.id, tenant_id=str(route.tenant_id), msg_type=m.type)
    return message_id

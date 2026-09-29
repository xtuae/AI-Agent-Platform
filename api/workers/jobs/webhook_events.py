"""Jobs that apply stored webhook_events: delivery statuses and template status updates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from api.core.logging import get_logger
from api.db.models import WebhookEvent
from api.db.session import Database
from api.meta.statuses import apply_statuses
from api.meta.templates import apply_template_status
from api.webhooks.payloads import MessagesValue, TemplateStatusValue

log = get_logger(__name__)


async def apply_status_event(ctx: dict[str, Any], tenant_id: str, event_id: str) -> dict[str, Any]:
    db: Database = ctx["db"]
    async with db.tenant_session(uuid.UUID(tenant_id)) as s:
        event = await s.get(WebhookEvent, uuid.UUID(event_id), with_for_update=True)
        if event is None or event.processed_at is not None:
            return {"status": "skipped"}
        try:
            value = MessagesValue.model_validate(event.payload)
        except ValidationError:
            event.error = "invalid payload"
            event.processed_at = datetime.now(UTC)
            return {"status": "invalid"}
        result = await apply_statuses(s, value.statuses)
        event.processed_at = datetime.now(UTC)
    log.info(
        "statuses_applied",
        tenant_id=tenant_id,
        messages=result.messages_updated,
        recipients=result.recipients_updated,
        unknown=result.unknown,
    )
    return {
        "status": "ok",
        "messages": result.messages_updated,
        "recipients": result.recipients_updated,
    }


async def apply_template_status_event(
    ctx: dict[str, Any], tenant_id: str, event_id: str
) -> dict[str, Any]:
    db: Database = ctx["db"]
    async with db.tenant_session(uuid.UUID(tenant_id)) as s:
        event = await s.get(WebhookEvent, uuid.UUID(event_id), with_for_update=True)
        if event is None or event.processed_at is not None:
            return {"status": "skipped"}
        try:
            value = TemplateStatusValue.model_validate(event.payload)
        except ValidationError:
            event.error = "invalid payload"
            event.processed_at = datetime.now(UTC)
            return {"status": "invalid"}
        updated = await apply_template_status(s, value)
        event.processed_at = datetime.now(UTC)
    log.info(
        "template_status_applied", tenant_id=tenant_id, template_event=value.event, updated=updated
    )
    return {"status": "ok", "updated": updated}

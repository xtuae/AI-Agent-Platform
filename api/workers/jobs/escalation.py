"""Escalation alert to the tenant's escalation phone (02 §1: "notify the tenant's escalation
number").

The staff number is usually outside a 24 h service window, so a free-form message would be
rejected by Meta: the alert is sent as an APPROVED utility template configured per tenant in
tenant_settings.feature_flags:

    {"escalation_template": {"name": "handover_alert", "language": "en"}}

with three body variables: {{1}} reason, {{2}} customer number, {{3}} short summary.
No template configured → logged; the dashboard's awaiting-human count (Phase 3) still shows it.
The alert is metered (utility) like every other send.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select

from api.config import Settings
from api.core.logging import get_logger
from api.db.models import AuditLog, Conversation, Customer, TenantChannel, TenantSettings
from api.db.session import Database
from api.meta.client import MetaAPIError
from api.meta.outbound import ChannelNotConfiguredError, client_for_channel
from api.meta.pricing import PricingNotConfiguredError, price_message
from api.metering import record_usage

log = get_logger(__name__)


async def notify_escalation(
    ctx: dict[str, Any], tenant_id: str, conversation_id: str, reason: str, urgency: str
) -> dict[str, Any]:
    db: Database = ctx["db"]
    http: httpx.AsyncClient = ctx["http"]
    settings: Settings = ctx["settings"]
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(conversation_id)

    async with db.platform_session() as s:
        ts = await s.get(TenantSettings, tid)
    phone = ts.escalation_phone if ts else None
    template = ((ts.feature_flags if ts else None) or {}).get("escalation_template")
    if not phone or not isinstance(template, dict) or not template.get("name"):
        log.warning("escalation_notify_unconfigured", tenant_id=tenant_id, has_phone=bool(phone))
        return {"status": "unconfigured"}

    async with db.tenant_session(tid) as s:
        conv = await s.get(Conversation, cid)
        if conv is None:
            return {"status": "missing"}
        customer = await s.get(Customer, conv.customer_id)
        summary = await s.scalar(
            select(AuditLog.after)
            .where(AuditLog.entity_id == cid, AuditLog.action == "escalate")
            .order_by(AuditLog.at.desc())
            .limit(1)
        )
        try:
            cost = await price_message(
                s, category="utility", recipient_wa_id=phone, at=datetime.now(UTC)
            )
        except PricingNotConfiguredError as exc:
            log.error("escalation_notify_unpriced", tenant_id=tenant_id, error=str(exc))
            return {"status": "unpriced"}
    async with db.platform_session() as s:
        channel = await s.scalar(select(TenantChannel).where(TenantChannel.id == conv.channel_id))
    if channel is None:
        return {"status": "missing"}

    short = str((summary or {}).get("summary", ""))[:200] or reason
    try:
        client = client_for_channel(channel, http, settings)
        await client.send_template(
            phone,
            str(template["name"]),
            str(template.get("language") or "en"),
            [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": f"{reason.replace('_', ' ')} ({urgency})"},
                        {"type": "text", "text": f"+{customer.wa_id}" if customer else "unknown"},
                        {"type": "text", "text": short},
                    ],
                }
            ],
        )
    except (MetaAPIError, ChannelNotConfiguredError) as exc:
        log.error("escalation_notify_failed", tenant_id=tenant_id, error=type(exc).__name__)
        return {"status": "failed"}

    async with db.tenant_session(tid) as s:
        await record_usage(s, tid, msgs_out=1, category="utility", meta_cost_aed=cost)
    log.info("escalation_notified", tenant_id=tenant_id, reason=reason, urgency=urgency)
    return {"status": "sent"}

"""Outbound sends with metering (constraint 7). Worker-side only.

Order for every send:
  1. tenant transaction: load conversation + customer, decide the pricing category, and PRICE
     the message. Unpriceable → refuse to send (never send unmetered).
  2. call Meta — outside any DB transaction, so a slow Graph API never holds a connection.
  3. tenant transaction: insert the messages row with cost_aed stamped, bump the conversation,
     and upsert usage_daily — message and meter commit together.
If step 3 fails after Meta accepted the message, it is logged loudly with the wamid so it can be
reconciled; the status webhook will still find no row and be ignored.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from api.config import Settings
from api.core.crypto import decrypt_secret
from api.core.logging import get_logger
from api.db.models import Conversation, Customer, Message, MessageTemplate, TenantChannel
from api.db.session import Database
from api.meta.client import MetaClient, SendResult
from api.meta.pricing import price_message
from api.metering import PricingCategory, record_usage

log = get_logger(__name__)

_TEMPLATE_CATEGORY: dict[str, PricingCategory] = {
    "MARKETING": "marketing",
    "UTILITY": "utility",
    "AUTHENTICATION": "authentication",
}


class OutsideServiceWindowError(Exception):
    """Free-form messages are only allowed inside the 24 h customer service window."""


class ChannelNotConfiguredError(Exception):
    pass


@dataclass(frozen=True)
class LLMUsage:
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    prompt_version: str | None = None
    cost_usd: Decimal = Decimal(0)


def client_for_channel(
    channel: TenantChannel, http: httpx.AsyncClient, settings: Settings
) -> MetaClient:
    if channel.access_token_encrypted is None:
        raise ChannelNotConfiguredError(f"channel {channel.id} has no access token")
    return MetaClient(
        http=http,
        access_token=decrypt_secret(channel.access_token_encrypted),
        phone_number_id=channel.phone_number_id,
        api_version=settings.meta_graph_api_version,
        base_url=settings.meta_graph_base_url,
        max_retries=settings.meta_max_retries,
        timeout=httpx.Timeout(
            settings.meta_http_timeout_s, connect=settings.meta_connect_timeout_s
        ),
        media_max_bytes=settings.meta_media_max_bytes,
    )


@dataclass(frozen=True)
class _Prepared:
    to: str
    category: PricingCategory
    cost_aed: Decimal


async def _prepare(
    db: Database,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    category: PricingCategory | None,
) -> _Prepared:
    async with db.tenant_session(tenant_id) as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None:
            raise LookupError("conversation not found for tenant")
        customer = await s.get(Customer, conv.customer_id)
        if customer is None:
            raise LookupError("customer not found for tenant")
        now = datetime.now(UTC)
        if category is None:  # free-form reply
            if conv.service_window_expires_at is None or conv.service_window_expires_at <= now:
                raise OutsideServiceWindowError(str(conversation_id))
            category = "service"
        cost = await price_message(s, category=category, recipient_wa_id=customer.wa_id, at=now)
        return _Prepared(to=customer.wa_id, category=category, cost_aed=cost)


async def _record(
    db: Database,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    prepared: _Prepared,
    result: SendResult,
    *,
    msg_type: str,
    body: str | None,
    template_name: str | None = None,
    llm: LLMUsage | None = None,
    sent_by: uuid.UUID | None = None,
) -> uuid.UUID:
    try:
        async with db.tenant_session(tenant_id) as s:
            now = datetime.now(UTC)
            msg = Message(
                conversation_id=conversation_id,
                wamid=result.wamid,
                direction="out",
                msg_type=msg_type,
                body=body,
                template_name=template_name,
                pricing_category=prepared.category,
                cost_aed=prepared.cost_aed,
                status="sent",
                llm_model=llm.model if llm else None,
                prompt_tokens=llm.prompt_tokens if llm else None,
                completion_tokens=llm.completion_tokens if llm else None,
                latency_ms=llm.latency_ms if llm else None,
                prompt_version=llm.prompt_version if llm else None,
                sent_by=sent_by,
            )
            s.add(msg)
            conv = await s.get(Conversation, conversation_id)
            if conv is not None:
                conv.last_outbound_at = now
            await record_usage(
                s,
                tenant_id,
                at=now,
                msgs_out=1,
                category=prepared.category,
                meta_cost_aed=prepared.cost_aed,
                llm_prompt_tokens=llm.prompt_tokens if llm else 0,
                llm_completion_tokens=llm.completion_tokens if llm else 0,
                llm_cost_usd=llm.cost_usd if llm else Decimal(0),
            )
            await s.flush()
            message_id = msg.id
    except Exception as exc:
        log.critical(
            "outbound_unrecorded",
            wamid=result.wamid,
            tenant_id=str(tenant_id),
            cost_aed=str(prepared.cost_aed),
            error=type(exc).__name__,
        )
        raise
    log.info(
        "outbound_recorded",
        wamid=result.wamid,
        tenant_id=str(tenant_id),
        category=prepared.category,
        cost_aed=str(prepared.cost_aed),
    )
    return message_id


async def send_text_reply(
    db: Database,
    client: MetaClient,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    text: str,
    llm: LLMUsage | None = None,
    sent_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """`sent_by` is the tenant_users.id when a person replies from the dashboard."""
    prepared = await _prepare(db, tenant_id, conversation_id, category=None)
    result = await client.send_text(prepared.to, text)
    return await _record(
        db,
        tenant_id,
        conversation_id,
        prepared,
        result,
        msg_type="text",
        body=text,
        llm=llm,
        sent_by=sent_by,
    )


async def send_template(
    db: Database,
    client: MetaClient,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    template: MessageTemplate,
    components: list[dict[str, Any]] | None = None,
    rendered_body: str | None = None,
) -> uuid.UUID:
    category = _TEMPLATE_CATEGORY.get((template.category or "").upper())
    if category is None:
        raise ValueError(f"template {template.name!r} has no billable category")
    prepared = await _prepare(db, tenant_id, conversation_id, category=category)
    result = await client.send_template(prepared.to, template.name, template.language, components)
    return await _record(
        db,
        tenant_id,
        conversation_id,
        prepared,
        result,
        msg_type="template",
        body=rendered_body,
        template_name=template.name,
    )

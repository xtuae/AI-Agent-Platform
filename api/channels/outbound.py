"""Outbound sends with metering (constraint 7), for every channel. Worker-side and dashboard.

Order for every send:
  1. tenant transaction: load conversation, customer and channel; check the channel's service
     window (WhatsApp: 24 h; Telegram: none); resolve the recipient on that channel; PRICE the
     message when the channel charges. Unpriceable → refuse to send (never send unmetered).
  2. call the channel — outside any DB transaction, so a slow API never holds a connection.
  3. tenant transaction: insert the messages row with cost_aed stamped, bump the conversation,
     and upsert usage_daily — message and meter commit together.
If step 3 fails after the channel accepted the message, it is logged loudly with the message id so
it can be reconciled.

Meta templates stay in api.meta.outbound (send_template); they reuse _prepare/_record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.channels.base import ChannelSender, caps_for
from api.channels.errors import RecipientBlockedError, RecipientUnreachableError
from api.core.logging import get_logger
from api.db.models import Conversation, Customer, CustomerIdentity, Message, TenantChannel
from api.db.session import Database
from api.meta.client import SendResult
from api.meta.pricing import price_message
from api.metering import PricingCategory, record_usage

log = get_logger(__name__)


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
        channel = await s.get(TenantChannel, conv.channel_id)
        if channel is None or channel.tenant_id != tenant_id:
            raise LookupError("channel not found for tenant")
        caps = caps_for(channel.kind)
        now = datetime.now(UTC)
        if category is None:  # free-form reply
            if caps.service_window is not None and (
                conv.service_window_expires_at is None or conv.service_window_expires_at <= now
            ):
                raise OutsideServiceWindowError(str(conversation_id))
            category = "service"
        to = await recipient_for(s, customer, channel.kind)
        if caps.priced:
            cost = await price_message(s, category=category, recipient_wa_id=to, at=now)
        else:
            cost = Decimal(0)  # the channel does not charge; still metered as a message
        return _Prepared(to=to, category=category, cost_aed=cost)


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


async def recipient_for(s: AsyncSession, customer: Customer, kind: str) -> str:
    """The customer's address on a channel kind. WhatsApp: the number (customers.wa_id). Other
    channels: their identity row, unless they blocked us."""
    if kind == "whatsapp":
        if not customer.wa_id:
            raise RecipientUnreachableError("customer has no WhatsApp number")
        return customer.wa_id
    identity = await s.scalar(
        select(CustomerIdentity).where(
            CustomerIdentity.customer_id == customer.id, CustomerIdentity.kind == kind
        )
    )
    if identity is None:
        raise RecipientUnreachableError(f"customer has no {kind} identity")
    if identity.blocked_at is not None:
        raise RecipientUnreachableError(f"customer blocked the {kind} channel")
    return identity.external_id


async def mark_blocked(db: Database, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    """The channel says the customer blocked us: never send there again until they write."""
    async with db.tenant_session(tenant_id) as s:
        conv = await s.get(Conversation, conversation_id)
        channel = await s.get(TenantChannel, conv.channel_id) if conv else None
        if conv is None or channel is None:
            return
        await s.execute(
            update(CustomerIdentity)
            .where(
                CustomerIdentity.customer_id == conv.customer_id,
                CustomerIdentity.kind == channel.kind,
                CustomerIdentity.blocked_at.is_(None),
            )
            .values(blocked_at=datetime.now(UTC))
        )
    log.warning("recipient_blocked", tenant_id=str(tenant_id), conversation_id=str(conversation_id))


async def send_text_reply(
    db: Database,
    client: ChannelSender,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    text: str,
    llm: LLMUsage | None = None,
    sent_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """A free-form text on the conversation's own channel. `client` is that channel's sender
    (MetaClient / TelegramSender). `sent_by` is the tenant_users.id when a person replies from the
    dashboard. The window check and pricing follow the channel's capabilities."""
    prepared = await _prepare(db, tenant_id, conversation_id, category=None)
    try:
        result = await client.send_text(prepared.to, text)
    except RecipientBlockedError:
        await mark_blocked(db, tenant_id, conversation_id)
        raise RecipientUnreachableError("the customer blocked this channel") from None
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

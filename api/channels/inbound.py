"""The one shape every channel's webhook produces, and how it is stored (06 §3.3, §3.5).

A channel's webhook verifies and parses its own envelope, then hands InboundMessage to
`persist_inbound` inside the tenant's transaction: identity → customer → conversation → message →
usage. (The Meta ingest predates this and keeps its own, equivalent path; WhatsApp identities are
kept in step by the customers.wa_id trigger.)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.channels.base import ChannelKind, caps_for
from api.core.logging import get_logger
from api.db.models import Conversation, Customer, CustomerIdentity, Message
from api.metering import record_usage

log = get_logger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    channel_kind: ChannelKind
    external_user_id: str  # wa_id | Telegram user id
    external_message_id: str  # wamid | tg:<bot_id>:<chat_id>:<message_id> — the dedup key
    sent_at: datetime
    msg_type: str  # text | audio | image | … (WhatsApp's vocabulary)
    text: str | None  # stored, never logged
    media_ref: str | None  # meta-media:<id> | tg-file:<file_id>
    profile_name: str | None
    username: str | None = None
    start_payload: str | None = None  # Telegram /start deep-link payload


async def customer_for(
    s: AsyncSession, tenant_id: uuid.UUID, m: InboundMessage
) -> tuple[uuid.UUID, bool]:
    """The customer behind (kind, external id): found, or created with their identity. Returns
    (customer_id, created). A transaction-scoped advisory lock makes two simultaneous first
    messages from one person create ONE customer."""
    await s.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"identity:{tenant_id}:{m.channel_kind}:{m.external_user_id}"},
    )
    identity = await s.scalar(
        select(CustomerIdentity).where(
            CustomerIdentity.kind == m.channel_kind,
            CustomerIdentity.external_id == m.external_user_id,
        )
    )
    now = datetime.now(UTC)
    if identity is not None:
        identity.last_seen_at = now
        identity.blocked_at = None  # they wrote to us: reachable again
        if m.username:
            identity.username = m.username
        if m.profile_name:
            identity.display_name = m.profile_name
        if m.profile_name:  # fill a missing name, never overwrite one the business set
            await s.execute(
                update(Customer)
                .where(Customer.id == identity.customer_id, Customer.name.is_(None))
                .values(name=m.profile_name)
            )
        return identity.customer_id, False

    customer = Customer(
        tenant_id=tenant_id,
        wa_id=m.external_user_id if m.channel_kind == "whatsapp" else None,
        name=m.profile_name,
        source="inbound" if m.channel_kind == "whatsapp" else m.channel_kind,
    )
    s.add(customer)
    await s.flush()
    if m.channel_kind != "whatsapp":  # the WhatsApp identity is written by the wa_id trigger
        s.add(
            CustomerIdentity(
                tenant_id=tenant_id,
                customer_id=customer.id,
                kind=m.channel_kind,
                external_id=m.external_user_id,
                username=m.username,
                display_name=m.profile_name,
                last_seen_at=now,
            )
        )
        await s.flush()
    return customer.id, True


async def persist_inbound(
    s: AsyncSession, tenant_id: uuid.UUID, channel_id: uuid.UUID, m: InboundMessage
) -> uuid.UUID | None:
    """Customer → conversation → message → usage. Returns the new message id, or None when the
    message id already exists (DB-level dedup)."""
    customer_id, _ = await customer_for(s, tenant_id, m)

    window = caps_for(m.channel_kind).service_window
    conv_ins = insert(Conversation).values(
        tenant_id=tenant_id,
        customer_id=customer_id,
        channel_id=channel_id,
        state="open",
        last_inbound_at=m.sent_at,
        service_window_expires_at=m.sent_at + window if window else None,
    )
    set_: dict[str, object] = {
        "last_inbound_at": func.greatest(
            Conversation.last_inbound_at, conv_ins.excluded.last_inbound_at
        )
    }
    if window:
        set_["service_window_expires_at"] = func.greatest(
            Conversation.service_window_expires_at, conv_ins.excluded.service_window_expires_at
        )
    conv = conv_ins.on_conflict_do_update(
        index_elements=[Conversation.tenant_id, Conversation.customer_id, Conversation.channel_id],
        index_where=text("state <> 'closed'"),
        set_=set_,
    ).returning(Conversation.id)
    conversation_id = (await s.execute(conv)).scalar_one()

    msg = (
        insert(Message)
        .values(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            wamid=m.external_message_id,
            direction="in",
            msg_type=m.msg_type,
            body=m.text,
            media_url=m.media_ref,
            status="received",
        )
        .on_conflict_do_nothing(index_elements=[Message.wamid])
        .returning(Message.id)
    )
    message_id = (await s.execute(msg)).scalar_one_or_none()
    if message_id is None:
        log.info(
            "inbound_duplicate", message_ref=m.external_message_id, tenant_id=str(tenant_id),
            layer="db",
        )  # fmt: skip
        return None

    await record_usage(s, tenant_id, msgs_in=1)
    log.info(
        "inbound_persisted", message_ref=m.external_message_id, tenant_id=str(tenant_id),
        msg_type=m.msg_type, channel=m.channel_kind,
    )  # fmt: skip
    return message_id

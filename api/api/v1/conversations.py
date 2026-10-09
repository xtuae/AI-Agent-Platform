"""/api/v1/conversations — live inbox, full thread, take over, hand back, reply as a person.

Take over sets state = awaiting_human (the agent then stores inbound messages but never replies —
the same switch escalation uses) and assigns the conversation to the person. A person can only
reply to a conversation they have taken over, so they never race the agent. Hand back returns it
to the agent; inbound messages that arrived while a person had it are marked handled, so the agent
does not answer questions the person already dealt with.

On WhatsApp, replies go out inside the 24 h service window only (free-form); outside it Meta
requires an approved template — Phase 4. Telegram has no window: a person can reply any time,
unless the customer blocked the bot.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from api.api.v1.common import (
    MAX_PAGE,
    ChannelRef,
    In,
    audit,
    channel_kinds,
    channels_of,
    conflict,
    identity_matches,
    not_found,
)
from api.auth.deps import Agent, Ctx, Viewer
from api.channels.base import caps_for
from api.channels.errors import ChannelAPIError, RecipientUnreachableError
from api.channels.outbound import (
    ChannelNotConfiguredError,
    OutsideServiceWindowError,
    send_text_reply,
)
from api.channels.registry import sender_for
from api.config import get_settings
from api.core.logging import get_logger
from api.db.models import AuditLog, Conversation, Customer, Message, TenantChannel, TenantUser
from api.meta.pricing import PricingNotConfiguredError

router = APIRouter(prefix="/conversations", tags=["conversations"])
log = get_logger(__name__)

State = Literal["open", "awaiting_human", "closed"]
PREVIEW_CHARS = 120


class CustomerRef(BaseModel):
    id: uuid.UUID
    name: str | None
    wa_id: str | None  # NULL for a customer known only on another channel
    area: str | None


class Escalation(BaseModel):
    reason: str | None
    summary: str | None
    urgency: str | None
    at: datetime
    actor: str


class LastMessage(BaseModel):
    direction: Literal["in", "out"]
    msg_type: str | None
    preview: str | None
    at: datetime


class ConversationRow(BaseModel):
    id: uuid.UUID
    state: State
    customer: CustomerRef
    # the channel this conversation is on, with the customer's handle there
    channel: ChannelRef
    assigned_to: uuid.UUID | None
    assigned_to_name: str | None
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    window_open: bool  # always true on a channel without a service window
    window_expires_at: datetime | None
    language: str | None
    last_message: LastMessage | None
    escalation: Escalation | None
    waiting: int  # inbound messages not yet answered


class MessageOut(BaseModel):
    id: uuid.UUID
    direction: Literal["in", "out"]
    msg_type: str | None
    body: str | None
    transcript: str | None
    template_name: str | None
    status: str | None
    error_code: str | None
    author: Literal["customer", "agent", "person", "template"]
    sent_by_name: str | None
    created_at: datetime


class Thread(ConversationRow):
    summary: str | None
    messages: list[MessageOut]
    has_more: bool


class ConversationPage(BaseModel):
    items: list[ConversationRow]
    total: int


class ReplyIn(In):
    text: str = Field(min_length=1, max_length=4096)


def _preview(m: Message) -> str | None:
    text = m.transcript or m.body
    if text is None:
        return f"[{m.msg_type}]" if m.msg_type else None
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


async def _user_names(ctx: Ctx, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    async with ctx.platform() as s:
        rows = (
            await s.execute(
                select(TenantUser.id, TenantUser.name, TenantUser.email).where(
                    TenantUser.tenant_id == ctx.tenant_id, TenantUser.id.in_(ids)
                )
            )
        ).all()
    return {r.id: r.name or r.email for r in rows}


async def _current_handover(s: Any, ids: list[uuid.UUID]) -> dict[uuid.UUID, AuditLog]:
    """Why each conversation is with a person right now: the agent's latest escalation since the
    last hand-back (its reason and summary are what the person needs), else the latest take-over."""
    recent = (
        select(
            AuditLog.id,
            func.row_number()
            .over(partition_by=AuditLog.entity_id, order_by=AuditLog.at.desc())
            .label("rn"),
        )
        .where(
            AuditLog.entity == "conversation",
            AuditLog.action.in_(("escalate", "takeover", "handback")),
            AuditLog.entity_id.in_(ids),
        )
        .subquery()
    )
    rows = (
        await s.scalars(
            select(AuditLog)
            .join(recent, recent.c.id == AuditLog.id)
            .where(recent.c.rn <= 10)
            .order_by(AuditLog.entity_id, AuditLog.at.desc())
        )
    ).all()
    out: dict[uuid.UUID, AuditLog] = {}
    done: set[uuid.UUID] = set()
    for a in rows:  # newest first per conversation
        cid = a.entity_id
        if cid is None or cid in done:
            continue
        if a.action == "handback":
            done.add(cid)
        elif a.action == "escalate":
            out[cid] = a
            done.add(cid)
        else:
            out.setdefault(cid, a)  # a take-over; keep looking for an escalation before it
    return out


async def _rows(
    ctx: Ctx, s: Any, pairs: list[tuple[Conversation, Customer]]
) -> list[ConversationRow]:
    ids = [c.id for c, _ in pairs]
    if not ids:
        return []
    last = {
        m.conversation_id: m
        for m in (
            await s.scalars(
                select(Message)
                .where(Message.conversation_id.in_(ids))
                .distinct(Message.conversation_id)
                .order_by(Message.conversation_id, Message.created_at.desc())
            )
        ).all()
    }
    waiting = dict(
        (
            await s.execute(
                select(Message.conversation_id, func.count())
                .where(
                    Message.conversation_id.in_(ids),
                    Message.direction == "in",
                    Message.status == "received",
                )
                .group_by(Message.conversation_id)
            )
        ).all()
    )
    esc = await _current_handover(s, ids)
    kinds = await channel_kinds(s, ctx.tenant_id)
    handles = await channels_of(s, list({cust.id for _, cust in pairs}))
    names = await _user_names(ctx, {c.assigned_to for c, _ in pairs if c.assigned_to})
    now = datetime.now(UTC)
    out: list[ConversationRow] = []
    for conv, cust in pairs:
        m = last.get(conv.id)
        kind = kinds.get(conv.channel_id, "whatsapp")
        caps = caps_for(kind)
        handle = next((h for h in handles[cust.id] if h.kind == kind), None)
        a = esc.get(conv.id) if conv.state == "awaiting_human" else None
        after = (a.after or {}) if a else {}
        out.append(
            ConversationRow(
                id=conv.id,
                state=conv.state,  # type: ignore[arg-type]
                customer=CustomerRef(id=cust.id, name=cust.name, wa_id=cust.wa_id, area=cust.area),
                channel=handle or ChannelRef(kind=kind, name=caps.name, handle=None),
                assigned_to=conv.assigned_to,
                assigned_to_name=names.get(conv.assigned_to) if conv.assigned_to else None,
                last_inbound_at=conv.last_inbound_at,
                last_outbound_at=conv.last_outbound_at,
                window_open=caps.service_window is None
                or bool(conv.service_window_expires_at and conv.service_window_expires_at > now),
                window_expires_at=conv.service_window_expires_at,
                language=conv.language,
                last_message=LastMessage(
                    direction=m.direction,
                    msg_type=m.msg_type,
                    preview=_preview(m),
                    at=m.created_at,
                )
                if m
                else None,
                escalation=Escalation(
                    reason=after.get("reason")
                    or ("taken_over" if a.action == "takeover" else None),
                    summary=after.get("summary"),
                    urgency=after.get("urgency"),
                    at=a.at,
                    actor=a.actor,
                )
                if a
                else None,
                waiting=int(waiting.get(conv.id, 0)),
            )
        )
    return out


@router.get("", response_model=ConversationPage)
async def list_conversations(
    ctx: Viewer,
    state: State | None = None,
    live: bool = False,
    q: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationPage:
    """Default: everything not closed, most recent activity first. `live` = service window open."""
    stmt = select(Conversation, Customer).join(Customer, Customer.id == Conversation.customer_id)
    count = (
        select(func.count())
        .select_from(Conversation)
        .join(Customer, Customer.id == Conversation.customer_id)
    )
    conds: list[Any] = [Conversation.state == state] if state else [Conversation.state != "closed"]
    if live:  # window open; on a channel without one, the customer wrote in the last 24 h
        conds.append(
            (Conversation.service_window_expires_at > func.now())
            | (
                Conversation.service_window_expires_at.is_(None)
                & (Conversation.last_inbound_at > func.now() - timedelta(hours=24))
            )
        )
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        conds.append(
            func.lower(Customer.name).like(like)
            | Customer.wa_id.like(like)
            | identity_matches(like.replace("%@", "%", 1))
        )
    activity = func.greatest(Conversation.last_inbound_at, Conversation.last_outbound_at)
    async with ctx.tx() as s:
        total = int(await s.scalar(count.where(*conds)) or 0)
        pairs = [
            (c, u)
            for c, u in (
                await s.execute(
                    stmt.where(*conds)
                    # people waiting on a human first, then most recent activity
                    .order_by(
                        (Conversation.state == "awaiting_human").desc(),
                        activity.desc().nulls_last(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        ]
        items = await _rows(ctx, s, pairs)
    return ConversationPage(items=items, total=total)


async def _thread(
    ctx: Ctx, conversation_id: uuid.UUID, before: datetime | None, limit: int
) -> Thread:
    async with ctx.tx() as s:
        pair = (
            await s.execute(
                select(Conversation, Customer)
                .join(Customer, Customer.id == Conversation.customer_id)
                .where(Conversation.id == conversation_id)
            )
        ).first()
        if pair is None:
            raise not_found("conversation")
        conv, cust = pair
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if before is not None:
            stmt = stmt.where(Message.created_at < before)
        msgs = list(
            (await s.scalars(stmt.order_by(Message.created_at.desc()).limit(limit + 1))).all()
        )
        row = (await _rows(ctx, s, [(conv, cust)]))[0]
    has_more = len(msgs) > limit
    msgs = list(reversed(msgs[:limit]))
    names = await _user_names(ctx, {m.sent_by for m in msgs if m.sent_by})

    def author(m: Message) -> Literal["customer", "agent", "person", "template"]:
        if m.direction == "in":
            return "customer"
        if m.sent_by:
            return "person"
        return "template" if m.template_name else "agent"

    return Thread(
        **row.model_dump(),
        summary=conv.summary,
        has_more=has_more,
        messages=[
            MessageOut(
                id=m.id,
                direction=m.direction,  # type: ignore[arg-type]
                msg_type=m.msg_type,
                body=m.body,
                transcript=m.transcript,
                template_name=m.template_name,
                status=m.status,
                error_code=m.error_code,
                author=author(m),
                sent_by_name=names.get(m.sent_by) if m.sent_by else None,
                created_at=m.created_at,
            )
            for m in msgs
        ],
    )


@router.get("/{conversation_id}", response_model=Thread)
async def get_thread(
    conversation_id: uuid.UUID,
    ctx: Viewer,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
) -> Thread:
    return await _thread(ctx, conversation_id, before, limit)


async def _mark_waiting_handled(s: Any, conversation_id: uuid.UUID) -> None:
    await s.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "in",
            Message.status == "received",
        )
        .values(status="handled")
    )


@router.post("/{conversation_id}/takeover", response_model=Thread)
async def take_over(conversation_id: uuid.UUID, ctx: Agent) -> Thread:
    async with ctx.tx() as s:
        conv = await s.get(Conversation, conversation_id, with_for_update=True)
        if conv is None:
            raise not_found("conversation")
        if conv.state == "closed":
            raise conflict("this conversation is closed")
        if conv.assigned_to not in (None, ctx.principal.user_id) and not ctx.principal.at_least(
            "admin"
        ):
            raise conflict(
                {"code": "assigned_to_someone_else", "assigned_to": str(conv.assigned_to)}
            )
        before = {"state": conv.state, "assigned_to": str(conv.assigned_to or "")}
        conv.state = "awaiting_human"
        conv.assigned_to = ctx.principal.user_id
        audit(
            s,
            ctx,
            "takeover",
            "conversation",
            conv.id,
            before=before,
            after={"state": "awaiting_human", "assigned_to": str(ctx.principal.user_id)},
        )
    return await _thread(ctx, conversation_id, None, 100)


@router.post("/{conversation_id}/handback", response_model=Thread)
async def hand_back(conversation_id: uuid.UUID, ctx: Agent) -> Thread:
    async with ctx.tx() as s:
        conv = await s.get(Conversation, conversation_id, with_for_update=True)
        if conv is None:
            raise not_found("conversation")
        if conv.state != "awaiting_human":
            raise conflict("the agent already has this conversation")
        before = {"state": conv.state, "assigned_to": str(conv.assigned_to or "")}
        conv.state = "open"
        conv.assigned_to = None
        await _mark_waiting_handled(s, conv.id)
        audit(s, ctx, "handback", "conversation", conv.id, before=before, after={"state": "open"})
    return await _thread(ctx, conversation_id, None, 100)


@router.post("/{conversation_id}/messages", response_model=Thread, status_code=201)
async def reply(conversation_id: uuid.UUID, body: ReplyIn, ctx: Agent, request: Request) -> Thread:
    async with ctx.tx() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None:
            raise not_found("conversation")
        if conv.state != "awaiting_human":
            raise conflict({"code": "take_over_first"})
        if conv.assigned_to != ctx.principal.user_id and not ctx.principal.at_least("admin"):
            raise conflict({"code": "assigned_to_someone_else"})
        channel_id = conv.channel_id
    async with ctx.platform() as s:
        channel = await s.scalar(
            select(TenantChannel).where(
                TenantChannel.id == channel_id, TenantChannel.tenant_id == ctx.tenant_id
            )
        )
    if channel is None or not channel.is_active:
        raise conflict({"code": "channel_inactive"})
    http: httpx.AsyncClient = request.app.state.http
    try:
        client = sender_for(channel, http, get_settings())
        await send_text_reply(
            ctx.db,
            client,
            tenant_id=ctx.tenant_id,
            conversation_id=conversation_id,
            text=body.text,
            sent_by=ctx.principal.user_id,
        )
    except OutsideServiceWindowError as exc:
        raise conflict({"code": "window_closed"}) from exc
    except RecipientUnreachableError as exc:  # blocked the bot / no identity on this channel
        raise conflict({"code": "unreachable"}) from exc
    except (ChannelNotConfiguredError, PricingNotConfiguredError) as exc:
        log.error(
            "dashboard_reply_unsendable", tenant_id=str(ctx.tenant_id), error=type(exc).__name__
        )
        raise conflict({"code": "not_sendable", "reason": type(exc).__name__}) from exc
    except ChannelAPIError as exc:
        log.warning(
            "dashboard_reply_failed", tenant_id=str(ctx.tenant_id), error=type(exc).__name__
        )
        code = "whatsapp_rejected" if channel.kind == "whatsapp" else "channel_rejected"
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"code": code}) from exc
    async with ctx.tx() as s:
        await _mark_waiting_handled(s, conversation_id)
    log.info(
        "dashboard_reply_sent", tenant_id=str(ctx.tenant_id), user_id=str(ctx.principal.user_id)
    )
    return await _thread(ctx, conversation_id, None, 100)

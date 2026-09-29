"""Everything the Support Agent sees, built from the database — never from the model.

Token discipline (01 §7.1):
* The rendered system prompt must stay ≤ system_prompt_token_cap (1,500). The spec's base prompt
  is ~800 tokens and its RAG allowance is 4 x 200 = 800, which together exceed the cap — so RAG
  chunks are added whole, best match first, only while the prompt stays under the cap.
* History: the last 10 turns (20 messages) plus the rolling summary, never the full thread.

Customer-controlled strings (WhatsApp profile name, area typed by the customer) are flattened to
one line before they enter the prompt, so they cannot open a fake section like "## ABSOLUTE RULES".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.agents.prompts import compose
from api.agents.text import clean_inline, estimate_tokens, truncate_to_tokens
from api.core.logging import get_logger
from api.db.models import (
    Conversation,
    Customer,
    KnowledgeChunk,
    Message,
    Tenant,
    TenantSettings,
)
from api.db.session import Database
from api.llm.embeddings import Embedder
from api.modules import registry
from api.modules.base import Line
from api.modules.registry import Enabled

log = get_logger(__name__)

HISTORY_MESSAGES = 20  # "last 10 turns"
RAG_TOP_K = 4
RAG_CHUNK_TOKENS = 200


@dataclass(frozen=True)
class Persona:
    tenant_id: uuid.UUID
    agent_name: str
    business_name: str
    business_description: str
    service_areas: str
    cross_sell_category: str
    business_hours: str
    timezone: str
    llm_provider: str
    chat_model: str
    classify_model: str
    escalation_phone: str | None
    feature_flags: dict[str, Any] = field(default_factory=dict)
    enabled: Enabled = field(default_factory=Enabled)  # the tenant's modules

    @property
    def disclosure_markers(self) -> tuple[str, ...]:
        return ("automated", "assistant", "bot", "آلي", "مساعد", "assistant", self.agent_name)

    def today(self, now: datetime) -> date:
        try:
            return now.astimezone(ZoneInfo(self.timezone)).date()
        except ZoneInfoNotFoundError:
            return now.date()


def _render_hours(value: Any) -> str:
    if not value:
        return "not specified — offer to check with the team"
    if isinstance(value, str):
        return clean_inline(value, 200)
    if isinstance(value, dict):
        return "; ".join(
            f"{clean_inline(str(k), 20)} {clean_inline(str(v), 40)}" for k, v in value.items()
        )
    return clean_inline(str(value), 200)


async def load_persona(db: Database, tenant_id: uuid.UUID) -> Persona:
    async with db.platform_session() as s:
        tenant = await s.get(Tenant, tenant_id)
        settings = await s.get(TenantSettings, tenant_id)
        enabled = await registry.enabled_for(s, tenant_id)
    if tenant is None:
        raise LookupError("tenant not found")
    persona: dict[str, Any] = (settings.agent_persona if settings else None) or {}
    missing = [k for k in ("name", "business_description", "service_areas") if not persona.get(k)]
    if missing:
        log.warning("persona_incomplete", tenant_id=str(tenant_id), missing=missing)
    areas = persona.get("service_areas") or []
    return Persona(
        tenant_id=tenant_id,
        agent_name=clean_inline(persona.get("name")) or "the assistant",
        business_name=clean_inline(tenant.name, 80),
        business_description=clean_inline(persona.get("business_description"), 160)
        or "local business",
        service_areas=", ".join(clean_inline(str(a), 40) for a in areas)
        if isinstance(areas, list) and areas
        else "our delivery areas",
        cross_sell_category=clean_inline(persona.get("cross_sell_category"), 30) or "add-on",
        business_hours=_render_hours(settings.business_hours if settings else None),
        timezone=tenant.timezone,
        llm_provider=settings.llm_provider if settings else "gemini",
        chat_model=settings.llm_model_chat if settings else "gemini-3-flash",
        classify_model=settings.llm_model_classify if settings else "gemini-2.5-flash-lite",
        escalation_phone=settings.escalation_phone if settings else None,
        feature_flags=(settings.feature_flags if settings else None) or {},
        enabled=enabled,
    )


async def render_customer_block(
    s: AsyncSession, customer: Customer, today: date, enabled: Enabled
) -> str:
    """02 §2.1 — the known/unknown customer block. Every figure here comes from the DB: the core
    profile and opt-in lines, plus whatever each enabled module adds (orders: last order and
    count; coupons: the live book)."""
    known = bool(customer.area)
    module_lines: list[tuple[int, int, str]] = []
    for i, module in enumerate(enabled.modules):
        if module.customer_block is None:
            continue
        contribution = await module.customer_block(s, customer, today)
        known = known or contribution.known
        module_lines.extend((line.weight, i, line.text) for line in contribution.lines)
    # A WhatsApp profile name alone does not make someone known: they still get the spec's
    # "new contact" block.
    if not known:
        return compose.new_contact_text(enabled.modules)
    area = ", ".join(x for x in (clean_inline(customer.area), clean_inline(customer.emirate)) if x)
    lines = [
        Line(
            0,
            f"Name: {clean_inline(customer.name) or 'unknown'} · Area: {area or 'unknown'} · "
            f"Language: {clean_inline(customer.language, 8) or 'unknown'}",
        )
    ]
    if customer.opt_in_status == "opted_in":
        ev = customer.opt_in_evidence or {}
        when = customer.opt_in_at.date().isoformat() if customer.opt_in_at else "date unknown"
        src = clean_inline(str(ev.get("source", "")), 40).replace("_", " ")
        lines.append(Line(1000, f"Opted in to offers: yes ({when}{', ' + src if src else ''})"))
    else:
        status = "no (opted out)" if customer.opt_in_status == "opted_out" else "not yet"
        lines.append(Line(1000, f"Opted in to offers: {status}"))
    ordered = sorted(
        [(ln.weight, -1, ln.text) for ln in lines] + module_lines, key=lambda t: (t[0], t[1])
    )
    return "\n".join(text for _, _, text in ordered)


async def load_history(
    s: AsyncSession, conversation_id: uuid.UUID
) -> tuple[list[dict[str, str]], str | None]:
    """Last 20 messages as chat turns (oldest first), and the latest outbound body."""
    rows = list(
        (
            await s.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc())
                .limit(HISTORY_MESSAGES)
            )
        ).all()
    )
    rows.reverse()
    history: list[dict[str, str]] = []
    last_out: str | None = None
    for m in rows:
        content = (m.transcript or m.body) if m.direction == "in" else m.body
        if not content:
            content = f"[{m.msg_type or 'message'}]"
        history.append({"role": "user" if m.direction == "in" else "assistant", "content": content})
        if m.direction == "out":
            last_out = m.body
    return history, last_out


async def retrieve_knowledge(s: AsyncSession, embedder: Embedder | None, query: str) -> list[str]:
    if embedder is None or not query.strip():
        return []
    try:
        [vector] = await embedder.embed([query[:2000]])
    except (RuntimeError, ValueError, OSError) as exc:
        log.warning("rag_embed_failed", error=type(exc).__name__)
        return []
    rows = (
        await s.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.embedding.is_not(None))
            .order_by(KnowledgeChunk.embedding.cosine_distance(vector))
            .limit(RAG_TOP_K)
        )
    ).all()
    return [
        truncate_to_tokens(" ".join(f"{c.title or ''} {c.content}".split()), RAG_CHUNK_TOKENS)
        for c in rows
    ]


def render_support_prompt(
    persona: Persona,
    *,
    customer_block: str,
    knowledge: list[str],
    now: datetime,
    token_cap: int,
) -> tuple[str, int, int]:
    """Returns (prompt, chunks_used, estimated_tokens). Chunks are dropped, lowest-ranked first,
    until the prompt fits the cap. Raises if even the chunk-free prompt exceeds it."""

    def render(chunks: list[str]) -> str:
        return compose.render_support(
            persona.enabled.keys,
            agent_name=persona.agent_name,
            business_name=persona.business_name,
            business_description=persona.business_description,
            service_areas=persona.service_areas,
            cross_sell_category=persona.cross_sell_category,
            customer_block=customer_block,
            business_hours=persona.business_hours,
            knowledge_block="\n".join(chunks),
            today=persona.today(now).isoformat(),
            timezone=persona.timezone,
        )

    used = list(knowledge)
    while True:
        text = render(used)
        tokens = estimate_tokens(text)
        if tokens <= token_cap:
            return text, len(used), tokens
        if not used:
            raise ValueError(
                f"support prompt is {tokens} tokens with no knowledge; cap is {token_cap}"
            )
        used.pop()


async def pending_inbound(s: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    """Inbound messages not yet answered (status 'received'), oldest first — answered together
    as one turn. A turn marks exactly the messages it answered as 'handled', so a message that
    arrives while a reply is being generated is never mistaken for answered."""
    return list(
        (
            await s.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "in",
                    Message.status == "received",
                )
                .order_by(Message.created_at)
            )
        ).all()
    )


async def mark_handled(s: AsyncSession, message_ids: list[uuid.UUID]) -> None:
    if message_ids:
        await s.execute(update(Message).where(Message.id.in_(message_ids)).values(status="handled"))


async def is_new_conversation(s: AsyncSession, conversation: Conversation) -> bool:
    n = await s.scalar(
        select(Message.id)
        .where(Message.conversation_id == conversation.id, Message.direction == "out")
        .limit(1)
    )
    return n is None

"""Opt-in capture through a consent page — the TDRA/PDPL evidence trail (01 §10.2).

    QR / short URL  →  /q/<code> consent page (the exact wording)  →  "Continue on WhatsApp"
      → a visit row snapshots the wording and mints a one-time ref  →  wa.me deep link with the
      prefilled message "<prefill> (Ref ABCD2345)"  →  the customer presses send
      → the inbound message carries the ref  →  opted in, deterministically, no model call

Why the ref: the page never learns who the visitor is, and the WhatsApp message never shows which
wording was on screen. The ref ties the two together, so the evidence records the exact wording,
when it was shown, when the customer consented (their own message), where (the link's source),
and the message id.

A ref is claimable once, within VISIT_TTL. A customer who opted out earlier CAN opt back in this
way: scanning, reading the wording and sending the message is their own deliberate act, and the
earlier opt-out is kept in the evidence. (The agent's record_opt_in tool still cannot re-subscribe
them — that path relies on the model's reading of a conversation.)
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import (
    AuditLog,
    Conversation,
    Customer,
    Message,
    OptinLink,
    OptinVisit,
    TenantChannel,
)

CODE_ALPHABET: Final = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: read off a sticker
REF_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN: Final = 8
REF_LEN: Final = 8
VISIT_TTL: Final = timedelta(days=7)
_REF = re.compile(rf"\bref[\s:#.-]*([{REF_ALPHABET}]{{{REF_LEN}}})\b", re.I)


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def new_ref() -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(REF_LEN))


def find_ref(text: str) -> str | None:
    m = _REF.search(text)
    return m.group(1).upper() if m else None


def prefilled(link: OptinLink, ref: str) -> str:
    return f"{link.prefill} (Ref {ref})"


def wa_link(display_phone: str, text: str) -> str:
    return f"https://wa.me/{re.sub(r'\D', '', display_phone)}?text={quote(text)}"


async def whatsapp_number(s: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    """The tenant's live WhatsApp number (platform session)."""
    return await s.scalar(
        select(TenantChannel.display_phone)
        .where(
            TenantChannel.tenant_id == tenant_id,
            TenantChannel.is_active.is_(True),
            TenantChannel.display_phone.is_not(None),
        )
        .order_by(TenantChannel.id)
        .limit(1)
    )


async def record_visit(s: AsyncSession, link: OptinLink) -> OptinVisit:
    """The tap on "Continue on WhatsApp" (tenant session for link.tenant_id)."""
    visit = OptinVisit(
        link_id=link.id,
        token=new_ref(),
        language=link.language,
        heading=link.heading,
        wording=link.wording,
    )
    s.add(visit)
    await s.flush()
    return visit


async def _first_message(s: AsyncSession, customer_id: uuid.UUID) -> bool:
    n = await s.scalar(
        select(func.count())
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.customer_id == customer_id)
    )
    return (n or 0) <= 1


@dataclass(frozen=True)
class Claim:
    customer_id: uuid.UUID
    evidence: dict[str, Any]
    language: str
    already: bool  # was already opted in; nothing changed but the visit is claimed


async def claim(
    s: AsyncSession,
    *,
    text: str,
    customer_id: uuid.UUID,
    wamid: str | None,
    now: datetime,
) -> Claim | None:
    """Opt the sender in if `text` carries a live, unclaimed ref (tenant session). None → not an
    opt-in message; the turn continues as usual."""
    ref = find_ref(text)
    if ref is None:
        return None
    visit = await s.scalar(select(OptinVisit).where(OptinVisit.token == ref).with_for_update())
    if visit is None or visit.claimed_at is not None or now - visit.created_at > VISIT_TTL:
        return None
    link = await s.get(OptinLink, visit.link_id)  # platform row; the composite FK pins its tenant
    customer = await s.get(Customer, customer_id, with_for_update=True)
    if link is None or customer is None:
        return None

    visit.claimed_at, visit.customer_id, visit.wamid = now, customer_id, wamid
    if customer.opt_in_status == "opted_in":
        return Claim(customer_id, customer.opt_in_evidence or {}, visit.language, already=True)

    evidence: dict[str, Any] = {
        "source": link.source,
        "method": "consent_page",
        "wording_shown": visit.wording,
        "heading_shown": visit.heading,
        "language": visit.language,
        "shown_at": visit.created_at.isoformat(),
        "at": now.isoformat(),
        "wamid": wamid,
        "link_code": link.code,
        "link_label": link.label,
        "visit_id": str(visit.id),
    }
    if customer.opt_in_status == "opted_out":
        evidence["previous_opt_out_at"] = (
            customer.opt_out_at.isoformat() if customer.opt_out_at else None
        )
    before = {"opt_in_status": customer.opt_in_status}
    customer.opt_in_status = "opted_in"
    customer.opt_in_at = now
    customer.opt_in_evidence = evidence
    customer.opt_out_at = None
    if not customer.source or (
        customer.source == "inbound" and await _first_message(s, customer_id)
    ):  # this message is how they found us: credit the QR, not "inbound"
        customer.source = link.source
    s.add(
        AuditLog(
            actor="customer",
            action="opt_in_consent_page",
            entity="customer",
            entity_id=customer.id,
            before=before,
            after={"opt_in_status": "opted_in", "evidence": evidence},
        )
    )
    return Claim(customer_id, evidence, visit.language, already=False)

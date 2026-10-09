"""Helpers shared by the /api/v1 dashboard routes."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import Ctx
from api.channels.base import caps_for
from api.db.models import AuditLog, Customer, CustomerIdentity, Tenant, TenantChannel

MAX_PAGE = 200


class In(BaseModel):
    """Request bodies: unknown fields are an error, so a client cannot smuggle `tenant_id`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def not_found(what: str) -> HTTPException:
    # Same answer for "does not exist" and "belongs to another tenant" — RLS makes them identical.
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


def conflict(detail: str | dict[str, Any]) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail)


def unprocessable(code: str, **details: Any) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": code, **details})


def audit(
    s: AsyncSession,
    ctx: Ctx,
    action: str,
    entity: str,
    entity_id: uuid.UUID | None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    s.add(
        AuditLog(
            actor=ctx.principal.actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before=before,
            after=after,
        )
    )


def money(value: Decimal | None) -> str | None:
    return None if value is None else amount(value)


def amount(value: Decimal) -> str:
    """AED to the fils, rounded half up (how an invoice rounds)."""
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


async def load_tenant(ctx: Ctx) -> Tenant:
    async with ctx.platform() as s:
        tenant = await s.get(Tenant, ctx.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "tenant not found")
    return tenant


def zone(tenant: Tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_today(tenant: Tenant, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(zone(tenant)).date()


_NON_DIGITS = re.compile(r"[^\d]")


def normalise_wa_id(raw: str, default_country: str = "971") -> str:
    """E.164 without '+', as WhatsApp reports it. Accepts '+971 50 123 4567', '00971…', and a
    UAE local '050 123 4567'. Raises ValueError for anything that is not 8 to 15 digits after."""
    digits = _NON_DIGITS.sub("", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and not raw.strip().startswith("+"):
        digits = default_country + digits[1:]
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise ValueError("not a valid international phone number")
    return digits


# ---------------------------------------------------------------- channels a customer is on


class ChannelRef(BaseModel):
    kind: str  # whatsapp | telegram
    name: str  # WhatsApp | Telegram
    handle: str | None  # the number (digits, no '+') | "@username" | the Telegram display name
    blocked: bool = False  # they blocked us there: unreachable until they write again


def identity_ref(i: CustomerIdentity) -> ChannelRef:
    if i.kind == "whatsapp":
        handle: str | None = i.external_id
    else:
        handle = f"@{i.username}" if i.username else i.display_name
    return ChannelRef(
        kind=i.kind, name=caps_for(i.kind).name, handle=handle, blocked=i.blocked_at is not None
    )


async def channels_of(
    s: AsyncSession, customer_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ChannelRef]]:
    """Every channel identity of these customers (tenant session), WhatsApp first."""
    out: dict[uuid.UUID, list[ChannelRef]] = {c: [] for c in customer_ids}
    if not customer_ids:
        return out
    rows = await s.scalars(
        select(CustomerIdentity)
        .where(CustomerIdentity.customer_id.in_(customer_ids))
        .order_by(CustomerIdentity.customer_id, CustomerIdentity.kind.desc())  # whatsapp first
    )
    for i in rows:
        out[i.customer_id].append(identity_ref(i))
    return out


async def channel_kinds(s: AsyncSession, tenant_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """channel id → kind, for this tenant's channels (a platform table, so filtered by hand)."""
    rows = await s.execute(
        select(TenantChannel.id, TenantChannel.kind).where(TenantChannel.tenant_id == tenant_id)
    )
    return dict(rows.tuples().all())


def identity_matches(like: str) -> ColumnElement[bool]:
    """Search: a customer whose Telegram @username or channel id matches (lower-case LIKE)."""
    return exists(
        select(CustomerIdentity.id).where(
            CustomerIdentity.customer_id == Customer.id,
            func.lower(CustomerIdentity.username).like(like)
            | CustomerIdentity.external_id.like(like),
        )
    )

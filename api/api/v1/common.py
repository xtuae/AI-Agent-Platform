"""Helpers shared by the /api/v1 dashboard routes."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import Ctx
from api.db.models import AuditLog, Tenant

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
    return None if value is None else str(value.quantize(Decimal("0.01")))


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

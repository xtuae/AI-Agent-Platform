"""What the appointments module contributes at runtime: customer block lines, a
get_customer_context section, Today data and the contact panel."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import (
    Appointment,
    AppointmentResource,
    AppointmentType,
    Customer,
    Tenant,
)
from api.modules.appointments.models import LIVE
from api.modules.appointments.service import iso_local, when
from api.modules.base import CustomerLines, Line, TodayScope

if TYPE_CHECKING:
    from api.agents.tools.base import ToolContext


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _now() -> datetime:
    return datetime.now(UTC)


async def _tenant_zone(s: AsyncSession, tenant_id: uuid.UUID) -> ZoneInfo:
    tz = await s.scalar(select(Tenant.timezone).where(Tenant.id == tenant_id))
    return _zone(tz or "UTC")


def row(a: Appointment, t: AppointmentType, r: AppointmentResource, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "ref": a.ref,
        "type": t.name_en,
        "resource": r.name,
        "starts_at": a.starts_at.isoformat(),
        "local": iso_local(a.starts_at, tz),
        "duration_min": t.duration_min,
        "status": a.status,
        "about": a.subject_label,
    }


def _joined() -> Any:
    return (
        select(Appointment, AppointmentType, AppointmentResource)
        .join(AppointmentType, AppointmentType.id == Appointment.type_id)
        .join(AppointmentResource, AppointmentResource.id == Appointment.resource_id)
    )


async def customer_block(s: AsyncSession, customer: Customer, _today: date) -> CustomerLines:
    tz = await _tenant_zone(s, customer.tenant_id)
    nxt = (
        await s.execute(
            select(Appointment, AppointmentType)
            .join(AppointmentType, AppointmentType.id == Appointment.type_id)
            .where(
                Appointment.customer_id == customer.id,
                Appointment.status.in_(LIVE),
                Appointment.starts_at > _now(),
            )
            .order_by(Appointment.starts_at)
            .limit(1)
        )
    ).first()
    past = await s.scalar(
        select(func.count()).where(
            Appointment.customer_id == customer.id, Appointment.status == "completed"
        )
    )
    lines: list[Line] = []
    if nxt is not None:
        a, t = nxt
        lines.append(
            Line(
                50, f"Next appointment: {when(a.starts_at, tz)}, {t.name_en} ({a.status}, {a.ref})"
            )
        )
    if past:
        lines.append(Line(55, f"Appointments attended: {past}"))
    return CustomerLines(known=nxt is not None or bool(past), lines=tuple(lines))


async def customer_context(ctx: ToolContext) -> dict[str, Any]:
    from api.agents.tools.base import tenant_zone
    from api.modules.appointments.tools.common import view

    tz = tenant_zone(ctx)
    rows = (
        await ctx.session.execute(
            select(Appointment, AppointmentType)
            .join(AppointmentType, AppointmentType.id == Appointment.type_id)
            .where(
                Appointment.customer_id == ctx.customer_id,
                Appointment.status.in_(LIVE),
                Appointment.ends_at > ctx.now,
            )
            .order_by(Appointment.starts_at)
            .limit(5)
        )
    ).all()
    return {"upcoming_appointments": [view(a, t, tz) for a, t in rows]}


async def today(s: AsyncSession, scope: TodayScope) -> dict[str, Any]:
    tz = _zone(scope.timezone)
    start = datetime.combine(scope.today, time.min, tz).astimezone(UTC)
    end = start + timedelta(days=1)
    rows = (
        await s.execute(
            _joined()
            .where(
                Appointment.starts_at >= start,
                Appointment.starts_at < end,
                Appointment.status.in_((*LIVE, "completed", "no_show")),
            )
            .order_by(Appointment.starts_at)
        )
    ).all()
    customers = (
        {
            c.id: c
            for c in (
                await s.scalars(
                    select(Customer).where(Customer.id.in_({a.customer_id for a, _, _ in rows}))
                )
            ).all()
        }
        if rows
        else {}
    )
    now = _now()
    items = []
    for a, t, r in rows:
        item = row(a, t, r, tz)
        c = customers.get(a.customer_id)
        item["customer"] = (c.name or c.wa_id) if c else None
        items.append(item)
    nxt = next(
        (
            i
            for (a, _, _), i in zip(rows, items, strict=True)
            if a.starts_at > now and a.status in LIVE
        ),
        None,
    )
    awaiting = await s.scalar(
        select(func.count()).where(Appointment.status == "requested", Appointment.ends_at > now)
    )
    return {
        "count": sum(1 for a, _, _ in rows if a.status in LIVE or a.status == "completed"),
        "next": nxt,
        "awaiting_confirmation": int(awaiting or 0),
        "items": items,
    }


async def contact_panel(s: AsyncSession, customer_id: uuid.UUID, _today: date) -> dict[str, Any]:
    customer = await s.get(Customer, customer_id)
    tz = await _tenant_zone(s, customer.tenant_id) if customer else _zone("UTC")
    rows = (
        await s.execute(
            _joined()
            .where(Appointment.customer_id == customer_id)
            .order_by(Appointment.starts_at.desc())
            .limit(50)
        )
    ).all()
    now = _now()
    upcoming = [
        row(a, t, r, tz) for a, t, r in reversed(rows) if a.ends_at > now and a.status in LIVE
    ]
    past = [row(a, t, r, tz) for a, t, r in rows if not (a.ends_at > now and a.status in LIVE)]
    return {"upcoming": upcoming, "past": past}

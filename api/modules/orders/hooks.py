"""What the orders module contributes at runtime: customer block lines, get_customer_context
section, Today data, the contact panel."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Customer, Order
from api.modules.base import CustomerLines, Line, TodayScope
from api.modules.orders.service import money
from api.modules.orders.tools.get_order_status import order_view

if TYPE_CHECKING:
    from api.agents.tools.base import ToolContext

OPEN = ("confirmed", "out_for_delivery")
CHART_DAYS = 14


async def _last_order(s: AsyncSession, customer_id: uuid.UUID) -> Order | None:
    last: Order | None = await s.scalar(
        select(Order)
        .where(Order.customer_id == customer_id, Order.status != "cancelled")
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    return last


async def customer_block(s: AsyncSession, customer: Customer, _today: date) -> CustomerLines:
    last = await _last_order(s, customer.id)
    lines: list[Line] = []
    if last is not None:
        qty = sum(int(i.get("qty") or 0) for i in (last.items or []))
        all_water = all(i.get("category") in (None, "water") for i in (last.items or []))
        lines.append(
            Line(
                30,
                f"Last order: {last.created_at.date().isoformat()}, "
                f"{qty} {'bottles' if all_water else 'items'}",
            )
        )
    lines.append(Line(40, f"Orders to date: {customer.lifetime_orders}"))
    return CustomerLines(known=last is not None, lines=tuple(lines))


async def customer_context(ctx: ToolContext) -> dict[str, Any]:
    customer = await ctx.session.get(Customer, ctx.customer_id)
    orders = (
        await ctx.session.scalars(
            select(Order)
            .where(Order.customer_id == ctx.customer_id, Order.status != "cancelled")
            .order_by(Order.created_at.desc())
            .limit(3)
        )
    ).all()
    return {
        "recent_orders": [order_view(o) for o in orders],
        "lifetime_orders": customer.lifetime_orders if customer else 0,
    }


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


async def today(s: AsyncSession, scope: TodayScope) -> dict[str, Any]:
    tz = _zone(scope.timezone)
    day_start = datetime.combine(scope.today, time.min, tz).astimezone(UTC)
    chart_start_day = scope.today - timedelta(days=CHART_DAYS - 1)
    chart_start = datetime.combine(chart_start_day, time.min, tz).astimezone(UTC)
    count, value = (
        await s.execute(
            select(func.count(), func.coalesce(func.sum(Order.total_aed), 0)).where(
                Order.created_at >= day_start, Order.status != "cancelled"
            )
        )
    ).one()
    due = await s.scalar(
        select(func.count()).where(Order.delivery_date == scope.today, Order.status.in_(OPEN))
    )
    unscheduled = await s.scalar(
        select(func.count()).where(Order.delivery_date.is_(None), Order.status.in_(OPEN))
    )
    local_day = cast(func.timezone(scope.timezone, Order.created_at), Date)
    by_day = {
        d: (int(n), v)
        for d, n, v in (
            await s.execute(
                select(local_day, func.count(), func.coalesce(func.sum(Order.total_aed), 0))
                .where(Order.created_at >= chart_start, Order.status != "cancelled")
                .group_by(local_day)
            )
        ).all()
    }
    points = []
    for i in range(CHART_DAYS):
        d = chart_start_day + timedelta(days=i)
        n, v = by_day.get(d, (0, Decimal(0)))
        points.append({"day": d.isoformat(), "orders": n, "value_aed": money(Decimal(v))})
    return {
        "count": int(count),
        "value_aed": money(Decimal(value)),
        "deliveries_due": int(due or 0),
        "unscheduled": int(unscheduled or 0),
        "by_day": points,
    }


async def contact_panel(s: AsyncSession, customer_id: uuid.UUID, _today: date) -> dict[str, Any]:
    customer = await s.get(Customer, customer_id)
    orders = (
        await s.scalars(
            select(Order)
            .where(Order.customer_id == customer_id)
            .order_by(Order.created_at.desc())
            .limit(50)
        )
    ).all()
    return {
        "lifetime_orders": customer.lifetime_orders if customer else 0,
        "last_order_at": customer.last_order_at.isoformat()
        if customer and customer.last_order_at
        else None,
        "orders": [
            {
                "id": str(o.id),
                "order_no": o.order_no,
                "status": o.status,
                "total_aed": money(o.total_aed),
                "created_at": o.created_at.isoformat(),
                "delivery_date": o.delivery_date.isoformat() if o.delivery_date else None,
            }
            for o in orders
        ],
    }

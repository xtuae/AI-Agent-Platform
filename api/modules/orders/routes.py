"""/api/v1/m/orders — the screen that replaces the Excel sheet (01 §8.2 item 2).

Prices and totals are computed server-side by api.commerce.orders — the dashboard sends SKUs and
quantities only, exactly like the agent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select

from api.api.v1.common import (
    MAX_PAGE,
    In,
    audit,
    conflict,
    load_tenant,
    local_today,
    money,
    not_found,
    unprocessable,
    zone,
)
from api.auth.deps import Agent, Viewer
from api.db.models import AuditLog, Customer, Order
from api.modules.orders.service import (
    OrderError,
    OrderRequest,
    cancel_hooks,
    place_order,
    redeem_hook,
)

router = APIRouter()

OrderStatus = Literal["draft", "confirmed", "out_for_delivery", "delivered", "cancelled"]
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"confirmed", "cancelled"}),
    "confirmed": frozenset({"out_for_delivery", "cancelled"}),
    # a failed drop goes back to the queue
    "out_for_delivery": frozenset({"delivered", "confirmed", "cancelled"}),
    "delivered": frozenset(),
    "cancelled": frozenset(),
}
OPEN_FOR_DELIVERY = ("confirmed", "out_for_delivery")
# A person typing an order can legitimately exceed the agent's 200-per-SKU escalation guard (an
# office order), so the dashboard cap is only a typo guard.
DASHBOARD_MAX_QTY = 1000
IDEMPOTENCY_WINDOW = timedelta(hours=24)


# ---------------------------------------------------------------- schemas


class CustomerRef(BaseModel):
    id: uuid.UUID
    name: str | None
    wa_id: str | None  # NULL for a customer known only on Telegram
    area: str | None
    address_note: str | None


class OrderLine(BaseModel):
    sku: str
    name: str | None = None
    qty: int
    unit_price_aed: str | None = None
    line_total_aed: str | None = None
    paid_with_coupon: bool = False


class OrderOut(BaseModel):
    id: uuid.UUID
    order_no: str
    status: OrderStatus
    items: list[OrderLine]
    total_aed: str | None
    source: str | None
    area: str | None
    delivery_date: date | None
    delivery_slot: str | None
    notes: str | None
    created_by: str | None
    created_at: datetime
    conversation_id: uuid.UUID | None
    customer: CustomerRef
    next_statuses: list[OrderStatus]


class HistoryEntry(BaseModel):
    at: datetime
    actor: str
    action: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class OrderDetail(OrderOut):
    history: list[HistoryEntry]


class OrderPage(BaseModel):
    items: list[OrderOut]
    total: int


class ItemIn(In):
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1, le=DASHBOARD_MAX_QTY)


class OrderCreate(In):
    customer_id: uuid.UUID
    items: list[ItemIn] = Field(min_length=1, max_length=30)
    use_coupon_book: bool = False  # honoured only when a prepaid module (coupons) is enabled
    area: str | None = Field(default=None, max_length=120)
    delivery_date: date | None = None
    delivery_slot: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=500)
    status: Literal["draft", "confirmed"] = "confirmed"


class OrderPatch(In):
    status: OrderStatus | None = None
    area: str | None = Field(default=None, max_length=120)
    delivery_date: date | None = None
    delivery_slot: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=500)


class DeliveryStop(BaseModel):
    order_no: str
    status: OrderStatus
    customer_name: str | None
    wa_id: str | None
    address_note: str | None
    delivery_slot: str | None
    items: list[OrderLine]
    total_aed: str | None
    notes: str | None


class DeliveryGroup(BaseModel):
    area: str
    stops: list[DeliveryStop]
    bottles: int
    to_collect_aed: str


class DeliveryList(BaseModel):
    date: date
    groups: list[DeliveryGroup]
    unscheduled: int  # open orders with no delivery date yet


# ---------------------------------------------------------------- helpers


def _lines(order: Order) -> list[OrderLine]:
    out: list[OrderLine] = []
    for line in order.items or []:
        if isinstance(line, dict):
            out.append(
                OrderLine(
                    sku=str(line.get("sku", "")),
                    name=line.get("name"),
                    qty=int(line.get("qty") or 0),
                    unit_price_aed=line.get("unit_price_aed"),
                    line_total_aed=line.get("line_total_aed"),
                    paid_with_coupon=bool(line.get("paid_with_coupon")),
                )
            )
    return out


def _out(order: Order, customer: Customer) -> OrderOut:
    return OrderOut(
        id=order.id,
        order_no=order.order_no,
        status=order.status,  # type: ignore[arg-type]  # DB CHECK guarantees the literal
        items=_lines(order),
        total_aed=money(order.total_aed),
        source=order.source,
        area=order.area,
        delivery_date=order.delivery_date,
        delivery_slot=order.delivery_slot,
        notes=order.notes,
        created_by=order.created_by,
        created_at=order.created_at,
        conversation_id=order.conversation_id,
        customer=CustomerRef(
            id=customer.id,
            name=customer.name,
            wa_id=customer.wa_id,
            area=customer.area,
            address_note=customer.address_note,
        ),
        next_statuses=sorted(TRANSITIONS.get(order.status, frozenset())),  # type: ignore[arg-type]
    )


def _local_bounds(day_from: date | None, day_to: date | None, tz: Any) -> tuple[Any, Any]:
    start = datetime.combine(day_from, time.min, tz).astimezone(UTC) if day_from else None
    end = (
        datetime.combine(day_to + timedelta(days=1), time.min, tz).astimezone(UTC)
        if day_to
        else None
    )
    return start, end


def _filtered(
    stmt: Select[Any],
    *,
    statuses: list[str],
    area: str | None,
    q: str | None,
    date_field: str,
    day_from: date | None,
    day_to: date | None,
    tz: Any,
) -> Select[Any]:
    if statuses:
        stmt = stmt.where(Order.status.in_(statuses))
    if area:
        stmt = stmt.where(func.lower(Order.area) == area.lower())
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                Order.order_no == q,
                func.lower(Customer.name).like(like),
                Customer.wa_id.like(f"%{q}%"),
            )
        )
    if date_field == "delivery":
        if day_from:
            stmt = stmt.where(Order.delivery_date >= day_from)
        if day_to:
            stmt = stmt.where(Order.delivery_date <= day_to)
    else:
        start, end = _local_bounds(day_from, day_to, tz)
        if start is not None:
            stmt = stmt.where(Order.created_at >= start)
        if end is not None:
            stmt = stmt.where(Order.created_at < end)
    return stmt


def _order_error(exc: OrderError) -> Exception:
    return unprocessable(exc.code, **exc.details)


# ---------------------------------------------------------------- routes


@router.get("", response_model=OrderPage)
async def list_orders(
    ctx: Viewer,
    status_: Annotated[list[OrderStatus] | None, Query(alias="status")] = None,
    area: Annotated[str | None, Query(max_length=120)] = None,
    q: Annotated[str | None, Query(max_length=80)] = None,
    date_field: Literal["created", "delivery"] = "created",
    day_from: Annotated[date | None, Query(alias="from")] = None,
    day_to: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderPage:
    tz = zone(await load_tenant(ctx))
    base = select(Order, Customer).join(Customer, Customer.id == Order.customer_id)
    filters: dict[str, Any] = {
        "statuses": list(status_ or []),
        "area": area,
        "q": q.strip() if q else None,
        "date_field": date_field,
        "day_from": day_from,
        "day_to": day_to,
        "tz": tz,
    }
    stmt = _filtered(base, **filters)
    count_stmt = _filtered(
        select(func.count()).select_from(Order).join(Customer, Customer.id == Order.customer_id),
        **filters,
    )
    order_col = Order.delivery_date if date_field == "delivery" else Order.created_at
    async with ctx.tx() as s:
        total = int(await s.scalar(count_stmt) or 0)
        rows = (
            await s.execute(
                stmt.order_by(order_col.desc().nulls_last(), Order.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    return OrderPage(items=[_out(o, c) for o, c in rows], total=total)


@router.get("/areas", response_model=list[str])
async def list_areas(ctx: Viewer) -> list[str]:
    """Areas seen on orders and customers — for the filter and the delivery list."""
    async with ctx.tx() as s:
        a = (await s.scalars(select(Order.area).where(Order.area.is_not(None)).distinct())).all()
        b = (
            await s.scalars(select(Customer.area).where(Customer.area.is_not(None)).distinct())
        ).all()
    return sorted({x.strip() for x in (*a, *b) if x and x.strip()}, key=str.lower)


@router.get("/delivery-list", response_model=DeliveryList)
async def delivery_list(
    ctx: Viewer,
    day: Annotated[date | None, Query(alias="date")] = None,
    area: Annotated[str | None, Query(max_length=120)] = None,
) -> DeliveryList:
    tenant = await load_tenant(ctx)
    day = day or local_today(tenant)
    stmt = (
        select(Order, Customer)
        .join(Customer, Customer.id == Order.customer_id)
        .where(Order.delivery_date == day, Order.status.in_(OPEN_FOR_DELIVERY))
    )
    if area:
        stmt = stmt.where(func.lower(Order.area) == area.lower())
    async with ctx.tx() as s:
        rows = (await s.execute(stmt.order_by(Order.area, Order.order_no))).all()
        unscheduled = int(
            await s.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.delivery_date.is_(None), Order.status.in_(OPEN_FOR_DELIVERY))
            )
            or 0
        )
    groups: dict[str, DeliveryGroup] = {}
    to_collect: dict[str, Decimal] = {}
    for order, customer in rows:
        key = (order.area or customer.area or "No area").strip()
        g = groups.setdefault(key, DeliveryGroup(area=key, stops=[], bottles=0, to_collect_aed=""))
        lines = _lines(order)
        g.stops.append(
            DeliveryStop(
                order_no=order.order_no,
                status=order.status,
                customer_name=customer.name,
                wa_id=customer.wa_id,
                address_note=customer.address_note,
                delivery_slot=order.delivery_slot,
                items=lines,
                total_aed=money(order.total_aed),
                notes=order.notes,
            )
        )
        g.bottles += sum(ln.qty for ln in lines)
        to_collect[key] = to_collect.get(key, Decimal(0)) + (order.total_aed or Decimal(0))
    for key, g in groups.items():
        g.to_collect_aed = money(to_collect[key]) or "0.00"
    return DeliveryList(
        date=day,
        groups=sorted(groups.values(), key=lambda g: g.area.lower()),
        unscheduled=unscheduled,
    )


@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(order_id: uuid.UUID, ctx: Viewer) -> OrderDetail:
    async with ctx.tx() as s:
        row = (
            await s.execute(
                select(Order, Customer)
                .join(Customer, Customer.id == Order.customer_id)
                .where(Order.id == order_id)
            )
        ).first()
        if row is None:
            raise not_found("order")
        history = (
            await s.scalars(
                select(AuditLog)
                .where(AuditLog.entity == "order", AuditLog.entity_id == order_id)
                .order_by(AuditLog.at)
            )
        ).all()
    base = _out(row[0], row[1])
    return OrderDetail(
        **base.model_dump(),
        history=[
            HistoryEntry(at=h.at, actor=h.actor, action=h.action, before=h.before, after=h.after)
            for h in history
        ],
    )


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    body: OrderCreate,
    ctx: Agent,
    response: Response,
    idempotency_key: Annotated[str | None, Header(max_length=64)] = None,
) -> OrderOut:
    tenant = await load_tenant(ctx)
    now = datetime.now(UTC)
    today = local_today(tenant, now)
    if body.delivery_date is not None and body.delivery_date < today:
        raise unprocessable("delivery_date_in_past")
    wanted: dict[str, int] = {}
    for it in body.items:
        wanted[it.sku] = wanted.get(it.sku, 0) + it.qty
    if any(q > DASHBOARD_MAX_QTY for q in wanted.values()):
        raise unprocessable("quantity_above_limit", limit=DASHBOARD_MAX_QTY)
    key = f"dash:{idempotency_key}" if idempotency_key else None
    enabled = await ctx.modules()

    async with ctx.tx() as s:
        customer = await s.get(Customer, body.customer_id)
        if customer is None:
            raise not_found("customer")
        if key is not None:
            existing = await s.scalar(
                select(Order).where(
                    Order.idempotency_key == key,
                    Order.conversation_id.is_(None),
                    Order.created_at > now - IDEMPOTENCY_WINDOW,
                )
            )
            if existing is not None:  # a retried submit — return the order, do not place twice
                response.status_code = status.HTTP_200_OK
                return _out(existing, customer)
        try:
            placed = await place_order(
                s,
                ctx.tenant_id,
                OrderRequest(
                    customer_id=customer.id,
                    items=wanted,
                    area=body.area or customer.area,
                    source="dashboard",
                    created_by=ctx.principal.actor,
                    use_prepaid=body.use_coupon_book,
                    status=body.status,
                    delivery_date=body.delivery_date,
                    delivery_slot=body.delivery_slot,
                    notes=body.notes,
                    idempotency_key=key,
                ),
                now=now,
                today=today,
                redeem=redeem_hook(enabled),
            )
        except OrderError as exc:
            raise _order_error(exc) from exc
        order = placed.order
        audit(
            s,
            ctx,
            "create_order",
            "order",
            order.id,
            after={
                "order_no": order.order_no,
                "status": order.status,
                "total_aed": money(order.total_aed),
                "items": placed.lines,
            },
        )
        await s.refresh(order)
        return _out(order, customer)


@router.patch("/{order_id}", response_model=OrderOut)
async def update_order(order_id: uuid.UUID, body: OrderPatch, ctx: Agent) -> OrderOut:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise unprocessable("nothing_to_change")
    enabled = await ctx.modules()
    async with ctx.tx() as s:
        order = await s.get(Order, order_id, with_for_update=True)
        if order is None:
            raise not_found("order")
        customer = await s.get(Customer, order.customer_id)
        if customer is None:
            raise not_found("customer")
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        closed = order.status in ("delivered", "cancelled")
        new_status = changes.pop("status", None)
        if closed and set(changes) - {"notes"}:
            raise conflict(f"a {order.status} order can only have its notes changed")
        if new_status is not None and new_status != order.status:
            if new_status not in TRANSITIONS[order.status]:
                raise conflict(
                    {
                        "code": "invalid_transition",
                        "from": order.status,
                        "to": new_status,
                        "allowed": sorted(TRANSITIONS[order.status]),
                    }
                )
            before["status"], after["status"] = order.status, new_status
            order.status = new_status
            if new_status == "cancelled":
                for give_back in cancel_hooks(enabled):
                    restored = await give_back(s, order)
                    if restored:
                        after["coupon_bottles_restored"] = restored
                locked = await s.get(Customer, order.customer_id, with_for_update=True)
                if locked is not None and locked.lifetime_orders:
                    locked.lifetime_orders -= 1
        for field_name, value in changes.items():
            current = getattr(order, field_name)
            if current != value:
                before[field_name] = current.isoformat() if isinstance(current, date) else current
                after[field_name] = value.isoformat() if isinstance(value, date) else value
                setattr(order, field_name, value)
        if after:
            audit(s, ctx, "update_order", "order", order.id, before=before, after=after)
        await s.flush()
        return _out(order, customer)

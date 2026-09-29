"""Order placement shared by the agent's create_order tool and the dashboard.

One implementation of the money rules, so a dashboard order and an agent order cannot disagree:
* every SKU exists, is active and priced for this tenant
* the total is recomputed from the products table; callers never supply a price
* prepaid redemption (e.g. coupon books) goes through the `order_redeem` extension point: the
  orders module does not know what a coupon is, the coupons module plugs in
* order_no is a per-tenant sequence from 1001, serialised with an advisory lock
* the customer's lifetime_orders / last_order_at are bumped

Runs inside the caller's tenant transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Customer, Order, Product
from api.modules.registry import Enabled

CENT = Decimal("0.01")


class OrderError(Exception):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Redemption:
    """What a prepaid module took: which SKUs it paid for, and where from."""

    covered_skus: frozenset[str]
    units: int
    source_id: str  # recorded on each covered line, so a cancellation can give it back
    remaining: int


# (session, customer_id, wanted sku→qty, products, today) → Redemption, or raise OrderError
RedeemHook = Callable[
    [AsyncSession, uuid.UUID, dict[str, int], dict[str, Product], date], Awaitable[Redemption]
]
# (session, cancelled order) → units given back
CancelHook = Callable[[AsyncSession, Order], Awaitable[int]]


def redeem_hook(enabled: Enabled) -> RedeemHook | None:
    return next((m.order_redeem for m in enabled.modules if m.order_redeem), None)


def cancel_hooks(enabled: Enabled) -> list[CancelHook]:
    return [m.order_cancelled for m in enabled.modules if m.order_cancelled]


@dataclass
class OrderRequest:
    customer_id: uuid.UUID
    items: dict[str, int]  # sku → qty (already merged)
    area: str | None
    source: str  # 'agent' | 'dashboard' | 'manual'
    created_by: str
    use_prepaid: bool = False
    status: str = "confirmed"
    delivery_date: date | None = None
    delivery_slot: str | None = None
    notes: str | None = None
    conversation_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Placed:
    order: Order
    lines: list[dict[str, Any]]
    redemption: Redemption | None


def money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(CENT))


async def load_products(s: AsyncSession, skus: list[str]) -> dict[str, Product]:
    products = {
        p.sku: p for p in (await s.scalars(select(Product).where(Product.sku.in_(skus)))).all()
    }
    unknown = sorted(sku for sku in skus if sku not in products or not products[sku].is_active)
    if unknown:
        raise OrderError("unknown_sku", skus=unknown)
    unpriced = sorted(sku for sku in skus if products[sku].price_aed is None)
    if unpriced:
        raise OrderError("product_unavailable", skus=unpriced)
    return products


async def next_order_no(s: AsyncSession, tenant_id: uuid.UUID) -> str:
    await s.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('order_no:' || :t))"), {"t": str(tenant_id)}
    )
    current = await s.scalar(
        select(
            func.max(text("CASE WHEN order_no ~ '^[0-9]+$' THEN order_no::bigint END"))
        ).select_from(Order)
    )
    return str(max(int(current or 1000), 1000) + 1)


async def place_order(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    req: OrderRequest,
    *,
    now: datetime,
    today: date,
    redeem: RedeemHook | None = None,
) -> Placed:
    if not req.items:
        raise OrderError("no_items")
    products = await load_products(s, list(req.items))

    redemption: Redemption | None = None
    if req.use_prepaid:
        if redeem is None:
            raise OrderError("prepaid_not_available")
        redemption = await redeem(s, req.customer_id, req.items, products, today)

    lines: list[dict[str, Any]] = []
    total = Decimal("0.00")
    for sku, qty in req.items.items():
        p = products[sku]
        unit = p.price_aed or Decimal(0)
        covered = redemption is not None and sku in redemption.covered_skus
        line_total = Decimal(0) if covered else unit * qty
        total += line_total
        line: dict[str, Any] = {
            "sku": sku,
            "name": p.name_en,
            "category": p.category,
            "qty": qty,
            "unit_price_aed": money(unit),
            "line_total_aed": money(line_total),
            "paid_with_coupon": covered,
        }
        if covered and redemption is not None:
            line["coupon_book_id"] = redemption.source_id
        lines.append(line)

    order = Order(
        customer_id=req.customer_id,
        order_no=await next_order_no(s, tenant_id),
        status=req.status,
        items=lines,
        total_aed=total.quantize(CENT),
        source=req.source,
        area=req.area,
        delivery_date=req.delivery_date,
        delivery_slot=req.delivery_slot,
        notes=req.notes,
        created_by=req.created_by,
        conversation_id=req.conversation_id,
        idempotency_key=req.idempotency_key,
    )
    s.add(order)
    customer = await s.get(Customer, req.customer_id, with_for_update=True)
    if customer is None:
        raise OrderError("unknown_customer")
    customer.lifetime_orders = (customer.lifetime_orders or 0) + 1
    customer.last_order_at = now
    if not customer.area and req.area:
        customer.area = req.area
    await s.flush()
    return Placed(order=order, lines=lines, redemption=redemption)

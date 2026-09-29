"""Order placement shared by the agent's create_order tool and the dashboard.

One implementation of the money rules, so a dashboard order and an agent order cannot disagree:
* every SKU exists, is active and priced for this tenant
* the total is recomputed from the products table; callers never supply a price
* coupon books redeem `water`-category bottles one-for-one (ASSUMPTION to confirm with the
  client — Phase 2 notes); the book used is recorded on each covered line so a cancellation can
  put the bottles back
* order_no is a per-tenant sequence from 1001, serialised with an advisory lock
* the customer's lifetime_orders / last_order_at are bumped

Runs inside the caller's tenant transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import CouponBook, Customer, Order, Product

CENT = Decimal("0.01")


class OrderError(Exception):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass
class OrderRequest:
    customer_id: uuid.UUID
    items: dict[str, int]  # sku → qty (already merged)
    area: str | None
    source: str  # 'agent' | 'dashboard' | 'manual'
    created_by: str
    use_coupon_book: bool = False
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
    bottles_from_coupon: int
    coupon_bottles_remaining: int | None


def money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(CENT))


async def live_coupon_books(
    s: AsyncSession, customer_id: uuid.UUID, today: date, *, lock: bool = False
) -> list[CouponBook]:
    stmt = (
        select(CouponBook)
        .where(
            CouponBook.customer_id == customer_id,
            CouponBook.bottles_remaining > 0,
            (CouponBook.expires_at.is_(None)) | (CouponBook.expires_at >= today),
        )
        .order_by(CouponBook.expires_at.asc().nulls_last())
    )
    if lock:
        stmt = stmt.with_for_update()
    return list((await s.scalars(stmt)).all())


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
    s: AsyncSession, tenant_id: uuid.UUID, req: OrderRequest, *, now: datetime, today: date
) -> Placed:
    if not req.items:
        raise OrderError("no_items")
    products = await load_products(s, list(req.items))

    water_qty = sum(q for sku, q in req.items.items() if products[sku].category == "water")
    book: CouponBook | None = None
    if req.use_coupon_book:
        if water_qty == 0:
            raise OrderError(
                "coupon_not_applicable", reason="coupon books cover water bottles only"
            )
        books = await live_coupon_books(s, req.customer_id, today, lock=True)
        book = next((b for b in books if (b.bottles_remaining or 0) >= water_qty), None)
        if book is None:
            raise OrderError(
                "insufficient_coupon_balance",
                bottles_requested=water_qty,
                bottles_remaining=sum(b.bottles_remaining or 0 for b in books),
                has_live_coupon_book=bool(books),
            )
        book.bottles_remaining = (book.bottles_remaining or 0) - water_qty

    lines: list[dict[str, Any]] = []
    total = Decimal("0.00")
    for sku, qty in req.items.items():
        p = products[sku]
        unit = p.price_aed or Decimal(0)
        covered = book is not None and p.category == "water"
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
        if covered and book is not None:
            line["coupon_book_id"] = str(book.id)
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
    return Placed(
        order=order,
        lines=lines,
        bottles_from_coupon=water_qty if book is not None else 0,
        coupon_bottles_remaining=book.bottles_remaining if book is not None else None,
    )


async def restore_coupon_bottles(s: AsyncSession, order: Order) -> int:
    """On cancellation: put coupon-paid bottles back into the book they came from."""
    restored = 0
    per_book: dict[uuid.UUID, int] = {}
    for line in order.items or []:
        if isinstance(line, dict) and line.get("paid_with_coupon") and line.get("coupon_book_id"):
            try:
                book_id = uuid.UUID(str(line["coupon_book_id"]))
            except ValueError:
                continue
            per_book[book_id] = per_book.get(book_id, 0) + int(line.get("qty") or 0)
    for book_id, qty in per_book.items():
        book = await s.get(CouponBook, book_id, with_for_update=True)
        if book is not None and book.customer_id == order.customer_id:
            book.bottles_remaining = (book.bottles_remaining or 0) + qty
            restored += qty
    return restored

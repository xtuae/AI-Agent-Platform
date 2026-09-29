"""Coupons: prepaid bottle books. Plugs into the orders module's extension points.

Coupon books redeem bottles of `water`-category products, one coupon per bottle (Aquamena sells
only 5-gallon cans, so one coupon = one can). Books are consumed earliest-expiry first.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import CouponBook, Customer, Order, Product
from api.modules.base import CustomerLines, Line
from api.modules.orders.service import OrderError, Redemption, money

if TYPE_CHECKING:
    from api.agents.tools.base import ToolContext

REDEEMABLE_CATEGORY = "water"


async def live_books(
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


async def redeem(
    s: AsyncSession,
    customer_id: uuid.UUID,
    wanted: dict[str, int],
    products: dict[str, Product],
    today: date,
) -> Redemption:
    covered = {sku for sku in wanted if products[sku].category == REDEEMABLE_CATEGORY}
    units = sum(wanted[sku] for sku in covered)
    if units == 0:
        raise OrderError("coupon_not_applicable", reason="coupon books cover water bottles only")
    books = await live_books(s, customer_id, today, lock=True)
    book = next((b for b in books if (b.bottles_remaining or 0) >= units), None)
    if book is None:
        raise OrderError(
            "insufficient_coupon_balance",
            bottles_requested=units,
            bottles_remaining=sum(b.bottles_remaining or 0 for b in books),
            has_live_coupon_book=bool(books),
        )
    book.bottles_remaining = (book.bottles_remaining or 0) - units
    return Redemption(
        covered_skus=frozenset(covered),
        units=units,
        source_id=str(book.id),
        remaining=book.bottles_remaining,
    )


async def restore(s: AsyncSession, order: Order) -> int:
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


async def customer_block(s: AsyncSession, customer: Customer, today: date) -> CustomerLines:
    books = await live_books(s, customer.id, today)
    if not books:
        return CustomerLines(known=False, lines=(Line(20, "Coupon book: none active"),))
    book = books[0]
    free = book.bottles_free or 0
    paid = (book.bottles_total or 0) - free
    price = f"AED {book.price_aed:.0f} package " if book.price_aed is not None else ""
    expiry = f", expires {book.expires_at.isoformat()}" if book.expires_at else ""
    return CustomerLines(
        known=False,
        lines=(
            Line(
                20,
                f"Coupon book: {price}({paid}+{free}) — "
                f"{book.bottles_remaining} bottles remaining{expiry}",
            ),
        ),
    )


async def customer_context(ctx: ToolContext) -> dict[str, Any]:
    books = await live_books(ctx.session, ctx.customer_id, ctx.today)
    return {
        "coupon_books": [
            {
                "package_price_aed": money(b.price_aed),
                "bottles_paid": (b.bottles_total or 0) - (b.bottles_free or 0),
                "bottles_free": b.bottles_free,
                "bottles_remaining": b.bottles_remaining,
                "expires_at": b.expires_at.isoformat() if b.expires_at else None,
            }
            for b in books
        ],
        "has_live_coupon_book": bool(books),
    }


async def contact_panel(s: AsyncSession, customer_id: uuid.UUID, today: date) -> dict[str, Any]:
    books = (
        await s.scalars(
            select(CouponBook)
            .where(CouponBook.customer_id == customer_id)
            .order_by(CouponBook.purchased_at.desc().nulls_last())
        )
    ).all()

    def live(b: CouponBook) -> bool:
        return (b.bottles_remaining or 0) > 0 and (b.expires_at is None or b.expires_at >= today)

    return {
        "bottles_remaining": sum(b.bottles_remaining or 0 for b in books if live(b)),
        "books": [
            {
                "id": str(b.id),
                "sku": b.sku,
                "bottles_total": b.bottles_total,
                "bottles_free": b.bottles_free,
                "bottles_remaining": b.bottles_remaining,
                "price_aed": money(b.price_aed),
                "purchased_at": b.purchased_at.isoformat() if b.purchased_at else None,
                "expires_at": b.expires_at.isoformat() if b.expires_at else None,
                "live": live(b),
            }
            for b in books
        ],
    }

"""get_customer_context — profile, live coupon balance, recent orders. Read-only."""

from __future__ import annotations

from sqlalchemy import select

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, error, money
from api.commerce import orders as commerce_orders
from api.db.models import CouponBook, Customer, Order

SCHEMA = {"type": "object", "properties": {}, "required": []}


class Args(ToolArgs):
    pass


async def live_coupon_books(ctx: ToolContext) -> list[CouponBook]:
    return await commerce_orders.live_coupon_books(ctx.session, ctx.customer_id, ctx.today)


def order_view(o: Order) -> dict[str, object]:
    return {
        "order_no": o.order_no,
        "status": o.status,
        "date": o.created_at.date().isoformat(),
        "items": [
            {
                "sku": i.get("sku"),
                "name": i.get("name"),
                "qty": i.get("qty"),
                "unit_price_aed": i.get("unit_price_aed"),
            }
            for i in (o.items or [])
        ],
        "total_aed": money(o.total_aed),
        "delivery_date": o.delivery_date.isoformat() if o.delivery_date else None,
        "delivery_slot": o.delivery_slot,
    }


async def run(ctx: ToolContext, _args: Args) -> ToolResult:
    customer = await ctx.session.get(Customer, ctx.customer_id)
    if customer is None:
        return error("customer_not_found")
    books = await live_coupon_books(ctx)
    orders = (
        await ctx.session.scalars(
            select(Order)
            .where(Order.customer_id == ctx.customer_id, Order.status != "cancelled")
            .order_by(Order.created_at.desc())
            .limit(3)
        )
    ).all()
    return {
        "customer": {
            "name": customer.name,
            "area": customer.area,
            "emirate": customer.emirate,
            "language": customer.language,
            "address_note": customer.address_note,
        },
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
        "recent_orders": [order_view(o) for o in orders],
        "lifetime_orders": customer.lifetime_orders,
        "opted_in_to_offers": customer.opt_in_status == "opted_in",
    }


TOOL = Tool(
    name="get_customer_context",
    description=(
        "Fetch this customer's profile, live coupon balance and recent orders. Call this before "
        "answering any question about their balance, their history, or a repeat order."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

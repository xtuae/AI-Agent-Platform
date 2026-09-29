"""get_order_status — one of THIS customer's orders. Never another customer's."""

from __future__ import annotations

from sqlalchemy import select

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, error, money
from api.db.models import Order


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


SCHEMA = {
    "type": "object",
    "properties": {"order_no": {"type": "string"}},
    "required": [],
}


class Args(ToolArgs):
    order_no: str | None = None


def normalise_order_no(raw: str | None) -> str | None:
    return raw.strip().lstrip("#").strip()[:32] if raw else None


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    stmt = select(Order).where(Order.customer_id == ctx.customer_id)
    order_no = normalise_order_no(args.order_no)
    if order_no:
        stmt = stmt.where(Order.order_no == order_no)
    order = await ctx.session.scalar(stmt.order_by(Order.created_at.desc()).limit(1))
    if order is None:
        return error("order_not_found", order_no=order_no)
    return order_view(order)


TOOL = Tool(
    name="get_order_status",
    description=(
        "Check the status of an existing order. Use the order reference if the customer gives one, "
        "otherwise their most recent order."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

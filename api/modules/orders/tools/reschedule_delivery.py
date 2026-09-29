"""reschedule_delivery — guard: refused once the order is out for delivery or later.

A refused reschedule is escalated automatically (scenario 11: "politely refused, escalated") —
the customer wants a change only the team can now make.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from api.agents.tools.base import (
    Tool,
    ToolArgs,
    ToolContext,
    ToolResult,
    audit,
    error,
    escalate_ctx,
)
from api.db.models import Order
from api.modules.orders.tools.get_order_status import normalise_order_no

LOCKED_STATUSES = frozenset({"out_for_delivery", "delivered"})

SCHEMA = {
    "type": "object",
    "properties": {
        "order_no": {"type": "string"},
        "new_date": {"type": "string"},
        "slot": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
    },
    "required": ["order_no", "new_date"],
}


class Args(ToolArgs):
    order_no: str = Field(min_length=1)
    new_date: str
    slot: Literal["morning", "afternoon", "evening"] | None = None


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    order_no = normalise_order_no(args.order_no)
    order = await ctx.session.scalar(
        select(Order)
        .where(Order.customer_id == ctx.customer_id, Order.order_no == order_no)
        .with_for_update()
    )
    if order is None:
        return error("order_not_found", order_no=order_no)
    if order.status in LOCKED_STATUSES:
        await escalate_ctx(
            ctx,
            reason="out_of_scope",
            summary=f"Customer asked to reschedule order {order.order_no}, which is already "
            f"{order.status.replace('_', ' ')}.",
        )
        return error("cannot_reschedule", status=order.status, escalated=True)
    if order.status == "cancelled":
        return error("order_cancelled", order_no=order.order_no)
    try:
        new_date = date.fromisoformat(args.new_date[:10])
    except ValueError:
        return error("invalid_date", expected="YYYY-MM-DD")
    if new_date < ctx.today:
        return error("date_in_past", today=ctx.today.isoformat())

    before = {
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "delivery_slot": order.delivery_slot,
    }
    order.delivery_date = new_date
    order.delivery_slot = args.slot or order.delivery_slot
    after = {"delivery_date": new_date.isoformat(), "delivery_slot": order.delivery_slot}
    await audit(ctx, "reschedule_delivery", "order", order.id, before=before, after=after)
    return {"order_no": order.order_no, "status": order.status, **after}


TOOL = Tool(
    name="reschedule_delivery",
    description=(
        "Change the delivery date or time slot of an order that has not yet gone out for delivery."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

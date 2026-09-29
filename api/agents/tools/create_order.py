"""create_order — the only tool that takes money. Guards from 02 §3.1, all enforced here:

* every SKU must exist and be active for this tenant — unknown SKU is an error, never a silent order
* qty above 200 for any SKU → escalate instead of creating
* the total is recomputed from the products table; the model never supplies a price or total
* use_coupon_book only with a live book holding enough bottles; otherwise the REAL balance is
  returned as an error for the agent to explain
* idempotent on (conversation_id, items) within 10 minutes — a retry cannot double-order
* one transaction (the tool round's), audit_log actor 'agent'

Coupon books redeem bottles of `water`-category products (one coupon = one bottle). ASSUMPTION
to confirm with the client — see the Phase 2 notes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

from pydantic import Field
from sqlalchemy import select, text

from api.agents.tools.base import (
    Tool,
    ToolArgs,
    ToolContext,
    ToolResult,
    audit,
    error,
    escalate_ctx,
    money,
)
from api.commerce.orders import OrderError, OrderRequest, load_products, place_order
from api.db.models import Order

MAX_QTY_PER_SKU = 200
IDEMPOTENCY_WINDOW = timedelta(minutes=10)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "qty": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["sku", "qty"],
            },
        },
        "use_coupon_book": {
            "type": "boolean",
            "description": (
                "Draw bottles from the customer's existing coupon book rather than charging"
            ),
        },
        "area": {"type": "string"},
        "delivery_date_preference": {"type": "string", "description": "ISO date, or 'asap'"},
        "notes": {"type": "string"},
    },
    "required": ["items", "area"],
}


class Item(ToolArgs):
    sku: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1)  # the 200 cap is a GUARD (escalate), not a validation error


class Args(ToolArgs):
    items: list[Item] = Field(min_length=1, max_length=20)
    use_coupon_book: bool = False
    area: str = Field(min_length=1, max_length=120)
    delivery_date_preference: str | None = None
    notes: str | None = Field(default=None, max_length=500)


def items_hash(items: dict[str, int]) -> str:
    return hashlib.sha256(json.dumps(sorted(items.items())).encode()).hexdigest()


def _delivery_date(pref: str | None, ctx: ToolContext) -> date | None:
    if pref and pref.lower() != "asap":
        try:
            d = date.fromisoformat(pref[:10])
        except ValueError:
            d = None
        if d is not None and d >= ctx.today:
            return d
    lead = (ctx.feature_flags.get("delivery") or {}).get("lead_days")
    return ctx.today + timedelta(days=int(lead)) if isinstance(lead, int) and lead >= 0 else None


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    wanted: dict[str, int] = {}
    for it in args.items:
        wanted[it.sku] = wanted.get(it.sku, 0) + it.qty

    # guard: quantity cap → escalate, never create
    over = {sku: q for sku, q in wanted.items() if q > MAX_QTY_PER_SKU}
    if over:
        await escalate_ctx(
            ctx,
            reason="out_of_scope",
            summary=f"Customer asked for a quantity above the per-item limit of {MAX_QTY_PER_SKU}: "
            + ", ".join(f"{q} x {sku}" for sku, q in over.items()),
        )
        return error("quantity_above_limit", limit=MAX_QTY_PER_SKU, escalated=True)

    # guard: SKUs exist, active, priced
    try:
        await load_products(ctx.session, list(wanted))
    except OrderError as exc:
        return _order_error(exc)

    # guard: idempotency (conversation, items) within 10 minutes
    key = items_hash(wanted)
    await ctx.session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"order:{ctx.conversation_id}:{key}"},
    )
    recent = await ctx.session.scalar(
        select(Order)
        .where(
            Order.conversation_id == ctx.conversation_id,
            Order.idempotency_key == key,
            Order.created_at > ctx.now - IDEMPOTENCY_WINDOW,
            Order.status != "cancelled",
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    if recent is not None:
        return {
            "order_no": recent.order_no,
            "status": recent.status,
            "total_aed": money(recent.total_aed),
            "duplicate": True,
            "note": "This order was already placed a moment ago; it was not placed twice.",
        }

    # coupon guard, server-side total, order number, customer stats — shared with the dashboard
    try:
        placed = await place_order(
            ctx.session,
            ctx.tenant_id,
            OrderRequest(
                customer_id=ctx.customer_id,
                items=wanted,
                area=args.area,
                source="agent",
                created_by="agent",
                use_coupon_book=args.use_coupon_book,
                delivery_date=_delivery_date(args.delivery_date_preference, ctx),
                notes=args.notes,
                conversation_id=ctx.conversation_id,
                idempotency_key=key,
            ),
            now=ctx.now,
            today=ctx.today,
        )
    except OrderError as exc:
        return _order_error(exc)
    order, lines = placed.order, placed.lines
    await audit(
        ctx,
        "create_order",
        "order",
        order.id,
        after={"order_no": order.order_no, "total_aed": money(order.total_aed), "items": lines},
    )

    hidden = {"category", "coupon_book_id"}
    result: ToolResult = {
        "order_no": order.order_no,
        "status": order.status,
        "items": [{k: v for k, v in ln.items() if k not in hidden} for ln in lines],
        "total_aed": money(order.total_aed),
        "area": order.area,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
    }
    if order.delivery_date is None:
        result["delivery_window"] = "to be confirmed by the team"
    if args.use_coupon_book:
        result["bottles_from_coupon"] = placed.bottles_from_coupon
        result["coupon_bottles_remaining"] = placed.coupon_bottles_remaining
    return result


def _order_error(exc: OrderError) -> ToolResult:
    if exc.code == "unknown_sku":
        return error(exc.code, **exc.details, hint="call get_products for valid SKUs")
    return error(exc.code, **exc.details)


TOOL = Tool(
    name="create_order",
    description=(
        "Place an order. Only call this AFTER the customer has confirmed the items, the total and "
        "the delivery area back to you."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

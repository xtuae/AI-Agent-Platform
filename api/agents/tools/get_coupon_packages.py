"""get_coupon_packages — coupon books on sale in an emirate. Read-only."""

from __future__ import annotations

from sqlalchemy import func, or_, select

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, money
from api.db.models import CouponPackage

SCHEMA = {
    "type": "object",
    "properties": {"emirate": {"type": "string"}},
    "required": ["emirate"],
}


class Args(ToolArgs):
    emirate: str = ""


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    stmt = select(CouponPackage).where(
        CouponPackage.is_active.is_(True),
        or_(
            CouponPackage.emirate.is_(None),
            func.lower(CouponPackage.emirate) == args.emirate.lower()[:40],
        ),
    )
    rows = (await ctx.session.scalars(stmt.order_by(CouponPackage.price_aed))).all()
    return {
        "packages": [
            {
                "sku": p.sku,
                "name_en": p.name_en,
                "name_ar": p.name_ar,
                "price_aed": money(p.price_aed),
                "bottles_paid": p.bottles_paid,
                "bottles_free": p.bottles_free,
                "validity_days": p.validity_days,
                "emirate": p.emirate,
            }
            for p in rows
        ],
        "found": len(rows),
    }


TOOL = Tool(
    name="get_coupon_packages",
    description=(
        "List the coupon book packages available in the customer's emirate, with prices and "
        "free-bottle counts."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

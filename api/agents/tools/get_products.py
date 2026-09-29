"""get_products — active products with current prices. Read-only."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import or_, select

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, money
from api.db.models import Product

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["water", "snack", "all"]},
        "query": {"type": "string", "description": "Optional name filter"},
    },
    "required": ["category"],
}


class Args(ToolArgs):
    category: Literal["water", "snack", "all"] = "all"
    query: str | None = None


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    stmt = select(Product).where(Product.is_active.is_(True), Product.price_aed.is_not(None))
    if args.category != "all":
        stmt = stmt.where(Product.category == args.category)
    if args.query:
        pattern = _like(args.query[:60])
        stmt = stmt.where(
            or_(
                Product.name_en.ilike(pattern, escape="\\"),
                Product.name_ar.ilike(pattern, escape="\\"),
                Product.sku.ilike(pattern, escape="\\"),
                Product.brand.ilike(pattern, escape="\\"),
            )
        )
    rows = (
        await ctx.session.scalars(stmt.order_by(Product.category, Product.name_en).limit(25))
    ).all()
    return {
        "products": [
            {
                "sku": p.sku,
                "name_en": p.name_en,
                "name_ar": p.name_ar,
                "category": p.category,
                "brand": p.brand,
                "price_aed": money(p.price_aed),
                "stock_note": p.stock_note,
            }
            for p in rows
        ],
        "found": len(rows),
    }


TOOL = Tool(
    name="get_products",
    description=(
        "List available products and current prices. Call before quoting any price. Never quote "
        "a price from memory."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

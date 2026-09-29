"""search_listings and get_listing — read-only, and only ever `available` listings. The agent may
say about a property only what these return."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import func, or_, select

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, error, money
from api.db.models import Listing
from api.modules.listings.models import PROPERTY_TYPES, PropertyType

MAX_RESULTS = 6
MAX_DESCRIPTION = 1500


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def summary(x: Listing) -> dict[str, Any]:
    return {
        "ref": x.ref,
        "title": x.title,
        "purpose": x.purpose,
        "property_type": x.property_type,
        "area": x.area,
        "community": x.community,
        "bedrooms": "studio" if x.bedrooms == 0 else x.bedrooms,
        "bathrooms": x.bathrooms,
        "size_sqft": x.size_sqft,
        "price_aed": money(x.price_aed),
        "rent_period": x.rent_period if x.purpose == "rent" else None,
        "viewings": x.viewings_enabled,
    }


SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "purpose": {"type": "string", "enum": ["sale", "rent"]},
        "area": {"type": "string", "description": "Area or community the customer asked for"},
        "bedrooms": {"type": "integer", "minimum": 0, "description": "0 = studio"},
        "max_price_aed": {
            "type": "number",
            "description": "The customer's budget, if they gave one",
        },
        "property_type": {"type": "string", "enum": list(PROPERTY_TYPES)},
    },
    "required": ["purpose"],
}


class SearchArgs(ToolArgs):
    purpose: Literal["sale", "rent"]
    area: str | None = Field(default=None, max_length=120)
    bedrooms: int | None = Field(default=None, ge=0, le=20)
    max_price_aed: Decimal | None = Field(default=None, ge=0)
    property_type: PropertyType | None = None


async def search(ctx: ToolContext, args: SearchArgs) -> ToolResult:
    stmt = select(Listing).where(Listing.status == "available", Listing.purpose == args.purpose)
    if args.area:
        pattern = _like(args.area[:120])
        stmt = stmt.where(
            or_(
                Listing.area.ilike(pattern, escape="\\"),
                Listing.community.ilike(pattern, escape="\\"),
            )
        )
    if args.bedrooms is not None:
        stmt = stmt.where(Listing.bedrooms == args.bedrooms)
    if args.max_price_aed is not None:
        stmt = stmt.where(Listing.price_aed <= args.max_price_aed)
    if args.property_type:
        stmt = stmt.where(Listing.property_type == args.property_type)
    total = await ctx.session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await ctx.session.scalars(stmt.order_by(Listing.price_aed, Listing.ref).limit(MAX_RESULTS))
    ).all()
    result: ToolResult = {"listings": [summary(x) for x in rows], "found": int(total or 0)}
    if (total or 0) > len(rows):
        result["note"] = f"Showing the {len(rows)} lowest priced; ask them to narrow the search."
    return result


GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ref": {"type": "string", "description": "The listing reference"}},
    "required": ["ref"],
}


class GetArgs(ToolArgs):
    ref: str = Field(min_length=1, max_length=64)


async def get(ctx: ToolContext, args: GetArgs) -> ToolResult:
    x = await ctx.session.scalar(
        select(Listing).where(
            func.lower(Listing.ref) == args.ref.strip().lstrip("#").lower(),
            Listing.status == "available",
        )
    )
    if x is None:
        return error("listing_not_available", ref=args.ref)
    return {
        **summary(x),
        "description": (x.description or "")[:MAX_DESCRIPTION] or None,
        "address_note": x.address_note,
    }


SEARCH_TOOL = Tool(
    name="search_listings",
    description=(
        "Search the available properties. Call before describing or suggesting any property. "
        "Never mention a property or a price that did not come from this or get_listing."
    ),
    parameters=SEARCH_SCHEMA,
    args_model=SearchArgs,
    run=search,
)

GET_TOOL = Tool(
    name="get_listing",
    description="Full details of one available property, by its reference.",
    parameters=GET_SCHEMA,
    args_model=GetArgs,
    run=get,
)

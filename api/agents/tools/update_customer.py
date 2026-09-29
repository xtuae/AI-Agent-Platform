"""update_customer — the customer's own name / area / emirate / address note."""

from __future__ import annotations

from api.agents.text import clean_inline
from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, audit, error
from api.db.models import Customer

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "area": {"type": "string"},
        "emirate": {"type": "string"},
        "address_note": {"type": "string"},
    },
    "required": [],
}

_LIMITS = {"name": 80, "area": 80, "emirate": 40, "address_note": 300}


class Args(ToolArgs):
    name: str | None = None
    area: str | None = None
    emirate: str | None = None
    address_note: str | None = None


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    customer = await ctx.session.get(Customer, ctx.customer_id, with_for_update=True)
    if customer is None:
        return error("customer_not_found")
    before: dict[str, object] = {}
    after: dict[str, object] = {}
    for field, limit in _LIMITS.items():
        value = clean_inline(getattr(args, field), limit)
        if value:
            before[field] = getattr(customer, field)
            setattr(customer, field, value)
            after[field] = value
    if not after:
        return error("nothing_to_update")
    await audit(ctx, "update_customer", "customer", customer.id, before=before, after=after)
    return {"updated": sorted(after), "customer": {k: getattr(customer, k) for k in _LIMITS}}


TOOL = Tool(
    name="update_customer",
    description=(
        "Correct or add the customer's name, area or delivery address when they tell you it."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

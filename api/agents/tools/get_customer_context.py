"""get_customer_context — profile plus whatever the tenant's modules know. Read-only.

Core returns the profile and opt-in status; each enabled module adds its own section through its
`customer_context` hook (coupons: live books; orders: recent orders). The description the model
sees is composed the same way, so it never mentions a capability the tenant does not have.
"""

from __future__ import annotations

from dataclasses import replace

from api.agents.prompts.compose import context_description
from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, error
from api.db.models import Customer
from api.modules.base import Module

SCHEMA = {"type": "object", "properties": {}, "required": []}


class Args(ToolArgs):
    pass


async def run(ctx: ToolContext, _args: Args) -> ToolResult:
    customer = await ctx.session.get(Customer, ctx.customer_id)
    if customer is None:
        return error("customer_not_found")
    result: ToolResult = {
        "customer": {
            "name": customer.name,
            "area": customer.area,
            "emirate": customer.emirate,
            "language": customer.language,
            "address_note": customer.address_note,
        },
    }
    for module in ctx.enabled.modules:
        if module.customer_context is not None:
            result.update(await module.customer_context(ctx))
    result["opted_in_to_offers"] = customer.opt_in_status == "opted_in"
    return result


TOOL = Tool(
    name="get_customer_context",
    description=context_description(()),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)


def for_modules(modules: tuple[Module, ...]) -> Tool:
    return replace(TOOL, description=context_description(modules))

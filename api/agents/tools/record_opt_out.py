"""record_opt_out — NOT in 02 §3's list of nine; added because 02 §4.2 instructs the model to
call it, and because a model-side path is defence in depth behind the deterministic STOP check
and the classifier. Opt-out only stops marketing; the customer can still order and get help.
"""

from __future__ import annotations

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, audit, error
from api.db.models import Customer

SCHEMA = {"type": "object", "properties": {}, "required": []}


class Args(ToolArgs):
    pass


async def mark_opted_out(ctx: ToolContext, source: str) -> bool:
    """Returns True if the status changed. Shared with the deterministic STOP path."""
    customer = await ctx.session.get(Customer, ctx.customer_id, with_for_update=True)
    if customer is None:
        return False
    if customer.opt_in_status == "opted_out":
        return False
    before = {"opt_in_status": customer.opt_in_status}
    customer.opt_in_status = "opted_out"
    customer.opt_out_at = ctx.now
    await audit(
        ctx,
        "record_opt_out",
        "customer",
        customer.id,
        before=before,
        after={"opt_in_status": "opted_out", "source": source, "wamid": ctx.inbound_wamid},
    )
    return True


async def run(ctx: ToolContext, _args: Args) -> ToolResult:
    customer = await ctx.session.get(Customer, ctx.customer_id)
    if customer is None:
        return error("customer_not_found")
    await mark_opted_out(ctx, "agent_tool")
    return {"opted_out": True}


TOOL = Tool(
    name="record_opt_out",
    description=(
        "Record that the customer no longer wants offers or campaign messages. Call this whenever "
        "they ask to stop receiving them."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

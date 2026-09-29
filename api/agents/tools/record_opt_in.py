"""record_opt_in — consent to offers, with PDPL/TDRA evidence.

Guard: rejected if the customer has opted out. A re-opt-in must be a deliberate human action.
"""

from __future__ import annotations

from pydantic import Field

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, audit, error
from api.db.models import Customer

SCHEMA = {
    "type": "object",
    "properties": {
        "wording_shown": {
            "type": "string",
            "description": "The exact consent wording the customer responded to",
        }
    },
    "required": ["wording_shown"],
}


class Args(ToolArgs):
    wording_shown: str = Field(min_length=5, max_length=1000)


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    customer = await ctx.session.get(Customer, ctx.customer_id, with_for_update=True)
    if customer is None:
        return error("customer_not_found")
    if customer.opt_in_status == "opted_out":
        return error(
            "opted_out_requires_human",
            message="This customer opted out earlier; only the team can re-subscribe them.",
        )
    if customer.opt_in_status == "opted_in":
        return {"opted_in": True, "already": True}
    evidence = {
        "source": "whatsapp_agent",
        "wording_shown": args.wording_shown,
        "at": ctx.now.isoformat(),
        "wamid": ctx.inbound_wamid,
        "conversation_id": str(ctx.conversation_id),
    }
    before = {"opt_in_status": customer.opt_in_status}
    customer.opt_in_status = "opted_in"
    customer.opt_in_at = ctx.now
    customer.opt_in_evidence = evidence
    await audit(
        ctx,
        "record_opt_in",
        "customer",
        customer.id,
        before=before,
        after={"opt_in_status": "opted_in", "evidence": evidence},
    )
    return {"opted_in": True}


TOOL = Tool(
    name="record_opt_in",
    description=(
        "Record that the customer has agreed to receive offers and campaign messages. Only call "
        "this when they have clearly said yes."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

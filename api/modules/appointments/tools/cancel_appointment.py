"""cancel_appointment — cancel one of THIS customer's appointments. Inside `cancel_cutoff_hours`
of the start it is escalated to the team instead."""

from __future__ import annotations

from pydantic import Field

from api.agents.tools.base import (
    Tool,
    ToolArgs,
    ToolContext,
    ToolResult,
    audit,
    error,
    escalate_ctx,
    tenant_zone,
)
from api.modules.appointments.models import LIVE
from api.modules.appointments.service import inside_cutoff
from api.modules.appointments.tools.common import config, own, view

SCHEMA = {
    "type": "object",
    "properties": {"ref": {"type": "string", "description": "The appointment reference"}},
    "required": ["ref"],
}


class Args(ToolArgs):
    ref: str = Field(min_length=1, max_length=40)


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    cfg = config(ctx)
    found = await own(ctx, args.ref)
    if found is None:
        return error("appointment_not_found", ref=args.ref)
    appt, type_ = found
    if appt.status not in LIVE:
        return error("appointment_not_active", status=appt.status)
    if inside_cutoff(appt, ctx.now, cfg):
        await escalate_ctx(
            ctx,
            reason="out_of_scope",
            summary=f"Customer asked to cancel appointment {appt.ref} ({type_.name_en}), which "
            f"starts within {cfg.cancel_cutoff_hours} hours.",
        )
        return error("inside_cutoff", cutoff_hours=cfg.cancel_cutoff_hours, escalated=True)
    before = appt.status
    appt.status = "cancelled"
    await ctx.session.flush()
    await audit(
        ctx,
        "cancel_appointment",
        "appointment",
        appt.id,
        before={"status": before},
        after={"status": "cancelled"},
    )
    return view(appt, type_, tenant_zone(ctx))


TOOL = Tool(
    name="cancel_appointment",
    description=(
        "Cancel one of the customer's appointments, after they have confirmed they want to."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

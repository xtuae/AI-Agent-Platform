"""reschedule_appointment — move one of THIS customer's appointments to a time get_availability
offered. Inside `cancel_cutoff_hours` of the start the change is the team's call: escalated.
With `require_team_confirmation` on, a moved booking goes back to `requested`."""

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
from api.modules.appointments.service import (
    BookingError,
    inside_cutoff,
    iso_local,
    move,
    parse_start,
)
from api.modules.appointments.tools.common import config, own, view

SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "The appointment reference, e.g. A-1042"},
        "new_starts_at": {
            "type": "string",
            "description": "Exactly a starts_at returned by get_availability",
        },
    },
    "required": ["ref", "new_starts_at"],
}


class Args(ToolArgs):
    ref: str = Field(min_length=1, max_length=40)
    new_starts_at: str = Field(min_length=10, max_length=40)


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    tz = tenant_zone(ctx)
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
            summary=f"Customer asked to move appointment {appt.ref} ({type_.name_en}), which "
            f"starts within {cfg.cancel_cutoff_hours} hours.",
        )
        return error("inside_cutoff", cutoff_hours=cfg.cancel_cutoff_hours, escalated=True)
    try:
        new_start = parse_start(args.new_starts_at, tz)
    except ValueError:
        return error("invalid_starts_at", expected="YYYY-MM-DDTHH:MM from get_availability")
    before = {"starts_at": iso_local(appt.starts_at, tz), "status": appt.status}
    try:
        await move(
            ctx.session, appt, type_, new_start, tz=tz, now=ctx.now, config=cfg, only_offered=True
        )
    except BookingError as exc:
        return error(exc.code, **exc.details)
    if cfg.require_team_confirmation:
        appt.status = "requested"
    await ctx.session.flush()
    await audit(
        ctx,
        "reschedule_appointment",
        "appointment",
        appt.id,
        before=before,
        after={"starts_at": iso_local(appt.starts_at, tz), "status": appt.status},
    )
    return view(appt, type_, tz)


TOOL = Tool(
    name="reschedule_appointment",
    description=(
        "Move one of the customer's appointments to a new time from get_availability. Only after "
        "they have agreed to the new time."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

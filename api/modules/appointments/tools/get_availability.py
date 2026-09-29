"""get_availability — free start times for one appointment type. The ONLY source of times the
agent may offer. Without a type (or with an unknown one) it returns the types to choose from."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import Field

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, error, tenant_zone
from api.modules.appointments.service import (
    active_types,
    find_type,
    free_slots,
    iso_local,
    spread,
    type_view,
    when,
    window,
)
from api.modules.appointments.tools.common import config

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "description": "Appointment type id or name. Leave out to list the types.",
        },
        "from_date": {"type": "string", "description": "ISO date; default today"},
        "to_date": {"type": "string", "description": "ISO date; default a week after from_date"},
    },
    "required": [],
}


class Args(ToolArgs):
    type: str | None = Field(default=None, max_length=120)
    from_date: str | None = None
    to_date: str | None = None


def _day(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    type_ = await find_type(ctx.session, args.type)
    if type_ is None:
        types = [type_view(t) for t in await active_types(ctx.session)]
        if not types:
            return error("no_appointment_types", hint="hand over: nothing can be booked yet")
        if args.type:
            return error("unknown_type", types=types)
        return {"types": types, "next_step": "Ask which one they need, then call again with it."}

    tz = tenant_zone(ctx)
    cfg = config(ctx)
    _, last_day = window(cfg, ctx.now, ctx.today)
    day_from = max(_day(args.from_date) or ctx.today, ctx.today)
    day_to = _day(args.to_date) or day_from + timedelta(days=6)
    slots = await free_slots(
        ctx.session, type_=type_, day_from=day_from, day_to=day_to, tz=tz, now=ctx.now, config=cfg
    )
    widened = False
    if not slots and day_to < last_day:  # nothing in the asked range: look further ahead
        slots = await free_slots(
            ctx.session,
            type_=type_,
            day_from=day_to + timedelta(days=1),
            day_to=last_day,
            tz=tz,
            now=ctx.now,
            config=cfg,
        )
        widened = bool(slots)
    offered = spread(slots, tz)
    result: ToolResult = {
        "type": type_view(type_),
        "slots": [
            {"starts_at": iso_local(s.starts_at, tz), "when": when(s.starts_at, tz)}
            for s in offered
        ],
        "timezone": ctx.timezone,
    }
    if widened:
        result["note"] = "Nothing free in the dates asked; these are the next free times."
    if not offered:
        result["note"] = (
            f"Nothing free in the next {cfg.horizon_days} days. Offer to have the team call them."
        )
    return result


TOOL = Tool(
    name="get_availability",
    description=(
        "Free appointment times for one appointment type. Call before offering any time, and offer "
        "only times it returns. Without a type it lists the appointment types."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

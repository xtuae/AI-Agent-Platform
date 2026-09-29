"""book_appointment — book a time the customer agreed to. Guards (04 §3):

* the start must be one get_availability would offer right now (not in the past, not inside the
  notice period, inside working hours, free) — the database's overlap guard has the final word
* idempotent per conversation: the same type and start again returns the existing booking
* `require_team_confirmation` → the booking lands as `requested`
* other modules add parameters (listings → listing_ref) through the `appointment_subject`
  extension point; this tool passes them the extra arguments and records what the booking is about
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field
from sqlalchemy import select

from api.agents.tools.base import (
    Tool,
    ToolArgs,
    ToolContext,
    ToolResult,
    audit,
    error,
    tenant_zone,
)
from api.db.models import Appointment
from api.modules.appointments.models import LIVE
from api.modules.appointments.service import (
    BookingError,
    BookingRequest,
    active_types,
    book,
    find_type,
    parse_start,
    resolve_subject,
    type_view,
)
from api.modules.appointments.tools.common import config, view

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "description": "Appointment type id or name"},
        "starts_at": {
            "type": "string",
            "description": "Exactly a starts_at returned by get_availability",
        },
        "notes": {"type": "string"},
    },
    "required": ["type", "starts_at"],
}


class Args(ToolArgs):
    # extra parameters are kept: other modules add them (see tool_params) and read them
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    type: str = Field(min_length=1, max_length=120)
    starts_at: str = Field(min_length=10, max_length=40)
    notes: str | None = Field(default=None, max_length=500)


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    tz = tenant_zone(ctx)
    cfg = config(ctx)
    type_ = await find_type(ctx.session, args.type)
    if type_ is None:
        return error("unknown_type", types=[type_view(t) for t in await active_types(ctx.session)])
    try:
        starts_at = parse_start(args.starts_at, tz)
    except ValueError:
        return error("invalid_starts_at", expected="YYYY-MM-DDTHH:MM from get_availability")

    existing = await ctx.session.scalar(
        select(Appointment).where(
            Appointment.conversation_id == ctx.conversation_id,
            Appointment.customer_id == ctx.customer_id,
            Appointment.type_id == type_.id,
            Appointment.starts_at == starts_at,
            Appointment.status.in_(LIVE),
        )
    )
    if existing is not None:
        return {
            **view(existing, type_, tz),
            "duplicate": True,
            "note": "This booking was already made a moment ago; it was not made twice.",
        }

    try:
        subject = await resolve_subject(ctx.session, ctx.enabled, dict(args.model_extra or {}))
        appt = await book(
            ctx.session,
            ctx.tenant_id,
            BookingRequest(
                customer_id=ctx.customer_id,
                type_=type_,
                starts_at=starts_at,
                source="agent",
                created_by="agent",
                status="requested" if cfg.require_team_confirmation else "confirmed",
                subject=subject,
                notes=args.notes,
                conversation_id=ctx.conversation_id,
            ),
            tz=tz,
            now=ctx.now,
            config=cfg,
            only_offered=True,
        )
    except BookingError as exc:
        return error(exc.code, **exc.details)
    await audit(
        ctx,
        "book_appointment",
        "appointment",
        appt.id,
        after={"ref": appt.ref, "starts_at": appt.starts_at.isoformat(), "status": appt.status},
    )
    return view(appt, type_, tz)


TOOL = Tool(
    name="book_appointment",
    description=(
        "Book an appointment. Only call this AFTER the customer has agreed to the type, date, time "
        "and place you said back to them."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

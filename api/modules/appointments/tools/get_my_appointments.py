"""get_my_appointments — THIS customer's upcoming appointments. Never another customer's."""

from __future__ import annotations

from sqlalchemy import select

from api.agents.tools.base import Tool, ToolArgs, ToolContext, ToolResult, tenant_zone
from api.db.models import Appointment, AppointmentType
from api.modules.appointments.models import LIVE
from api.modules.appointments.tools.common import view

SCHEMA = {"type": "object", "properties": {}, "required": []}


class Args(ToolArgs):
    pass


async def run(ctx: ToolContext, _args: Args) -> ToolResult:
    tz = tenant_zone(ctx)
    rows = (
        await ctx.session.execute(
            select(Appointment, AppointmentType)
            .join(AppointmentType, AppointmentType.id == Appointment.type_id)
            .where(
                Appointment.customer_id == ctx.customer_id,
                Appointment.status.in_(LIVE),
                Appointment.ends_at > ctx.now,
            )
            .order_by(Appointment.starts_at)
            .limit(10)
        )
    ).all()
    return {"appointments": [view(a, t, tz) for a, t in rows], "found": len(rows)}


TOOL = Tool(
    name="get_my_appointments",
    description="This customer's upcoming appointments, with their references.",
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

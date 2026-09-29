"""Shared by the appointment tools: config, the customer-facing view of an appointment."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from api.agents.tools.base import ToolContext
from api.db.models import Appointment, AppointmentType
from api.modules.appointments.config import AppointmentsConfig, config_of
from api.modules.appointments.service import iso_local, when


def config(ctx: ToolContext) -> AppointmentsConfig:
    return config_of(ctx.enabled.config("appointments"))


def view(a: Appointment, t: AppointmentType | None, tz: ZoneInfo) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ref": a.ref,
        "type": t.name_en if t else None,
        "type_ar": t.name_ar if t else None,
        "starts_at": iso_local(a.starts_at, tz),
        "when": when(a.starts_at, tz),
        "duration_min": t.duration_min if t else None,
        "location": t.location_kind if t else None,
        "location_note": a.location_note,
        "status": a.status,
    }
    if a.subject_label:
        out["about"] = a.subject_label
    if a.status == "requested":
        out["note"] = "Requested — the team will confirm it."
    return out


def normalise_ref(raw: str | None) -> str | None:
    if not raw:
        return None
    ref = raw.strip().lstrip("#").strip().upper()[:32]
    return ref if ref.startswith("A-") else f"A-{ref}"


async def own(ctx: ToolContext, ref: str | None) -> tuple[Appointment, AppointmentType] | None:
    """THIS customer's appointment by reference (locked for update)."""
    code = normalise_ref(ref)
    if code is None:
        return None
    row = (
        await ctx.session.execute(
            select(Appointment, AppointmentType)
            .join(AppointmentType, AppointmentType.id == Appointment.type_id)
            .where(Appointment.customer_id == ctx.customer_id, Appointment.ref == code)
            .with_for_update(of=Appointment)
        )
    ).first()
    return (row[0], row[1]) if row else None

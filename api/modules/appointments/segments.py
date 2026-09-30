"""Segment fields the appointments module adds to campaigns."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import ColumnElement, and_, exists, select

from api.db.models import Appointment, Customer
from api.modules.base import SegmentField, SegmentScope


def _not_seen(days: int, scope: SegmentScope) -> ColumnElement[bool]:
    """Came to an appointment before, and has had none (nor has one booked) in `days` days."""
    return and_(
        exists(
            select(Appointment.id).where(
                Appointment.customer_id == Customer.id, Appointment.status == "completed"
            )
        ),
        ~exists(
            select(Appointment.id).where(
                Appointment.customer_id == Customer.id,
                Appointment.status.in_(("requested", "confirmed", "completed")),
                Appointment.starts_at >= scope.now - timedelta(days=days),
            )
        ),
    )


FIELDS = (
    SegmentField(
        name="last_appointment_before_days",
        label="Last appointment more than … days ago",
        kind="int",
        help="Past clients with nothing booked since",
        minimum=1,
        maximum=3650,
        clause=_not_seen,
    ),
)

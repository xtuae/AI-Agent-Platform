"""Availability and booking, shared by the agent's tools and the dashboard.

* Free slots come from each resource's weekly hours, minus time off, minus live appointments (and
  the configured buffer around them), on a `slot_minutes` grid, between `min_notice_hours` from now
  and `horizon_days` ahead. The customer never picks a resource: a slot is free if ANY resource is.
* The agent may only book a start time this module would offer right now; the dashboard may book
  any time (a person knows what they are doing), and then only the database's overlap guard
  applies.
* The overlap guard (migration 0006 trigger) is the final word under a race. A booking that loses
  the race tries the next free resource, inside a savepoint, and otherwise fails with slot_taken.
* What the appointment is about (a listing, …) comes from the `appointment_subject` extension point.

Runs inside the caller's tenant transaction. All datetimes are timezone-aware.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import (
    Appointment,
    AppointmentResource,
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
    Customer,
)
from api.modules.appointments.config import AppointmentsConfig
from api.modules.appointments.models import LIVE
from api.modules.base import Subject, SubjectError, SubjectHook
from api.modules.registry import Enabled

EXCLUSION_VIOLATION = "23P01"
MAX_OFFERED = 8
PER_DAY = 3


class BookingError(Exception):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Slot:
    starts_at: datetime  # UTC
    ends_at: datetime
    resource_ids: tuple[uuid.UUID, ...]  # free resources, in preference order


Interval = tuple[datetime, datetime]


def _overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def when(dt: datetime, tz: ZoneInfo) -> str:
    """'Fri 2 Oct, 10:00' in the tenant's time — what the agent reads back to the customer."""
    d = dt.astimezone(tz)
    return f"{d:%a} {d.day} {d:%b}, {d:%H:%M}"


def iso_local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def parse_start(raw: str, tz: ZoneInfo) -> datetime:
    """An ISO date-time from the model or the dashboard. No offset → the tenant's local time."""
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def _at(d: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, t, tz).astimezone(UTC)


# ---------------------------------------------------------------- lookups


async def active_types(s: AsyncSession) -> Sequence[AppointmentType]:
    return (
        await s.scalars(
            select(AppointmentType)
            .where(AppointmentType.is_active.is_(True))
            .order_by(AppointmentType.name_en)
        )
    ).all()


async def find_type(s: AsyncSession, ref: str | None) -> AppointmentType | None:
    """An active type by id or by its English or Arabic name (case-insensitive)."""
    if not ref or not ref.strip():
        return None
    ref = ref.strip()
    stmt = select(AppointmentType).where(AppointmentType.is_active.is_(True))
    try:
        by_id = uuid.UUID(ref)
    except ValueError:
        by_id = None
    found: AppointmentType | None
    if by_id is not None:
        found = await s.scalar(stmt.where(AppointmentType.id == by_id))
        return found
    found = await s.scalar(
        stmt.where(
            or_(
                func.lower(AppointmentType.name_en) == ref.lower(),
                func.lower(AppointmentType.name_ar) == ref.lower(),
            )
        ).limit(1)
    )
    return found


def type_view(t: AppointmentType) -> dict[str, Any]:
    return {
        "type_id": str(t.id),
        "name_en": t.name_en,
        "name_ar": t.name_ar,
        "duration_min": t.duration_min,
        "location": t.location_kind,
    }


async def _resources(
    s: AsyncSession, only: Sequence[uuid.UUID] | None = None
) -> list[AppointmentResource]:
    stmt = select(AppointmentResource).where(AppointmentResource.is_active.is_(True))
    if only is not None:
        stmt = stmt.where(AppointmentResource.id.in_(only))
    return list((await s.scalars(stmt.order_by(AppointmentResource.name))).all())


# ---------------------------------------------------------------- availability


def window(config: AppointmentsConfig, now: datetime, today: date) -> tuple[datetime, date]:
    """(earliest start, last bookable local day)."""
    return now + timedelta(hours=config.min_notice_hours), today + timedelta(
        days=config.horizon_days
    )


async def free_slots(
    s: AsyncSession,
    *,
    type_: AppointmentType,
    day_from: date,
    day_to: date,
    tz: ZoneInfo,
    now: datetime,
    config: AppointmentsConfig,
    resource_ids: Sequence[uuid.UUID] | None = None,
    ignore: uuid.UUID | None = None,
) -> list[Slot]:
    today = now.astimezone(tz).date()
    earliest, last_day = window(config, now, today)
    day_from, day_to = max(day_from, today), min(day_to, last_day)
    if day_to < day_from:
        return []
    resources = await _resources(s, resource_ids)
    if not resources:
        return []
    rids = [r.id for r in resources]
    rules: dict[tuple[uuid.UUID, int], list[AvailabilityRule]] = {}
    for rule in (
        await s.scalars(
            select(AvailabilityRule)
            .where(AvailabilityRule.resource_id.in_(rids))
            .order_by(AvailabilityRule.start_time)
        )
    ).all():
        rules.setdefault((rule.resource_id, rule.weekday), []).append(rule)
    exceptions = (
        await s.scalars(
            select(AvailabilityException).where(
                AvailabilityException.day >= day_from,
                AvailabilityException.day <= day_to,
                or_(
                    AvailabilityException.resource_id.is_(None),
                    AvailabilityException.resource_id.in_(rids),
                ),
            )
        )
    ).all()
    span_start = _at(day_from, time.min, tz) - timedelta(days=1)
    span_end = _at(day_to, time.min, tz) + timedelta(days=2)
    busy_stmt = select(Appointment.resource_id, Appointment.starts_at, Appointment.ends_at).where(
        Appointment.resource_id.in_(rids),
        Appointment.status.in_(LIVE),
        Appointment.starts_at < span_end,
        Appointment.ends_at > span_start,
    )
    if ignore is not None:
        busy_stmt = busy_stmt.where(Appointment.id != ignore)
    pad = timedelta(minutes=config.buffer_minutes)
    busy: dict[uuid.UUID, list[Interval]] = {}
    for rid, b_start, b_end in (await s.execute(busy_stmt)).all():
        busy.setdefault(rid, []).append((b_start - pad, b_end + pad))

    duration = timedelta(minutes=type_.duration_min)
    step = timedelta(minutes=config.slot_minutes)
    found: dict[datetime, list[uuid.UUID]] = {}
    d = day_from
    while d <= day_to:
        for r in resources:
            day_off = [e for e in exceptions if e.day == d and e.resource_id in (None, r.id)]
            if any(e.start_time is None for e in day_off):
                continue
            blocked: list[Interval] = [
                (_at(d, e.start_time, tz), _at(d, e.end_time, tz))
                for e in day_off
                if e.start_time is not None and e.end_time is not None
            ] + busy.get(r.id, [])
            for rule in rules.get((r.id, d.weekday()), []):
                t, close = _at(d, rule.start_time, tz), _at(d, rule.end_time, tz)
                while t + duration <= close:
                    candidate = (t, t + duration)
                    if t >= earliest and not any(_overlaps(candidate, b) for b in blocked):
                        free = found.setdefault(t, [])
                        if r.id not in free:
                            free.append(r.id)
                    t += step
        d += timedelta(days=1)
    return [
        Slot(starts_at=t, ends_at=t + duration, resource_ids=tuple(found[t])) for t in sorted(found)
    ]


def spread(slots: Sequence[Slot], tz: ZoneInfo, limit: int = MAX_OFFERED) -> list[Slot]:
    """At most `limit` slots, no more than PER_DAY on one day, so the customer sees a choice of days
    rather than eight times on the first morning."""
    per_day: dict[date, int] = {}
    out: list[Slot] = []
    for slot in slots:
        day = slot.starts_at.astimezone(tz).date()
        if per_day.get(day, 0) >= PER_DAY:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        out.append(slot)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- subjects


def subject_hooks(enabled: Enabled) -> list[SubjectHook]:
    return [m.appointment_subject for m in enabled.modules if m.appointment_subject]


async def resolve_subject(
    s: AsyncSession, enabled: Enabled, params: dict[str, Any]
) -> Subject | None:
    for hook in subject_hooks(enabled):
        try:
            subject = await hook(s, params)
        except SubjectError as exc:
            raise BookingError(exc.code, **exc.details) from exc
        if subject is not None:
            return subject
    return None


# ---------------------------------------------------------------- booking


async def next_ref(s: AsyncSession, tenant_id: uuid.UUID) -> str:
    await s.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('appointment_ref:' || :t))"),
        {"t": str(tenant_id)},
    )
    current = await s.scalar(
        select(
            func.max(text("CASE WHEN ref ~ '^A-[0-9]+$' THEN substr(ref, 3)::bigint END"))
        ).select_from(Appointment)
    )
    return f"A-{max(int(current or 1000), 1000) + 1}"


def _is_overlap(exc: IntegrityError) -> bool:
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == EXCLUSION_VIOLATION or "appointment_overlap" in str(orig)


@dataclass
class BookingRequest:
    customer_id: uuid.UUID
    type_: AppointmentType
    starts_at: datetime
    source: str  # 'agent' | 'dashboard'
    created_by: str
    status: str = "confirmed"
    resource_id: uuid.UUID | None = None  # dashboard only; the agent never picks
    subject: Subject | None = None
    location_note: str | None = None
    notes: str | None = None
    conversation_id: uuid.UUID | None = None


async def _location(s: AsyncSession, req: BookingRequest) -> str | None:
    if req.location_note:
        return req.location_note
    if req.subject is not None and req.subject.location_note:
        return req.subject.location_note
    if req.type_.needs_address:
        customer = await s.get(Customer, req.customer_id)
        where = (
            ", ".join(x for x in (customer.address_note, customer.area) if x) if customer else ""
        )
        if not where:
            raise BookingError("address_needed")
        return where
    return None


async def _offered(
    s: AsyncSession,
    type_: AppointmentType,
    starts_at: datetime,
    *,
    tz: ZoneInfo,
    now: datetime,
    config: AppointmentsConfig,
    ignore: uuid.UUID | None = None,
) -> Slot:
    day = starts_at.astimezone(tz).date()
    slots = await free_slots(
        s, type_=type_, day_from=day, day_to=day, tz=tz, now=now, config=config, ignore=ignore
    )
    for slot in slots:
        if slot.starts_at == starts_at:
            return slot
    raise BookingError(
        "slot_not_available",
        hint="call get_availability and offer only the times it returns",
    )


async def book(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    req: BookingRequest,
    *,
    tz: ZoneInfo,
    now: datetime,
    config: AppointmentsConfig,
    only_offered: bool,
) -> Appointment:
    if only_offered:
        slot = await _offered(s, req.type_, req.starts_at, tz=tz, now=now, config=config)
        candidates = list(slot.resource_ids)
    elif req.resource_id is not None:
        candidates = [r.id for r in await _resources(s, [req.resource_id])]
        if not candidates:
            raise BookingError("resource_not_found")
    else:
        candidates = [r.id for r in await _resources(s)]
        if not candidates:
            raise BookingError("no_resources")
    location = await _location(s, req)
    ref = await next_ref(s, tenant_id)
    ends_at = req.starts_at + timedelta(minutes=req.type_.duration_min)
    for rid in candidates:
        appt = Appointment(
            ref=ref,
            customer_id=req.customer_id,
            type_id=req.type_.id,
            resource_id=rid,
            subject_module=req.subject.module if req.subject else None,
            subject_id=req.subject.id if req.subject else None,
            subject_label=req.subject.label if req.subject else None,
            starts_at=req.starts_at,
            ends_at=ends_at,
            status=req.status,
            location_note=location,
            notes=req.notes,
            source=req.source,
            conversation_id=req.conversation_id,
            created_by=req.created_by,
        )
        try:
            async with s.begin_nested():
                s.add(appt)
                await s.flush()
        except IntegrityError as exc:
            if not _is_overlap(exc):
                raise
            continue
        return appt
    raise BookingError("slot_taken")


async def move(
    s: AsyncSession,
    appt: Appointment,
    type_: AppointmentType,
    new_start: datetime,
    *,
    tz: ZoneInfo,
    now: datetime,
    config: AppointmentsConfig,
    only_offered: bool,
    resource_id: uuid.UUID | None = None,
) -> None:
    """Move a live appointment. Keeps its resource when that one is free, else takes another."""
    if only_offered:
        free = (
            await _offered(s, type_, new_start, tz=tz, now=now, config=config, ignore=appt.id)
        ).resource_ids
        candidates = sorted(free, key=lambda r: r != appt.resource_id)
    elif resource_id is not None:
        candidates = [resource_id]
    else:
        others = [r.id for r in await _resources(s) if r.id != appt.resource_id]
        candidates = [appt.resource_id, *others]
    for rid in candidates:
        try:
            async with s.begin_nested():
                appt.resource_id = rid
                appt.starts_at = new_start
                appt.ends_at = new_start + timedelta(minutes=type_.duration_min)
                await s.flush()
        except IntegrityError as exc:
            if not _is_overlap(exc):
                raise
            continue
        # a rolled-back savepoint expires what it touched; reload so callers can read the row
        await s.refresh(appt)
        return
    await s.refresh(appt)  # back to the stored times
    raise BookingError("slot_taken")


def inside_cutoff(appt: Appointment, now: datetime, config: AppointmentsConfig) -> bool:
    return appt.starts_at - now < timedelta(hours=config.cancel_cutoff_hours)

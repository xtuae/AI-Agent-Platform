"""/api/v1/m/appointments — the agenda, and the settings that decide what can be booked.

Reading is open to every role; booking and changing appointments needs `agent`; types, resources,
weekly hours and time off are admin-only. The dashboard may book any time (a person decides), but
never over another live appointment of the same resource: the database refuses that (409).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select

from api.api.v1.common import (
    MAX_PAGE,
    In,
    audit,
    conflict,
    load_tenant,
    local_today,
    not_found,
    unprocessable,
    zone,
)
from api.auth.deps import Admin, Agent, Ctx, Viewer
from api.db.models import (
    Appointment,
    AppointmentResource,
    AppointmentType,
    AuditLog,
    AvailabilityException,
    AvailabilityRule,
    Customer,
    TenantUser,
)
from api.modules.appointments.config import AppointmentsConfig, config_of
from api.modules.appointments.models import LIVE
from api.modules.appointments.service import (
    BookingError,
    BookingRequest,
    book,
    free_slots,
    iso_local,
    move,
    parse_start,
    resolve_subject,
)

router = APIRouter()

Status = Literal["requested", "confirmed", "cancelled", "completed", "no_show"]
LocationKind = Literal["office", "onsite", "video", "phone"]
TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"confirmed", "cancelled"}),
    "confirmed": frozenset({"completed", "no_show", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "no_show": frozenset(),
}
MAX_RANGE_DAYS = 62


# ---------------------------------------------------------------- schemas


class TypeOut(BaseModel):
    id: uuid.UUID
    name_en: str
    name_ar: str | None
    duration_min: int
    location_kind: LocationKind
    needs_address: bool
    is_active: bool


class ResourceOut(BaseModel):
    id: uuid.UUID
    name: str
    tenant_user_id: uuid.UUID | None
    is_active: bool


class CustomerRef(BaseModel):
    id: uuid.UUID
    name: str | None
    wa_id: str


class AppointmentOut(BaseModel):
    id: uuid.UUID
    ref: str
    status: Status
    starts_at: datetime
    ends_at: datetime
    local: str  # tenant-local "YYYY-MM-DDTHH:MM"
    type: TypeOut
    resource: ResourceOut
    customer: CustomerRef
    subject_module: str | None
    subject_label: str | None
    location_note: str | None
    notes: str | None
    source: str
    created_by: str | None
    created_at: datetime
    conversation_id: uuid.UUID | None
    next_statuses: list[Status]


class HistoryEntry(BaseModel):
    at: datetime
    actor: str
    action: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class AppointmentDetail(AppointmentOut):
    history: list[HistoryEntry]


class AppointmentPage(BaseModel):
    items: list[AppointmentOut]
    total: int


class SlotOut(BaseModel):
    starts_at: datetime
    local: str
    resource_ids: list[uuid.UUID]


class AppointmentCreate(In):
    customer_id: uuid.UUID
    type_id: uuid.UUID
    starts_at: str = Field(min_length=10, max_length=40)  # local time unless it has an offset
    resource_id: uuid.UUID | None = None
    status: Literal["requested", "confirmed"] = "confirmed"
    location_note: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=500)
    # what it is about, in the terms of the module that owns it, e.g. {"listing_ref": "JVC-1204"}
    subject: dict[str, str] | None = Field(default=None, max_length=5)


class AppointmentPatch(In):
    status: Status | None = None
    starts_at: str | None = Field(default=None, min_length=10, max_length=40)
    resource_id: uuid.UUID | None = None
    location_note: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=500)


class TypeIn(In):
    name_en: str = Field(min_length=1, max_length=120)
    name_ar: str | None = Field(default=None, max_length=120)
    duration_min: int = Field(ge=5, le=480)
    location_kind: LocationKind
    needs_address: bool = False
    is_active: bool = True


class TypePatch(In):
    name_en: str | None = Field(default=None, min_length=1, max_length=120)
    name_ar: str | None = Field(default=None, max_length=120)
    duration_min: int | None = Field(default=None, ge=5, le=480)
    location_kind: LocationKind | None = None
    needs_address: bool | None = None
    is_active: bool | None = None


class ResourceIn(In):
    name: str = Field(min_length=1, max_length=120)
    tenant_user_id: uuid.UUID | None = None
    is_active: bool = True


class ResourcePatch(In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    tenant_user_id: uuid.UUID | None = None
    is_active: bool | None = None


class HoursIn(In):
    weekday: int = Field(ge=0, le=6)  # 0 = Monday
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def _ordered(self) -> HoursIn:
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class HoursSet(In):
    hours: list[HoursIn] = Field(max_length=50)


class HoursOut(BaseModel):
    weekday: int
    start_time: time
    end_time: time


class ExceptionIn(In):
    resource_id: uuid.UUID | None = None  # none = everyone
    day: date
    start_time: time | None = None  # none = the whole day
    end_time: time | None = None
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _span(self) -> ExceptionIn:
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("give both start_time and end_time, or neither")
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class ExceptionOut(BaseModel):
    id: uuid.UUID
    resource_id: uuid.UUID | None
    day: date
    start_time: time | None
    end_time: time | None
    reason: str | None


# ---------------------------------------------------------------- helpers


def _type(t: AppointmentType) -> TypeOut:
    return TypeOut(
        id=t.id,
        name_en=t.name_en,
        name_ar=t.name_ar,
        duration_min=t.duration_min,
        location_kind=t.location_kind,  # type: ignore[arg-type]  # DB CHECK guarantees the literal
        needs_address=t.needs_address,
        is_active=t.is_active,
    )


def _resource(r: AppointmentResource) -> ResourceOut:
    return ResourceOut(id=r.id, name=r.name, tenant_user_id=r.tenant_user_id, is_active=r.is_active)


def _out(
    a: Appointment, t: AppointmentType, r: AppointmentResource, c: Customer, tz: Any
) -> AppointmentOut:
    return AppointmentOut(
        id=a.id,
        ref=a.ref,
        status=a.status,  # type: ignore[arg-type]
        starts_at=a.starts_at,
        ends_at=a.ends_at,
        local=iso_local(a.starts_at, tz),
        type=_type(t),
        resource=_resource(r),
        customer=CustomerRef(id=c.id, name=c.name, wa_id=c.wa_id),
        subject_module=a.subject_module,
        subject_label=a.subject_label,
        location_note=a.location_note,
        notes=a.notes,
        source=a.source,
        created_by=a.created_by,
        created_at=a.created_at,
        conversation_id=a.conversation_id,
        next_statuses=sorted(TRANSITIONS.get(a.status, frozenset())),  # type: ignore[arg-type]
    )


def _joined() -> Any:
    return (
        select(Appointment, AppointmentType, AppointmentResource, Customer)
        .join(AppointmentType, AppointmentType.id == Appointment.type_id)
        .join(AppointmentResource, AppointmentResource.id == Appointment.resource_id)
        .join(Customer, Customer.id == Appointment.customer_id)
    )


async def _config(ctx: Ctx) -> AppointmentsConfig:
    return config_of((await ctx.modules()).config("appointments"))


def _booking_error(exc: BookingError) -> Exception:
    if exc.code == "slot_taken":
        return conflict({"code": "slot_taken"})
    return unprocessable(exc.code, **exc.details)


def _start(raw: str, tz: Any) -> datetime:
    try:
        return parse_start(raw, tz)
    except ValueError as exc:
        raise unprocessable("invalid_starts_at", expected="YYYY-MM-DDTHH:MM") from exc


# ---------------------------------------------------------------- agenda


@router.get("", response_model=AppointmentPage)
async def list_appointments(
    ctx: Viewer,
    day_from: Annotated[date | None, Query(alias="from")] = None,
    day_to: Annotated[date | None, Query(alias="to")] = None,
    status_: Annotated[list[Status] | None, Query(alias="status")] = None,
    resource_id: uuid.UUID | None = None,
    type_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = MAX_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AppointmentPage:
    tenant = await load_tenant(ctx)
    tz = zone(tenant)
    day_from = day_from or local_today(tenant)
    day_to = day_to or day_from + timedelta(days=6)
    if day_to < day_from or (day_to - day_from).days > MAX_RANGE_DAYS:
        raise unprocessable("invalid_range", max_days=MAX_RANGE_DAYS)
    start = datetime.combine(day_from, time.min, tz).astimezone(UTC)
    end = datetime.combine(day_to + timedelta(days=1), time.min, tz).astimezone(UTC)
    conds: list[Any] = [Appointment.starts_at >= start, Appointment.starts_at < end]
    if status_:
        conds.append(Appointment.status.in_(status_))
    if resource_id:
        conds.append(Appointment.resource_id == resource_id)
    if type_id:
        conds.append(Appointment.type_id == type_id)
    if customer_id:
        conds.append(Appointment.customer_id == customer_id)
    async with ctx.tx() as s:
        total = int(
            await s.scalar(select(func.count()).select_from(Appointment).where(*conds)) or 0
        )
        rows = (
            await s.execute(
                _joined().where(*conds).order_by(Appointment.starts_at).limit(limit).offset(offset)
            )
        ).all()
    return AppointmentPage(items=[_out(a, t, r, c, tz) for a, t, r, c in rows], total=total)


@router.get("/availability", response_model=list[SlotOut])
async def availability(
    ctx: Viewer,
    type_id: uuid.UUID,
    day_from: Annotated[date | None, Query(alias="from")] = None,
    day_to: Annotated[date | None, Query(alias="to")] = None,
    resource_id: uuid.UUID | None = None,
) -> list[SlotOut]:
    tenant = await load_tenant(ctx)
    tz = zone(tenant)
    day_from = day_from or local_today(tenant)
    day_to = min(day_to or day_from, day_from + timedelta(days=13))
    cfg = await _config(ctx)
    async with ctx.tx() as s:
        type_ = await s.get(AppointmentType, type_id)
        if type_ is None:
            raise not_found("appointment type")
        slots = await free_slots(
            s,
            type_=type_,
            day_from=day_from,
            day_to=day_to,
            tz=tz,
            now=datetime.now(UTC),
            config=cfg,
            resource_ids=[resource_id] if resource_id else None,
        )
    return [
        SlotOut(
            starts_at=x.starts_at,
            local=iso_local(x.starts_at, tz),
            resource_ids=list(x.resource_ids),
        )
        for x in slots
    ]


# ---------------------------------------------------------------- settings: types


@router.get("/types", response_model=list[TypeOut])
async def list_types(ctx: Viewer) -> list[TypeOut]:
    async with ctx.tx() as s:
        rows = (await s.scalars(select(AppointmentType).order_by(AppointmentType.name_en))).all()
    return [_type(t) for t in rows]


@router.post("/types", response_model=TypeOut, status_code=status.HTTP_201_CREATED)
async def create_type(body: TypeIn, ctx: Admin) -> TypeOut:
    async with ctx.tx() as s:
        t = AppointmentType(**body.model_dump())
        s.add(t)
        await s.flush()
        audit(s, ctx, "create_appointment_type", "appointment_type", t.id, after=body.model_dump())
        return _type(t)


@router.patch("/types/{type_id}", response_model=TypeOut)
async def update_type(type_id: uuid.UUID, body: TypePatch, ctx: Admin) -> TypeOut:
    changes = body.model_dump(exclude_unset=True)
    async with ctx.tx() as s:
        t = await s.get(AppointmentType, type_id, with_for_update=True)
        if t is None:
            raise not_found("appointment type")
        before = {k: getattr(t, k) for k in changes}
        for k, v in changes.items():
            setattr(t, k, v)
        if changes:
            audit(
                s,
                ctx,
                "update_appointment_type",
                "appointment_type",
                t.id,
                before=before,
                after=changes,
            )
        await s.flush()
        return _type(t)


# ---------------------------------------------------------------- settings: resources + hours


async def _check_user(ctx: Ctx, user_id: uuid.UUID | None) -> None:
    if user_id is None:
        return
    async with ctx.platform() as p:
        user = await p.scalar(
            select(TenantUser.id).where(
                TenantUser.id == user_id, TenantUser.tenant_id == ctx.tenant_id
            )
        )
    if user is None:
        raise not_found("team member")


@router.get("/resources", response_model=list[ResourceOut])
async def list_resources(ctx: Viewer) -> list[ResourceOut]:
    async with ctx.tx() as s:
        rows = (
            await s.scalars(select(AppointmentResource).order_by(AppointmentResource.name))
        ).all()
    return [_resource(r) for r in rows]


@router.post("/resources", response_model=ResourceOut, status_code=status.HTTP_201_CREATED)
async def create_resource(body: ResourceIn, ctx: Admin) -> ResourceOut:
    await _check_user(ctx, body.tenant_user_id)
    async with ctx.tx() as s:
        r = AppointmentResource(**body.model_dump())
        s.add(r)
        await s.flush()
        audit(s, ctx, "create_resource", "appointment_resource", r.id, after={"name": r.name})
        return _resource(r)


@router.patch("/resources/{resource_id}", response_model=ResourceOut)
async def update_resource(resource_id: uuid.UUID, body: ResourcePatch, ctx: Admin) -> ResourceOut:
    changes = body.model_dump(exclude_unset=True)
    async with ctx.tx() as s:
        r = await s.get(AppointmentResource, resource_id, with_for_update=True)
        if r is None:
            raise not_found("resource")
        await _check_user(ctx, changes.get("tenant_user_id"))
        before = {k: str(getattr(r, k)) for k in changes}
        for k, v in changes.items():
            setattr(r, k, v)
        if changes:
            audit(
                s,
                ctx,
                "update_resource",
                "appointment_resource",
                r.id,
                before=before,
                after={k: str(v) for k, v in changes.items()},
            )
        await s.flush()
        return _resource(r)


@router.get("/resources/{resource_id}/hours", response_model=list[HoursOut])
async def get_hours(resource_id: uuid.UUID, ctx: Viewer) -> list[HoursOut]:
    async with ctx.tx() as s:
        if await s.get(AppointmentResource, resource_id) is None:
            raise not_found("resource")
        rows = (
            await s.scalars(
                select(AvailabilityRule)
                .where(AvailabilityRule.resource_id == resource_id)
                .order_by(AvailabilityRule.weekday, AvailabilityRule.start_time)
            )
        ).all()
    return [HoursOut(weekday=r.weekday, start_time=r.start_time, end_time=r.end_time) for r in rows]


@router.put("/resources/{resource_id}/hours", response_model=list[HoursOut])
async def set_hours(resource_id: uuid.UUID, body: HoursSet, ctx: Admin) -> list[HoursOut]:
    """Replace the resource's whole week. Overlapping ranges on one day are refused."""
    hours = body.hours
    by_day: dict[int, list[HoursIn]] = {}
    for h in sorted(hours, key=lambda h: (h.weekday, h.start_time)):
        prev = by_day.setdefault(h.weekday, [])
        if prev and prev[-1].end_time > h.start_time:
            raise unprocessable("hours_overlap", weekday=h.weekday)
        prev.append(h)
    async with ctx.tx() as s:
        r = await s.get(AppointmentResource, resource_id, with_for_update=True)
        if r is None:
            raise not_found("resource")
        await s.execute(delete(AvailabilityRule).where(AvailabilityRule.resource_id == resource_id))
        for h in hours:
            s.add(AvailabilityRule(resource_id=resource_id, **h.model_dump()))
        audit(
            s,
            ctx,
            "set_hours",
            "appointment_resource",
            resource_id,
            after={"hours": [h.model_dump(mode="json") for h in hours]},
        )
        await s.flush()
    return [
        HoursOut(**h.model_dump()) for h in sorted(hours, key=lambda h: (h.weekday, h.start_time))
    ]


# ---------------------------------------------------------------- settings: time off


@router.get("/exceptions", response_model=list[ExceptionOut])
async def list_exceptions(
    ctx: Viewer, day_from: Annotated[date | None, Query(alias="from")] = None
) -> list[ExceptionOut]:
    tenant = await load_tenant(ctx)
    since = day_from or local_today(tenant)
    async with ctx.tx() as s:
        rows = (
            await s.scalars(
                select(AvailabilityException)
                .where(AvailabilityException.day >= since)
                .order_by(AvailabilityException.day, AvailabilityException.start_time)
                .limit(MAX_PAGE)
            )
        ).all()
    return [ExceptionOut.model_validate(e, from_attributes=True) for e in rows]


@router.post("/exceptions", response_model=ExceptionOut, status_code=status.HTTP_201_CREATED)
async def create_exception(body: ExceptionIn, ctx: Admin) -> ExceptionOut:
    async with ctx.tx() as s:
        if body.resource_id and await s.get(AppointmentResource, body.resource_id) is None:
            raise not_found("resource")
        e = AvailabilityException(**body.model_dump())
        s.add(e)
        await s.flush()
        audit(
            s,
            ctx,
            "create_time_off",
            "availability_exception",
            e.id,
            after=body.model_dump(mode="json"),
        )
        return ExceptionOut.model_validate(e, from_attributes=True)


@router.delete("/exceptions/{exception_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_exception(exception_id: uuid.UUID, ctx: Admin) -> Response:
    async with ctx.tx() as s:
        e = await s.get(AvailabilityException, exception_id)
        if e is None:
            raise not_found("time off")
        await s.delete(e)
        audit(s, ctx, "delete_time_off", "availability_exception", exception_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- one appointment


@router.get("/{appointment_id}", response_model=AppointmentDetail)
async def get_appointment(appointment_id: uuid.UUID, ctx: Viewer) -> AppointmentDetail:
    tz = zone(await load_tenant(ctx))
    async with ctx.tx() as s:
        found = (await s.execute(_joined().where(Appointment.id == appointment_id))).first()
        if found is None:
            raise not_found("appointment")
        history = (
            await s.scalars(
                select(AuditLog)
                .where(AuditLog.entity == "appointment", AuditLog.entity_id == appointment_id)
                .order_by(AuditLog.at)
            )
        ).all()
    a, t, r, c = found
    base = _out(a, t, r, c, tz)
    return AppointmentDetail(
        **base.model_dump(),
        history=[
            HistoryEntry(at=h.at, actor=h.actor, action=h.action, before=h.before, after=h.after)
            for h in history
        ],
    )


async def _fetch(s: Any, appointment_id: uuid.UUID, tz: Any) -> AppointmentOut:
    found = (await s.execute(_joined().where(Appointment.id == appointment_id))).first()
    assert found is not None
    a, t, r, c = found
    return _out(a, t, r, c, tz)


@router.post("", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
async def create_appointment(body: AppointmentCreate, ctx: Agent) -> AppointmentOut:
    tz = zone(await load_tenant(ctx))
    starts_at = _start(body.starts_at, tz)
    if starts_at < datetime.now(UTC) - timedelta(minutes=5):
        raise unprocessable("starts_at_in_past")
    cfg = await _config(ctx)
    enabled = await ctx.modules()
    async with ctx.tx() as s:
        customer = await s.get(Customer, body.customer_id)
        if customer is None:
            raise not_found("customer")
        type_ = await s.get(AppointmentType, body.type_id)
        if type_ is None:
            raise not_found("appointment type")
        try:
            subject = await resolve_subject(s, enabled, dict(body.subject or {}))
            appt = await book(
                s,
                ctx.tenant_id,
                BookingRequest(
                    customer_id=customer.id,
                    type_=type_,
                    starts_at=starts_at,
                    source="dashboard",
                    created_by=ctx.principal.actor,
                    status=body.status,
                    resource_id=body.resource_id,
                    subject=subject,
                    location_note=body.location_note,
                    notes=body.notes,
                ),
                tz=tz,
                now=datetime.now(UTC),
                config=cfg,
                only_offered=False,
            )
        except BookingError as exc:
            if exc.code == "resource_not_found":
                raise not_found("resource") from exc
            raise _booking_error(exc) from exc
        audit(
            s,
            ctx,
            "book_appointment",
            "appointment",
            appt.id,
            after={
                "ref": appt.ref,
                "starts_at": iso_local(appt.starts_at, tz),
                "status": appt.status,
            },
        )
        return await _fetch(s, appt.id, tz)


@router.patch("/{appointment_id}", response_model=AppointmentOut)
async def update_appointment(
    appointment_id: uuid.UUID, body: AppointmentPatch, ctx: Agent
) -> AppointmentOut:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise unprocessable("nothing_to_change")
    tz = zone(await load_tenant(ctx))
    cfg = await _config(ctx)
    async with ctx.tx() as s:
        appt = await s.get(Appointment, appointment_id, with_for_update=True)
        if appt is None:
            raise not_found("appointment")
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        new_status = changes.pop("status", None)
        new_start = changes.pop("starts_at", None)
        new_resource = changes.pop("resource_id", None)
        if (new_start or new_resource) and appt.status not in LIVE:
            raise conflict(f"a {appt.status} appointment cannot be moved")
        if new_resource is not None and await s.get(AppointmentResource, new_resource) is None:
            raise not_found("resource")
        if new_start is not None or (new_resource is not None and new_resource != appt.resource_id):
            type_ = await s.get(AppointmentType, appt.type_id)
            assert type_ is not None
            target = _start(new_start, tz) if new_start else appt.starts_at
            before["starts_at"], before["resource_id"] = (
                iso_local(appt.starts_at, tz),
                str(appt.resource_id),
            )
            try:
                await move(
                    s,
                    appt,
                    type_,
                    target,
                    tz=tz,
                    now=datetime.now(UTC),
                    config=cfg,
                    only_offered=False,
                    resource_id=new_resource,
                )
            except BookingError as exc:
                raise _booking_error(exc) from exc
            after["starts_at"], after["resource_id"] = (
                iso_local(appt.starts_at, tz),
                str(appt.resource_id),
            )
        if new_status is not None and new_status != appt.status:
            if new_status not in TRANSITIONS[appt.status]:
                raise conflict(
                    {
                        "code": "invalid_transition",
                        "from": appt.status,
                        "to": new_status,
                        "allowed": sorted(TRANSITIONS[appt.status]),
                    }
                )
            before["status"], after["status"] = appt.status, new_status
            appt.status = new_status
        for field_name, value in changes.items():
            if getattr(appt, field_name) != value:
                before[field_name], after[field_name] = getattr(appt, field_name), value
                setattr(appt, field_name, value)
        # (a status change never makes an overlap: nothing can go back to a live status)
        await s.flush()
        if after:
            audit(s, ctx, "update_appointment", "appointment", appt.id, before=before, after=after)
        return await _fetch(s, appt.id, tz)

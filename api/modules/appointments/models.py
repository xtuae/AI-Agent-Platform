"""Appointments module tables. The no-double-booking guard is a database trigger (migration 0006):
it holds even when two bookings race."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk

LOCATION_KINDS = ("office", "onsite", "video", "phone")
STATUSES = ("requested", "confirmed", "cancelled", "completed", "no_show")
LIVE = ("requested", "confirmed")  # these hold the resource's time


class AppointmentType(TenantScoped, Base):
    __tablename__ = "appointment_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("duration_min between 5 and 480", name="duration"),
        CheckConstraint(
            "location_kind in ('office','onsite','video','phone')", name="location_kind"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name_en: Mapped[str] = mapped_column(Text)
    name_ar: Mapped[str | None] = mapped_column(Text)
    duration_min: Mapped[int] = mapped_column(SmallInteger)
    location_kind: Mapped[str] = mapped_column(Text)
    needs_address: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AppointmentResource(TenantScoped, Base):
    """Who or what is booked: an agent, a lawyer, a meeting room."""

    __tablename__ = "appointment_resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "tenant_user_id"], ["tenant_users.tenant_id", "tenant_users.id"]
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    tenant_user_id: Mapped[uuid.UUID | None]
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AvailabilityRule(TenantScoped, Base):
    """Weekly working hours of a resource. weekday 0 = Monday … 6 = Sunday."""

    __tablename__ = "availability_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["appointment_resources.tenant_id", "appointment_resources.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("weekday between 0 and 6", name="weekday"),
        CheckConstraint("start_time < end_time", name="order"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    resource_id: Mapped[uuid.UUID]
    weekday: Mapped[int] = mapped_column(SmallInteger)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)


class AvailabilityException(TenantScoped, Base):
    """Time off: a whole day (no times) or part of one, for one resource or (resource NULL) all."""

    __tablename__ = "availability_exceptions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["appointment_resources.tenant_id", "appointment_resources.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    resource_id: Mapped[uuid.UUID | None]
    day: Mapped[date]
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    reason: Mapped[str | None] = mapped_column(Text)


class Appointment(TenantScoped, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "ref"),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "type_id"], ["appointment_types.tenant_id", "appointment_types.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["appointment_resources.tenant_id", "appointment_resources.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"]
        ),
        CheckConstraint("ends_at > starts_at", name="order"),
        CheckConstraint(
            "status in ('requested','confirmed','cancelled','completed','no_show')", name="status"
        ),
        Index("ix_appointments_time", "tenant_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    ref: Mapped[str] = mapped_column(Text)
    customer_id: Mapped[uuid.UUID]
    type_id: Mapped[uuid.UUID]
    resource_id: Mapped[uuid.UUID]
    # what the appointment is about, owned by another module (e.g. listings → a property)
    subject_module: Mapped[str | None] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID | None]
    subject_label: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(Text)
    location_note: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID | None]
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

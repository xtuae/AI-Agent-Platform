"""Customers (the dashboard calls them contacts): core, shared by every module."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk


class Customer(TenantScoped, Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(
            "opt_in_status in ('pending','opted_in','opted_out')", name="opt_in_status"
        ),
        UniqueConstraint("tenant_id", "wa_id"),
        UniqueConstraint("tenant_id", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    wa_id: Mapped[str] = mapped_column(Text)  # E.164, no '+'
    name: Mapped[str | None] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    emirate: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    opt_in_status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    opt_in_at: Mapped[datetime | None]
    opt_in_evidence: Mapped[dict[str, Any] | None]
    opt_out_at: Mapped[datetime | None]
    lifetime_orders: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_order_at: Mapped[datetime | None]
    external_ref: Mapped[str | None] = mapped_column(Text)
    address_note: Mapped[str | None] = mapped_column(Text)

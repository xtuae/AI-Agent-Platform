"""Orders module tables."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk


class Order(TenantScoped, Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "status in ('draft','confirmed','out_for_delivery','delivered','cancelled')",
            name="status",
        ),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"]
        ),
        UniqueConstraint("tenant_id", "order_no"),
        Index("ix_orders_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(index=True)
    order_no: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))
    items: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))
    total_aed: Mapped[Decimal | None]
    source: Mapped[str | None] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    delivery_slot: Mapped[str | None] = mapped_column(Text)
    delivery_date: Mapped[date | None]
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    conversation_id: Mapped[uuid.UUID | None]
    idempotency_key: Mapped[str | None] = mapped_column(Text)

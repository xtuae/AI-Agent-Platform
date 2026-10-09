"""Customers (the dashboard calls them contacts): core, shared by every module."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    func,
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
    # E.164, no '+'. The WhatsApp number; NULL for a customer known only on another channel.
    # A trigger keeps the customer's 'whatsapp' row in customer_identities in step with it.
    wa_id: Mapped[str | None] = mapped_column(Text)
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


class CustomerIdentity(TenantScoped, Base):
    """How a channel knows a customer: (kind, external_id) — a WhatsApp number, a Telegram user
    id. One per channel kind per customer. The 'whatsapp' row is maintained by a trigger on
    customers.wa_id (migration 0009); other kinds are written by their channel's ingest."""

    __tablename__ = "customer_identities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_customer_identities_customer",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "kind", "external_id", name="uq_customer_identities_external"
        ),
        UniqueConstraint(
            "tenant_id", "customer_id", "kind", name="uq_customer_identities_customer_kind"
        ),
        CheckConstraint("kind in ('whatsapp','telegram')", name="kind"),
        CheckConstraint("external_id ~ '^[0-9]{1,32}$'", name="external_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)  # Telegram @handle, display only
    display_name: Mapped[str | None] = mapped_column(Text)
    blocked_at: Mapped[datetime | None]  # the customer blocked the bot: unreachable, not opted out
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime | None]

"""Message templates, campaigns and recipients."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk


class MessageTemplate(TenantScoped, Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "language"),
        UniqueConstraint("tenant_id", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    meta_status: Mapped[str | None] = mapped_column(Text)  # APPROVED | PENDING | REJECTED
    body: Mapped[str | None] = mapped_column(Text)
    variables: Mapped[list[Any] | None]
    meta_template_id: Mapped[str | None] = mapped_column(Text)


class Campaign(TenantScoped, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            "status in ('draft','approved','sending','paused','done','cancelled')", name="status"
        ),
        # Constraint 8, enforced by the database: nothing leaves draft without a human approval.
        # 'cancelled' is allowed without approval so an unapproved draft can be abandoned.
        CheckConstraint(
            "status in ('draft','cancelled') "
            "or (approved_by is not null and approved_at is not null)",
            name="approval_required",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"], ["message_templates.tenant_id", "message_templates.id"]
        ),
        UniqueConstraint("tenant_id", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    template_id: Mapped[uuid.UUID | None]
    segment_query: Mapped[dict[str, Any] | None]
    status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))
    approved_by: Mapped[uuid.UUID | None]
    approved_at: Mapped[datetime | None]
    scheduled_for: Mapped[datetime | None]
    throttle_per_minute: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    budget_cap_aed: Mapped[Decimal | None]
    sent_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    delivered_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    read_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reply_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    order_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    spend_aed: Mapped[Decimal] = mapped_column(server_default=text("0"))


class CampaignRecipient(TenantScoped, Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "campaign_id"], ["campaigns.tenant_id", "campaigns.id"]),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        UniqueConstraint("campaign_id", "customer_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID]
    customer_id: Mapped[uuid.UUID]
    status: Mapped[str | None] = mapped_column(Text)
    wamid: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None]
    cost_aed: Mapped[Decimal | None]

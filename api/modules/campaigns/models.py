"""Campaigns module tables: WhatsApp templates, campaigns and their recipients.

The database refuses a campaign leaving draft without a human approval (approved_by + approved_at),
whatever the API does (constraint 8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk

CampaignStatus = Literal["draft", "approved", "sending", "paused", "done", "cancelled"]
# pending → sent → delivered → read, or failed; skipped = dropped at send time (reason recorded)
RecipientStatus = Literal["pending", "sent", "delivered", "read", "failed", "skipped"]


class MessageTemplate(TenantScoped, Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "language"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("source in ('drafted','meta')", name="source"),
        CheckConstraint("name ~ '^[a-z0-9_]+$' AND char_length(name) <= 512", name="name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)  # MARKETING | UTILITY
    meta_status: Mapped[str | None] = mapped_column(Text)  # None = not submitted | PENDING | …
    body: Mapped[str | None] = mapped_column(Text)
    # [{"index": 1, "meaning": "customer first name", "example": "Ahmed"}, …]
    variables: Mapped[list[Any] | None]
    meta_template_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    created_by: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, server_default=text("'drafted'"))
    submitted_at: Mapped[datetime | None]
    rejected_reason: Mapped[str | None] = mapped_column(Text)


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
        CheckConstraint("throttle_per_minute between 1 and 1000", name="throttle"),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"], ["message_templates.tenant_id", "message_templates.id"]
        ),
        UniqueConstraint("tenant_id", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    template_id: Mapped[uuid.UUID | None]
    # the segment definition (02 §4.3): {"lapsed_60d": …} fields, compiled by campaigns.segments
    segment_query: Mapped[dict[str, Any] | None]
    # how each template variable is filled: [{"source": "contact.first_name", "fallback": "there"},
    # {"source": "text", "value": "AED 10 off"}]
    variable_bindings: Mapped[list[Any] | None]
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
    recipient_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    skipped_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    paused_reason: Mapped[str | None] = mapped_column(Text)


class CampaignRecipient(TenantScoped, Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "campaign_id"], ["campaigns.tenant_id", "campaigns.id"]),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        UniqueConstraint("campaign_id", "customer_id"),
        Index("ix_campaign_recipients_customer_sent", "tenant_id", "customer_id", "sent_at"),
        Index("ix_campaign_recipients_wamid", "wamid"),
        Index("ix_campaign_recipients_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    campaign_id: Mapped[uuid.UUID]
    customer_id: Mapped[uuid.UUID]
    status: Mapped[str | None] = mapped_column(Text)
    wamid: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None]
    cost_aed: Mapped[Decimal | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    skip_reason: Mapped[str | None] = mapped_column(Text)
    replied_at: Mapped[datetime | None]
    variables: Mapped[list[Any] | None]

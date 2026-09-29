"""Conversations and messages."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk


class Conversation(TenantScoped, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"], ["tenant_channels.tenant_id", "tenant_channels.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assigned_to"], ["tenant_users.tenant_id", "tenant_users.id"]
        ),
        UniqueConstraint("tenant_id", "id"),
        Index("ix_conversations_tenant_customer", "tenant_id", "customer_id"),
        # At most one non-closed conversation per customer per channel; ON CONFLICT target.
        Index(
            "uq_conversations_open_per_customer_channel",
            "tenant_id",
            "customer_id",
            "channel_id",
            unique=True,
            postgresql_where=text("state <> 'closed'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID]
    channel_id: Mapped[uuid.UUID]
    state: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    assigned_to: Mapped[uuid.UUID | None]
    last_inbound_at: Mapped[datetime | None]
    last_outbound_at: Mapped[datetime | None]
    service_window_expires_at: Mapped[datetime | None]
    summary: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)


class Message(TenantScoped, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("direction in ('in','out')", name="direction"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "sent_by"], ["tenant_users.tenant_id", "tenant_users.id"]
        ),
        Index("ix_messages_conversation_created", "tenant_id", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID]
    wamid: Mapped[str | None] = mapped_column(Text, unique=True)  # Meta's id; the dedup key
    direction: Mapped[str] = mapped_column(Text)
    msg_type: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    template_name: Mapped[str | None] = mapped_column(Text)
    pricing_category: Mapped[str | None] = mapped_column(Text)
    cost_aed: Mapped[Decimal | None] = mapped_column(Numeric(8, 5))  # stamped at send time
    status: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    sent_by: Mapped[uuid.UUID | None]  # tenant_users.id when a person replied from the dashboard
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    def __repr__(self) -> str:  # never let a message body reach a traceback or log
        return f"Message(id={self.id}, wamid={self.wamid}, direction={self.direction})"

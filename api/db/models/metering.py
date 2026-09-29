"""Metering (the billing spine) and the audit log."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, Numeric, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped


class UsageDaily(TenantScoped, Base):
    """Per-tenant, per-day metering. Upserted on every inbound and outbound message."""

    __tablename__ = "usage_daily"

    # tenant_id comes from TenantScoped; it is also part of the primary key.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True
    )
    day: Mapped[date] = mapped_column(primary_key=True)
    msgs_in: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    msgs_out: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    marketing_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    utility_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    service_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    authentication_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    meta_cost_aed: Mapped[Decimal] = mapped_column(Numeric(10, 4), server_default=text("0"))
    llm_prompt_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    llm_completion_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    llm_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 5), server_default=text("0"))


class AuditLog(TenantScoped, Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None]
    before: Mapped[dict[str, Any] | None]
    after: Mapped[dict[str, Any] | None]
    at: Mapped[datetime] = mapped_column(server_default=func.now())

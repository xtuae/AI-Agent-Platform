"""Opt-in evidence captured through a consent page (api/optin)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk


class OptinVisit(TenantScoped, Base):
    """One tap on "Continue on WhatsApp": the exact wording on screen at that moment, and the
    one-time code the prefilled message carries. Claimed when that message arrives."""

    __tablename__ = "optin_visits"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "link_id"],
            ["optin_links.tenant_id", "optin_links.id"],
            name="fk_optin_visits_link",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            name="fk_optin_visits_customer",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "token"),
        CheckConstraint("(claimed_at IS NULL) = (customer_id IS NULL)", name="claim"),
        Index("ix_optin_visits_link", "tenant_id", "link_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    link_id: Mapped[uuid.UUID]
    token: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    heading: Mapped[str] = mapped_column(Text)
    wording: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    claimed_at: Mapped[datetime | None]
    customer_id: Mapped[uuid.UUID | None]
    wamid: Mapped[str | None] = mapped_column(Text)

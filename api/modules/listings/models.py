"""Listings module table: properties for sale or rent."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, get_args

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk

Purpose = Literal["sale", "rent"]
PropertyType = Literal["apartment", "villa", "townhouse", "office", "retail", "land", "other"]
# The agent only ever sees `available`; `archived` hides a listing from the dashboard's list.
ListingStatus = Literal["draft", "available", "under_offer", "let", "sold", "archived"]
RentPeriod = Literal["year", "month"]
PROPERTY_TYPES: tuple[str, ...] = get_args(PropertyType)


class Listing(TenantScoped, Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "ref"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("purpose in ('sale','rent')", name="purpose"),
        CheckConstraint(
            "property_type in ('apartment','villa','townhouse','office','retail','land','other')",
            name="property_type",
        ),
        CheckConstraint(
            "status in ('draft','available','under_offer','let','sold','archived')", name="status"
        ),
        CheckConstraint(
            "rent_period is null or rent_period in ('year','month')", name="rent_period"
        ),
        CheckConstraint("status <> 'available' or price_aed is not null", name="available_priced"),
        Index("ix_listings_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    ref: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    property_type: Mapped[str] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    community: Mapped[str | None] = mapped_column(Text)
    address_note: Mapped[str | None] = mapped_column(Text)
    bedrooms: Mapped[int | None] = mapped_column(SmallInteger)
    bathrooms: Mapped[int | None] = mapped_column(SmallInteger)
    size_sqft: Mapped[int | None] = mapped_column(Integer)
    price_aed: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rent_period: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))
    viewings_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

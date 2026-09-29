"""Coupons module tables: prepaid bottle books on sale (CouponPackage) and bought (CouponBook)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk


class CouponPackage(TenantScoped, Base):
    __tablename__ = "coupon_packages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku"),
        CheckConstraint(
            "not is_active or (price_aed is not null and bottles_paid is not null)",
            name="active_needs_price",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sku: Mapped[str] = mapped_column(Text)
    name_en: Mapped[str | None] = mapped_column(Text)
    name_ar: Mapped[str | None] = mapped_column(Text)
    price_aed: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    bottles_paid: Mapped[int | None] = mapped_column(Integer)
    bottles_free: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    emirate: Mapped[str | None] = mapped_column(Text)  # NULL = every emirate served
    validity_days: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class CouponBook(TenantScoped, Base):
    __tablename__ = "coupon_books"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        CheckConstraint("bottles_remaining >= 0", name="bottles_remaining_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(index=True)
    sku: Mapped[str | None] = mapped_column(Text)
    price_aed: Mapped[Decimal | None]
    bottles_total: Mapped[int | None] = mapped_column(Integer)
    bottles_free: Mapped[int | None] = mapped_column(Integer)
    bottles_remaining: Mapped[int | None] = mapped_column(Integer)
    purchased_at: Mapped[datetime | None]
    expires_at: Mapped[date | None]

"""Catalog module tables: what the business sells."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk


class Product(TenantScoped, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("tenant_id", "sku"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    sku: Mapped[str] = mapped_column(Text)
    name_en: Mapped[str | None] = mapped_column(Text)
    name_ar: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)  # 'water' | 'snack'
    brand: Mapped[str | None] = mapped_column(Text)
    price_aed: Mapped[Decimal | None]
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    cross_sell_priority: Mapped[int | None] = mapped_column(Integer)
    stock_note: Mapped[str | None] = mapped_column(Text)

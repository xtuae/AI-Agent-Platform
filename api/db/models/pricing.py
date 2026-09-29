"""Dated Meta price table (platform-wide, no tenant). A price change is a row, not a deploy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Identity, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base


class MetaRate(Base):
    __tablename__ = "meta_rates"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    market: Mapped[str] = mapped_column(Text)  # recipient country, ISO 3166-1 alpha-2
    category: Mapped[str] = mapped_column(Text)
    rate_aed: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    effective_from: Mapped[date]
    effective_to: Mapped[date | None]  # exclusive
    source: Mapped[str | None] = mapped_column(Text)

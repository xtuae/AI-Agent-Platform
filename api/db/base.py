"""Declarative base and the TenantScoped marker.

Every model that inherits `TenantScoped` gets `tenant_id UUID NOT NULL` and is registered in
`TENANT_SCOPED_TABLES`. The migration enables + forces RLS on exactly that set, and a test asserts
the database agrees — so a new tenant table cannot be added without isolation.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import Date, DateTime, ForeignKey, MetaData, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TENANT_SCOPED_TABLES: set[str] = set()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        date: Date(),
        Decimal: Numeric(),
        dict[str, Any]: JSONB(),
        list[Any]: JSONB(),
    }


def uuid_pk() -> MappedColumn[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


class TenantScoped:
    """Mixin for every RLS-protected table. Do not add tenant tables without it."""

    __tenant_scoped__: ClassVar[bool] = True

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805 — SQLAlchemy declared_attr convention
        return mapped_column(
            UUID(as_uuid=True),
            ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        tablename = cls.__dict__.get("__tablename__")
        if isinstance(tablename, str):
            TENANT_SCOPED_TABLES.add(tablename)

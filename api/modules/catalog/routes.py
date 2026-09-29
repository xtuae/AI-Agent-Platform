"""/api/v1/m/catalog — products and prices. Reading is open to every role; changing is admin-only.
The agent and every new order use these prices."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.api.v1.common import In, audit, conflict, money, not_found
from api.auth.deps import Admin, Viewer
from api.db.models import Product

router = APIRouter()


class ProductOut(BaseModel):
    id: uuid.UUID
    sku: str
    name_en: str | None
    name_ar: str | None
    category: str | None
    brand: str | None
    price_aed: str | None
    is_active: bool
    cross_sell_priority: int | None
    stock_note: str | None


class ProductCreate(In):
    sku: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    name_en: str = Field(min_length=1, max_length=120)
    name_ar: str | None = Field(default=None, max_length=120)
    category: Literal["water", "snack", "other"]
    brand: str | None = Field(default=None, max_length=80)
    price_aed: Decimal = Field(ge=0, le=100000, max_digits=10, decimal_places=2)
    is_active: bool = True
    cross_sell_priority: int | None = Field(default=None, ge=0, le=100)
    stock_note: str | None = Field(default=None, max_length=200)


class ProductPatch(In):
    name_en: str | None = Field(default=None, min_length=1, max_length=120)
    name_ar: str | None = Field(default=None, max_length=120)
    category: Literal["water", "snack", "other"] | None = None
    brand: str | None = Field(default=None, max_length=80)
    price_aed: Decimal | None = Field(
        default=None, ge=0, le=100000, max_digits=10, decimal_places=2
    )
    is_active: bool | None = None
    cross_sell_priority: int | None = Field(default=None, ge=0, le=100)
    stock_note: str | None = Field(default=None, max_length=200)


def _product(p: Product) -> ProductOut:
    return ProductOut(
        id=p.id,
        sku=p.sku,
        name_en=p.name_en,
        name_ar=p.name_ar,
        category=p.category,
        brand=p.brand,
        price_aed=money(p.price_aed),
        is_active=p.is_active,
        cross_sell_priority=p.cross_sell_priority,
        stock_note=p.stock_note,
    )


def _jsonable(v: Any) -> Any:
    return str(v) if isinstance(v, Decimal) else v


@router.get("/products", response_model=list[ProductOut])
async def list_products(ctx: Viewer, active: bool | None = None) -> list[ProductOut]:
    stmt = select(Product).order_by(Product.category, Product.name_en)
    if active is not None:
        stmt = stmt.where(Product.is_active.is_(active))
    async with ctx.tx() as s:
        return [_product(p) for p in (await s.scalars(stmt)).all()]


@router.post("/products", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(body: ProductCreate, ctx: Admin) -> ProductOut:
    async with ctx.tx() as s:
        if await s.scalar(select(Product.id).where(Product.sku == body.sku)) is not None:
            raise conflict({"code": "sku_exists"})
        p = Product(**body.model_dump())
        s.add(p)
        await s.flush()
        audit(
            s,
            ctx,
            "create_product",
            "product",
            p.id,
            after={k: _jsonable(v) for k, v in body.model_dump().items()},
        )
        return _product(p)


@router.patch("/products/{product_id}", response_model=ProductOut)
async def update_product(product_id: uuid.UUID, body: ProductPatch, ctx: Admin) -> ProductOut:
    changes = body.model_dump(exclude_unset=True)
    async with ctx.tx() as s:
        p = await s.get(Product, product_id, with_for_update=True)
        if p is None:
            raise not_found("product")
        before = {k: _jsonable(getattr(p, k)) for k in changes}
        for k, v in changes.items():
            setattr(p, k, v)
        if changes:
            audit(
                s,
                ctx,
                "update_product",
                "product",
                p.id,
                before=before,
                after={k: _jsonable(v) for k, v in changes.items()},
            )
        await s.flush()
        return _product(p)

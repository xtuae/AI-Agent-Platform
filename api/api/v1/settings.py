"""/api/v1/settings, /products, /team — business hours, escalation number, products and prices,
team logins. Reading is open to every role; changing is admin-only.

tenant_settings and tenant_users are platform tables (no RLS), so every statement here filters on
`ctx.tenant_id` explicitly; the cross-tenant tests cover each route.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update

from api.api.v1.common import In, audit, conflict, load_tenant, money, normalise_wa_id, not_found
from api.auth.deps import Admin, Viewer, revoke_access
from api.auth.tokens import Role
from api.config import get_settings
from api.core.passwords import hash_password
from api.db.models import AuthRefreshToken, Product, TenantSettings, TenantUser

router = APIRouter(tags=["settings"])

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HOURS = re.compile(r"^(closed|([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-4]):[0-5]\d)$")


# ---------------------------------------------------------------- settings


class SettingsOut(BaseModel):
    business_name: str
    timezone: str
    business_hours: dict[str, str]
    escalation_phone: str | None
    monthly_message_cap_aed: str | None
    meta_charges_borne_by_us_until: date | None


class SettingsPatch(In):
    business_hours: dict[str, str] | None = None
    escalation_phone: str | None = Field(default=None, max_length=24)

    @field_validator("business_hours")
    @classmethod
    def _hours(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        unknown = set(v) - set(DAYS)
        if unknown:
            raise ValueError(f"unknown days {sorted(unknown)}; use {', '.join(DAYS)}")
        for day, span in v.items():
            if not _HOURS.match(span.strip()):
                raise ValueError(f"{day}: use 'HH:MM-HH:MM' or 'closed'")
        return {d: v[d].strip() for d in DAYS if d in v}

    @field_validator("escalation_phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalise_wa_id(v) if v else None


def _hours_out(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if str(k) in DAYS}
    return {}


async def _settings(ctx: Viewer) -> SettingsOut:
    tenant = await load_tenant(ctx)
    async with ctx.platform() as s:
        row = await s.get(TenantSettings, ctx.tenant_id)
    return SettingsOut(
        business_name=tenant.name,
        timezone=tenant.timezone,
        business_hours=_hours_out(row.business_hours if row else None),
        escalation_phone=row.escalation_phone if row else None,
        monthly_message_cap_aed=money(row.monthly_message_cap_aed) if row else None,
        meta_charges_borne_by_us_until=tenant.meta_charges_borne_by_us_until,
    )


@router.get("/settings", response_model=SettingsOut)
async def get_settings_(ctx: Viewer) -> SettingsOut:
    return await _settings(ctx)


@router.patch("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsPatch, ctx: Admin) -> SettingsOut:
    changes = body.model_dump(exclude_unset=True)
    async with ctx.platform() as s:
        row = await s.get(TenantSettings, ctx.tenant_id, with_for_update=True)
        if row is None:
            row = TenantSettings(tenant_id=ctx.tenant_id)
            s.add(row)
        before = {k: getattr(row, k) for k in changes}
        for k, v in changes.items():
            setattr(row, k, v)
    if changes:
        async with ctx.tx() as s:
            audit(
                s,
                ctx,
                "update_settings",
                "tenant_settings",
                ctx.tenant_id,
                before=before,
                after=changes,
            )
    return await _settings(ctx)


# ---------------------------------------------------------------- products


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


# ---------------------------------------------------------------- team logins


class TeamMember(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    role: Role
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class TeamCreate(In):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str | None = Field(default=None, max_length=120)
    role: Role
    password: str = Field(min_length=12, max_length=256)  # temporary; they change it


class TeamPatch(In):
    name: str | None = Field(default=None, max_length=120)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)  # reset


def _member(u: TenantUser) -> TeamMember:
    return TeamMember(
        id=u.id,
        email=u.email,
        name=u.name,
        role=u.role,  # type: ignore[arg-type]
        is_active=u.is_active,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


@router.get("/team", response_model=list[TeamMember])
async def list_team(
    ctx: Admin, include_inactive: Annotated[bool, Query()] = True
) -> list[TeamMember]:
    stmt = select(TenantUser).where(TenantUser.tenant_id == ctx.tenant_id)
    if not include_inactive:
        stmt = stmt.where(TenantUser.is_active.is_(True))
    async with ctx.platform() as s:
        return [_member(u) for u in (await s.scalars(stmt.order_by(TenantUser.email))).all()]


@router.post("/team", response_model=TeamMember, status_code=status.HTTP_201_CREATED)
async def add_member(body: TeamCreate, ctx: Admin) -> TeamMember:
    email = body.email.lower()
    password_hash = await asyncio.to_thread(hash_password, body.password)
    async with ctx.platform() as s:
        exists = await s.scalar(
            select(TenantUser.id).where(
                TenantUser.tenant_id == ctx.tenant_id, func.lower(TenantUser.email) == email
            )
        )
        if exists is not None:
            raise conflict({"code": "email_exists"})
        u = TenantUser(
            tenant_id=ctx.tenant_id,
            email=email,
            name=body.name,
            role=body.role,
            password_hash=password_hash,
        )
        s.add(u)
        await s.flush()
        await s.refresh(u)
        out = _member(u)
    async with ctx.tx() as s:
        audit(s, ctx, "add_member", "tenant_user", out.id, after={"role": body.role})
    return out


@router.patch("/team/{user_id}", response_model=TeamMember)
async def update_member(user_id: uuid.UUID, body: TeamPatch, ctx: Admin) -> TeamMember:
    changes = body.model_dump(exclude_unset=True)
    password = changes.pop("password", None)
    new_hash = await asyncio.to_thread(hash_password, password) if password else None
    async with ctx.platform() as s:
        u = await s.scalar(
            select(TenantUser)
            .where(TenantUser.id == user_id, TenantUser.tenant_id == ctx.tenant_id)
            .with_for_update()
        )
        if u is None:
            raise not_found("team member")
        demoting = changes.get("role", "admin") != "admin" or changes.get("is_active") is False
        if u.role == "admin" and u.is_active and demoting:
            admins = await s.scalar(
                select(func.count())
                .select_from(TenantUser)
                .where(
                    TenantUser.tenant_id == ctx.tenant_id,
                    TenantUser.role == "admin",
                    TenantUser.is_active.is_(True),
                )
            )
            if int(admins or 0) <= 1:
                raise conflict({"code": "last_admin"})
        before = {k: getattr(u, k) for k in changes}
        for k, v in changes.items():
            setattr(u, k, v)
        access_changed = (
            new_hash is not None
            or ("role" in changes and before["role"] != u.role)
            or ("is_active" in changes and before["is_active"] and not u.is_active)
        )
        if new_hash is not None:
            u.password_hash = new_hash
        if access_changed:  # end their sessions: refresh tokens now, access tokens via Redis
            await s.execute(
                update(AuthRefreshToken)
                .where(AuthRefreshToken.user_id == u.id, AuthRefreshToken.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
        await s.flush()
        out = _member(u)
    if access_changed:
        await revoke_access(ctx.redis, get_settings(), user_id)
    after = dict(changes) | ({"password": "reset"} if new_hash else {})
    if after:
        async with ctx.tx() as s:
            audit(s, ctx, "update_member", "tenant_user", user_id, before=before, after=after)
    return out

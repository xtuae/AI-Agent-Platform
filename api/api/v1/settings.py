"""/api/v1/settings, /team — business hours, escalation number, team logins (products and prices
moved to the catalog module). Reading is open to every role; changing is admin-only.

tenant_settings and tenant_users are platform tables (no RLS), so every statement here filters on
`ctx.tenant_id` explicitly; the cross-tenant tests cover each route.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from api.api.v1.common import In, audit, conflict, load_tenant, money, normalise_wa_id, not_found
from api.auth import team
from api.auth.deps import Admin, Viewer, revoke_access
from api.auth.tokens import Role
from api.config import get_settings
from api.core.passwords import hash_password
from api.db.models import TenantSettings, TenantUser

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
    password_hash = await asyncio.to_thread(hash_password, body.password)
    async with ctx.platform() as s:
        try:
            u = await team.add_user(
                s,
                ctx.tenant_id,
                email=body.email,
                name=body.name,
                role=body.role,
                password_hash=password_hash,
            )
        except team.TeamError as exc:
            raise conflict({"code": exc.code}) from exc
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
        try:
            done = await team.update_user(s, ctx.tenant_id, user_id, changes, new_hash=new_hash)
        except team.TeamError as exc:
            if exc.code == "not_found":
                raise not_found("team member") from exc
            raise conflict({"code": exc.code}) from exc
        out = _member(done.user)
    before, access_changed = done.before, done.access_changed
    if access_changed:
        await revoke_access(ctx.redis, get_settings(), user_id)
    after = dict(changes) | ({"password": "reset"} if new_hash else {})
    if after:
        async with ctx.tx() as s:
            audit(s, ctx, "update_member", "tenant_user", user_id, before=before, after=after)
    return out

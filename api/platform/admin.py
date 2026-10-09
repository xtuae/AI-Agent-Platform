"""/api/v1/platform/admin — what the onboarding scripts do, from the console: create and edit
clients, switch their modules, connect WhatsApp numbers and rotate their Meta tokens, connect a
Telegram bot, manage a client's dashboard users and HMH Labz's own staff, and download a client's
DPA description.

Roles: support reads; ops changes clients; owner manages staff. Every change to a client is
written to that client's audit log; staff changes are logged (they belong to no tenant).
Secrets only ever travel inward: a Meta token is checked against Meta, a bot token against
Telegram (getMe), each encrypted and stored, and no response contains either. The Telegram webhook
is registered by the server with a fresh secret_token, of which only the sha256 is kept. A new
staff member's TOTP secret is shown once, as a QR code.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal

import httpx
import pyotp
import segno
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from api.auth import team
from api.auth.deps import revoke_access
from api.auth.tokens import Role
from api.channels.registry import telegram_client
from api.channels.telegram.client import (
    TOKEN_RE,
    BotInfo,
    TelegramAPIError,
    TelegramClient,
    bot_id_of,
)
from api.channels.telegram.router import TelegramRouter
from api.config import get_settings
from api.core.crypto import decrypt_secret, encrypt_secret
from api.core.logging import get_logger
from api.core.passwords import hash_password
from api.db.models import AuditLog, PlatformUser, Tenant, TenantChannel, TenantSettings, TenantUser
from api.meta.client import MetaAPIError, MetaClient
from api.modules import admin as modules_admin
from api.modules import registry
from api.modules.registry import ModuleError
from api.platform.auth import ROLES, OpsCtx, OwnerCtx, PlatformCtx, PlatformRole, StaffCtx
from api.scripts import dpa
from api.webhooks.router import TenantRouter

router = APIRouter(prefix="/api/v1/platform/admin", tags=["platform-admin"])
log = get_logger(__name__)

TOTP_ISSUER = "HMH Labz console"
UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT
TenantStatus = Literal["trial", "active", "suspended", "churned"]
# Sub-domains a client can never take: they are the platform's own hosts.
RESERVED_SLUGS = frozenset(
    {"api", "admin", "go", "status", "www", "app", "console", "mail", "help", "docs", "static"}
)
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    ),
]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
MetaId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{5,25}$")]
Money = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bad(code: str, http: int = status.HTTP_409_CONFLICT, **extra: Any) -> HTTPException:
    return HTTPException(http, {"code": code, **extra})


# ---------------------------------------------------------------- shapes


class ModuleInfo(BaseModel):
    key: str
    name: str
    description: str
    requires: list[str]


class Catalogue(BaseModel):
    modules: list[ModuleInfo]
    presets: dict[str, list[str]]
    tenant_roles: list[str]
    staff_roles: list[str]


class ChannelAdmin(BaseModel):
    id: uuid.UUID
    kind: str  # whatsapp | telegram
    phone_number_id: str | None  # WhatsApp only
    waba_id: str | None
    display_phone: str | None
    is_active: bool
    has_token: bool
    token_expires_at: datetime | None
    quality_rating: str | None
    # Telegram only. Never the token or the webhook secret.
    telegram_username: str | None = None
    webhook_set_at: datetime | None = None
    webhook_error: str | None = None


class UserAdmin(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class Profile(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    legal_name: str | None
    status: str
    contract_ref: str | None
    timezone: str
    locale_default: str
    service_start: date | None
    free_months_until: date | None
    meta_charges_borne_by_us_until: date | None
    monthly_fee_aed: str | None
    monthly_message_cap_aed: str | None
    escalation_phone: str | None
    contact_label: str | None
    created_at: datetime


class TenantAdmin(BaseModel):
    profile: Profile
    modules: list[str]
    channels: list[ChannelAdmin]
    users: list[UserAdmin]


class TenantFields(In):
    name: Text | None = None
    legal_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = (
        None
    )
    status: TenantStatus | None = None
    contract_ref: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] | None = (
        None
    )
    timezone: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_]+/[A-Za-z_]+$")] | None = None
    locale_default: Literal["en", "ar"] | None = None
    service_start: date | None = None
    free_months_until: date | None = None
    meta_charges_borne_by_us_until: date | None = None
    monthly_fee_aed: Money | None = None
    monthly_message_cap_aed: Money | None = None
    escalation_phone: Annotated[str, StringConstraints(pattern=r"^\d{8,15}$")] | None = None
    contact_label: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None
    ) = None


class TenantCreate(TenantFields):
    slug: str
    name: Text
    status: TenantStatus = "trial"
    modules: list[str] = Field(default_factory=list)  # module keys and/or preset names

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError("lowercase letters, digits and dashes; 1-40 characters")
        if v in RESERVED_SLUGS:
            raise ValueError("reserved for the platform")
        return v


class ModulesIn(In):
    enabled: list[str]


class ChannelCreate(In):
    phone_number_id: MetaId
    waba_id: MetaId | None = None
    display_phone: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=30)] | None
    ) = None


class ChannelPatch(In):
    waba_id: MetaId | None = None
    display_phone: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=30)] | None
    ) = None
    is_active: bool | None = None


class TokenIn(In):
    token: Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=1000)]
    expires: date | None = None


class TelegramTokenIn(In):
    # BotFather's "123456789:AA…" — the shape is checked again before any network call
    token: Annotated[str, StringConstraints(strip_whitespace=True, min_length=36, max_length=100)]


class UserCreate(In):
    email: Email
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    role: Role
    password: str = Field(min_length=12, max_length=256)  # temporary; they change it


class UserPatch(In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)


class StaffOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    role: str
    is_active: bool
    has_totp: bool
    created_at: datetime
    last_login_at: datetime | None


class StaffCreate(In):
    email: Email
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    role: PlatformRole
    password: str = Field(min_length=12, max_length=256)


class StaffPatch(In):
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    role: PlatformRole | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)


class Enrolment(BaseModel):
    """Shown once: the authenticator enrolment for a staff account."""

    staff: StaffOut
    otpauth_uri: str
    qr_svg: str  # data: URI


# ---------------------------------------------------------------- helpers


def _router(request: Request) -> TenantRouter:
    r: TenantRouter = request.app.state.router
    return r


def _tg_router(request: Request) -> TelegramRouter:
    r: TelegramRouter = request.app.state.telegram_router
    return r


def _money(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def _channel(ch: TenantChannel) -> ChannelAdmin:
    return ChannelAdmin(
        id=ch.id,
        kind=ch.kind,
        telegram_username=ch.telegram_username,
        webhook_set_at=ch.webhook_set_at,
        webhook_error=ch.webhook_error,
        phone_number_id=ch.phone_number_id,
        waba_id=ch.waba_id,
        display_phone=ch.display_phone,
        is_active=ch.is_active,
        has_token=ch.access_token_encrypted is not None,
        token_expires_at=ch.token_expires_at,
        quality_rating=ch.quality_rating,
    )


def _user(u: TenantUser) -> UserAdmin:
    return UserAdmin(
        id=u.id,
        email=u.email,
        name=u.name,
        role=u.role,
        is_active=u.is_active,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


def _staff(u: PlatformUser) -> StaffOut:
    return StaffOut(
        id=u.id,
        email=u.email,
        name=u.name,
        role=u.role,
        is_active=u.is_active,
        has_totp=u.totp_secret is not None,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


def _enrolment(u: PlatformUser, secret: str) -> Enrolment:
    uri = pyotp.TOTP(secret).provisioning_uri(name=u.email, issuer_name=TOTP_ISSUER)
    qr = segno.make(uri, error="m").svg_data_uri(scale=6, border=2, dark="#141614")
    return Enrolment(staff=_staff(u), otpauth_uri=uri, qr_svg=qr)


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = (
            v.isoformat()
            if isinstance(v, (date, datetime))
            else (str(v) if isinstance(v, Decimal) else v)
        )
    return out


async def _audit(
    ctx: PlatformCtx,
    tenant_id: uuid.UUID,
    action: str,
    entity: str,
    entity_id: uuid.UUID | None = None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    async with ctx.db.tenant_session(tenant_id) as s:
        s.add(
            AuditLog(
                actor=ctx.staff.actor,
                action=action,
                entity=entity,
                entity_id=entity_id,
                before=_jsonable(before) if before else None,
                after=_jsonable(after) if after else None,
            )
        )


async def _tenant_admin(ctx: PlatformCtx, tenant_id: uuid.UUID) -> TenantAdmin:
    async with ctx.db.platform_session() as s:
        t = await s.get(Tenant, tenant_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
        ts = await s.get(TenantSettings, tenant_id)
        channels = (
            await s.scalars(
                select(TenantChannel)
                .where(TenantChannel.tenant_id == tenant_id)
                .order_by(TenantChannel.kind.desc(), TenantChannel.phone_number_id)
            )
        ).all()
        users = (
            await s.scalars(
                select(TenantUser)
                .where(TenantUser.tenant_id == tenant_id)
                .order_by(TenantUser.email)
            )
        ).all()
        modules = (await registry.enabled_for(s, tenant_id)).keys
        profile = Profile(
            id=t.id,
            slug=t.slug,
            name=t.name,
            legal_name=t.legal_name,
            status=t.status,
            contract_ref=t.contract_ref,
            timezone=t.timezone,
            locale_default=t.locale_default,
            service_start=t.service_start,
            free_months_until=t.free_months_until,
            meta_charges_borne_by_us_until=t.meta_charges_borne_by_us_until,
            monthly_fee_aed=_money(t.monthly_fee_aed),
            monthly_message_cap_aed=_money(ts.monthly_message_cap_aed if ts else None),
            escalation_phone=ts.escalation_phone if ts else None,
            contact_label=ts.contact_label if ts else None,
            created_at=t.created_at,
        )
        return TenantAdmin(
            profile=profile,
            modules=list(modules),
            channels=[_channel(c) for c in channels],
            users=[_user(u) for u in users],
        )


TENANT_COLUMNS = (
    "name",
    "legal_name",
    "status",
    "contract_ref",
    "timezone",
    "locale_default",
    "service_start",
    "free_months_until",
    "meta_charges_borne_by_us_until",
    "monthly_fee_aed",
)
SETTINGS_COLUMNS = ("monthly_message_cap_aed", "escalation_phone", "contact_label")


def _apply(
    t: Tenant, ts: TenantSettings, changes: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for k, v in changes.items():
        target: Any = t if k in TENANT_COLUMNS else ts if k in SETTINGS_COLUMNS else None
        if target is None:
            continue
        old = getattr(target, k)
        if old != v:
            before[k], after[k] = old, v
            setattr(target, k, v)
    return before, after


async def _invalidate_channels(request: Request, ctx: PlatformCtx, tenant_id: uuid.UUID) -> None:
    async with ctx.db.platform_session() as s:
        rows = (
            await s.execute(
                select(
                    TenantChannel.phone_number_id, TenantChannel.waba_id, TenantChannel.channel_key
                ).where(TenantChannel.tenant_id == tenant_id)
            )
        ).all()
    for pid, waba, key in rows:
        await _router(request).invalidate(phone_number_id=pid, waba_id=waba)
        await _tg_router(request).invalidate(key)


# ---------------------------------------------------------------- reference


@router.get("/catalogue", response_model=Catalogue)
async def catalogue(ctx: StaffCtx) -> Catalogue:
    return Catalogue(
        modules=[
            ModuleInfo(key=m.key, name=m.name, description=m.description, requires=list(m.requires))
            for m in registry.all_modules().values()
        ],
        presets={k: list(v) for k, v in registry.PRESETS.items()},
        tenant_roles=["admin", "agent", "viewer"],
        staff_roles=list(ROLES),
    )


# ---------------------------------------------------------------- clients


@router.post("/tenants", response_model=TenantAdmin, status_code=status.HTTP_201_CREATED)
async def create_tenant(ctx: OpsCtx, body: TenantCreate) -> TenantAdmin:
    fields = body.model_dump(exclude={"slug", "modules"}, exclude_none=True)
    try:
        async with ctx.db.platform_session() as s:
            if await s.scalar(select(Tenant.id).where(Tenant.slug == body.slug)) is not None:
                raise _bad("slug_taken")
            t = Tenant(slug=body.slug, name=body.name, status=body.status)
            s.add(t)
            await s.flush()
            ts = TenantSettings(tenant_id=t.id)
            s.add(ts)
            await s.flush()
            _apply(t, ts, fields)
            if body.modules:
                await modules_admin.enable(s, t.id, body.modules)
            tenant_id = t.id
    except ModuleError as exc:
        raise _bad("modules", UNPROCESSABLE, message=str(exc)) from exc
    await _audit(
        ctx,
        tenant_id,
        "create_tenant",
        "tenant",
        tenant_id,
        after={"slug": body.slug, **fields, "modules": body.modules},
    )
    log.info("tenant_created", tenant_id=str(tenant_id), slug=body.slug, by=ctx.staff.actor)
    return await _tenant_admin(ctx, tenant_id)


@router.get("/tenants/{tenant_id}", response_model=TenantAdmin)
async def get_tenant(ctx: StaffCtx, tenant_id: uuid.UUID) -> TenantAdmin:
    return await _tenant_admin(ctx, tenant_id)


@router.patch("/tenants/{tenant_id}", response_model=TenantAdmin)
async def update_tenant(
    ctx: OpsCtx, request: Request, tenant_id: uuid.UUID, body: TenantFields
) -> TenantAdmin:
    changes = body.model_dump(exclude_unset=True)
    for k in ("name", "status", "timezone", "locale_default"):  # NOT NULL columns
        if k in changes and changes[k] is None:
            raise _bad("required", UNPROCESSABLE, field=k)
    async with ctx.db.platform_session() as s:
        t = await s.get(Tenant, tenant_id, with_for_update=True)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
        ts = await s.get(TenantSettings, tenant_id)
        if ts is None:
            ts = TenantSettings(tenant_id=tenant_id)
            s.add(ts)
            await s.flush()
        before, after = _apply(t, ts, changes)
    if "status" in after:  # webhook routing caches the tenant's status
        await _invalidate_channels(request, ctx, tenant_id)
    if after:
        await _audit(
            ctx, tenant_id, "update_tenant", "tenant", tenant_id, before=before, after=after
        )
    return await _tenant_admin(ctx, tenant_id)


@router.put("/tenants/{tenant_id}/modules", response_model=TenantAdmin)
async def set_modules(ctx: OpsCtx, tenant_id: uuid.UUID, body: ModulesIn) -> TenantAdmin:
    try:
        wanted = set(registry.expand(body.enabled))
        async with ctx.db.platform_session() as s:
            if await s.get(Tenant, tenant_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
            current = set((await registry.enabled_for(s, tenant_id)).keys)
            order = list(registry.all_modules())
            on = sorted(wanted - current, key=order.index)
            off = sorted(current - wanted, key=order.index, reverse=True)
            if off:  # dependents first, so "x still needs y" only fires for real conflicts
                await modules_admin.disable(s, tenant_id, off)
            if on:
                await modules_admin.enable(s, tenant_id, on)
    except ModuleError as exc:
        raise _bad("modules", message=str(exc)) from exc
    if on or off:
        await _audit(
            ctx,
            tenant_id,
            "set_modules",
            "tenant_module",
            before={"enabled": sorted(current)},
            after={"enabled": sorted(wanted)},
        )
    return await _tenant_admin(ctx, tenant_id)


@router.get("/tenants/{tenant_id}/dpa")
async def download_dpa(ctx: StaffCtx, tenant_id: uuid.UUID) -> Response:
    async with ctx.db.platform_session() as s:
        slug = await s.scalar(select(Tenant.slug).where(Tenant.id == tenant_id))
    if slug is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    text = await dpa.render(slug, ctx.db)
    return Response(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "content-disposition": f'attachment; filename="dpa-{slug}.md"',
            "cache-control": "no-store",
        },
    )


# ---------------------------------------------------------------- WhatsApp numbers


@router.post(
    "/tenants/{tenant_id}/channels", response_model=TenantAdmin, status_code=status.HTTP_201_CREATED
)
async def add_channel(
    ctx: OpsCtx, request: Request, tenant_id: uuid.UUID, body: ChannelCreate
) -> TenantAdmin:
    async with ctx.db.platform_session() as s:
        if await s.get(Tenant, tenant_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
        taken = await s.scalar(
            select(TenantChannel.tenant_id).where(
                TenantChannel.phone_number_id == body.phone_number_id
            )
        )
        if taken is not None:  # never reroute a number that is live for someone
            raise _bad("phone_number_taken", same_tenant=taken == tenant_id)
        ch = TenantChannel(
            tenant_id=tenant_id,
            phone_number_id=body.phone_number_id,
            waba_id=body.waba_id,
            display_phone=body.display_phone,
        )
        s.add(ch)
        await s.flush()
        channel_id = ch.id
    await _router(request).invalidate(phone_number_id=body.phone_number_id, waba_id=body.waba_id)
    await _audit(
        ctx, tenant_id, "add_channel", "tenant_channel", channel_id, after=body.model_dump()
    )
    return await _tenant_admin(ctx, tenant_id)


async def _load_channel(ctx: PlatformCtx, channel_id: uuid.UUID) -> TenantChannel:
    async with ctx.db.platform_session() as s:
        ch = await s.get(TenantChannel, channel_id)
    if ch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
    return ch


@router.patch("/channels/{channel_id}", response_model=TenantAdmin)
async def update_channel(
    ctx: OpsCtx, request: Request, channel_id: uuid.UUID, body: ChannelPatch
) -> TenantAdmin:
    changes = body.model_dump(exclude_unset=True)
    if "is_active" in changes and changes["is_active"] is None:
        raise _bad("required", UNPROCESSABLE, field="is_active")
    current = await _load_channel(ctx, channel_id)
    if current.kind == "telegram":
        if set(changes) - {"is_active"}:
            raise _bad("not_whatsapp", UNPROCESSABLE)
        if "is_active" in changes and changes["is_active"] != current.is_active:
            return await _set_bot_active(ctx, request, current, bool(changes["is_active"]))
        return await _tenant_admin(ctx, current.tenant_id)
    async with ctx.db.platform_session() as s:
        ch = await s.get(TenantChannel, channel_id, with_for_update=True)
        if ch is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
        old_waba = ch.waba_id
        before = {k: getattr(ch, k) for k in changes}
        for k, v in changes.items():
            setattr(ch, k, v)
        tenant_id, pid, waba = ch.tenant_id, ch.phone_number_id, ch.waba_id
    await _router(request).invalidate(phone_number_id=pid, waba_id=waba)
    if old_waba and old_waba != waba:
        await _router(request).invalidate(waba_id=old_waba)
    if changes:
        await _audit(
            ctx, tenant_id, "update_channel", "tenant_channel", channel_id,
            before=before, after=changes,
        )  # fmt: skip
    return await _tenant_admin(ctx, tenant_id)


@router.put("/channels/{channel_id}/token", response_model=TenantAdmin)
async def set_token(
    ctx: OpsCtx, request: Request, channel_id: uuid.UUID, body: TokenIn
) -> TenantAdmin:
    ch = await _load_channel(ctx, channel_id)
    if ch.kind != "whatsapp" or ch.phone_number_id is None:
        raise _bad("not_whatsapp")
    settings = get_settings()
    http: httpx.AsyncClient = request.app.state.http
    client = MetaClient(
        http=http,
        access_token=body.token,
        phone_number_id=ch.phone_number_id,
        api_version=settings.meta_graph_api_version,
        base_url=settings.meta_graph_base_url,
    )
    try:  # a token Meta rejects is never stored
        meta = await client.phone_status()
    except MetaAPIError as exc:
        raise _bad("meta_rejected", UNPROCESSABLE, message=str(exc)) from exc
    expires = datetime.combine(body.expires, time(0), UTC) if body.expires else None
    async with ctx.db.platform_session() as s:
        row = await s.get(TenantChannel, channel_id, with_for_update=True)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
        row.access_token_encrypted = encrypt_secret(body.token)
        row.token_expires_at = expires
        if meta.quality_rating:
            row.quality_rating = meta.quality_rating
    await _audit(
        ctx,
        ch.tenant_id,
        "set_channel_token",
        "tenant_channel",
        channel_id,
        after={"expires": body.expires, "quality": meta.quality_rating},  # never the token
    )
    log.info("channel_token_set", channel_id=str(channel_id), by=ctx.staff.actor)
    return await _tenant_admin(ctx, ch.tenant_id)


# ---------------------------------------------------------------- Telegram bots

TELEGRAM_UPDATES = ["message", "my_chat_member"]


async def _check_bot(request: Request, token: str) -> tuple[TelegramClient, BotInfo]:
    """A token Telegram does not accept is never stored."""
    if not TOKEN_RE.match(token):
        raise _bad("telegram_token_invalid", UNPROCESSABLE)
    client = telegram_client(token, request.app.state.http, get_settings())
    try:
        bot = await client.get_me()
    except TelegramAPIError as exc:
        raise _bad("telegram_rejected", UNPROCESSABLE, message=exc.description) from None
    if not bot.is_bot or str(bot.id) != bot_id_of(token):
        raise _bad("telegram_rejected", UNPROCESSABLE, message="not a bot token")
    return client, bot


async def _register_webhook(client: TelegramClient, channel_key: str) -> tuple[bytes, str | None]:
    """Point the bot at /webhook/telegram/<channel_key> with a NEW secret_token. Returns the
    secret's sha256 (all we keep) and an error if Telegram did not take the URL."""
    settings = get_settings()
    if not settings.public_api_base_url:
        raise _bad("public_api_base_url_missing", status.HTTP_503_SERVICE_UNAVAILABLE)
    url = f"{settings.public_api_base_url.rstrip('/')}/webhook/telegram/{channel_key}"
    secret = secrets.token_urlsafe(48)
    try:
        await client.set_webhook(
            url,
            secret,
            allowed_updates=TELEGRAM_UPDATES,
            max_connections=settings.telegram_webhook_max_connections,
        )
        info = await client.get_webhook_info()
    except TelegramAPIError as exc:
        raise _bad("telegram_rejected", UNPROCESSABLE, message=exc.description) from None
    error = None if info.url == url else "Telegram did not keep the webhook URL"
    return hashlib.sha256(secret.encode()).digest(), error


async def _stored_client(request: Request, ch: TenantChannel) -> TelegramClient:
    if ch.kind != "telegram" or ch.access_token_encrypted is None:
        raise _bad("not_telegram")
    return telegram_client(
        decrypt_secret(ch.access_token_encrypted), request.app.state.http, get_settings()
    )


async def _set_bot_active(
    ctx: PlatformCtx, request: Request, ch: TenantChannel, active: bool
) -> TenantAdmin:
    """Deactivating deletes the webhook (Telegram would otherwise retry into a 404 for a day);
    re-activating registers it again with a new secret."""
    client = await _stored_client(request, ch)
    secret_hash: bytes | None = None
    error: str | None = None
    if active:
        secret_hash, error = await _register_webhook(client, ch.channel_key)
    else:
        try:
            await client.delete_webhook()
        except TelegramAPIError as exc:
            error = f"deleteWebhook failed: {exc.description}"
            log.warning("telegram_delete_webhook_failed", channel_id=str(ch.id))
    async with ctx.db.platform_session() as s:
        row = await s.get(TenantChannel, ch.id, with_for_update=True)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
        row.is_active = active
        row.webhook_secret_hash = secret_hash
        row.webhook_set_at = datetime.now(UTC) if active else None
        row.webhook_error = error
    await _tg_router(request).invalidate(ch.channel_key)
    await _audit(
        ctx, ch.tenant_id, "update_channel", "tenant_channel", ch.id,
        before={"is_active": ch.is_active}, after={"is_active": active},
    )  # fmt: skip
    return await _tenant_admin(ctx, ch.tenant_id)


@router.post(
    "/tenants/{tenant_id}/telegram", response_model=TenantAdmin, status_code=status.HTTP_201_CREATED
)
async def add_telegram_bot(
    ctx: OpsCtx, request: Request, tenant_id: uuid.UUID, body: TelegramTokenIn
) -> TenantAdmin:
    async with ctx.db.platform_session() as s:
        if await s.get(Tenant, tenant_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    client, bot = await _check_bot(request, body.token)
    bot_id = str(bot.id)
    async with ctx.db.platform_session() as s:
        taken = await s.scalar(
            select(TenantChannel.tenant_id).where(TenantChannel.telegram_bot_id == bot_id)
        )
    if taken is not None:  # never reroute a bot that is live for someone
        raise _bad("bot_taken", same_tenant=taken == tenant_id)

    channel_key = secrets.token_urlsafe(24)
    secret_hash, error = await _register_webhook(client, channel_key)
    try:
        async with ctx.db.platform_session() as s:
            ch = TenantChannel(
                tenant_id=tenant_id,
                kind="telegram",
                channel_key=channel_key,
                telegram_bot_id=bot_id,
                telegram_username=bot.username,
                access_token_encrypted=encrypt_secret(body.token),
                webhook_secret_hash=secret_hash,
                webhook_set_at=datetime.now(UTC),
                webhook_error=error,
            )
            s.add(ch)
            await s.flush()
            channel_id = ch.id
    except IntegrityError:  # added twice at once: the other request won
        try:
            await client.delete_webhook()
        except TelegramAPIError:
            log.warning("telegram_delete_webhook_failed", bot_id=bot_id)
        raise _bad("bot_taken", same_tenant=True) from None
    await _audit(
        ctx, tenant_id, "add_telegram_bot", "tenant_channel", channel_id,
        after={"bot_id": bot_id, "bot_username": bot.username},  # never the token
    )  # fmt: skip
    log.info("telegram_bot_added", channel_id=str(channel_id), bot_id=bot_id, by=ctx.staff.actor)
    return await _tenant_admin(ctx, tenant_id)


@router.put("/channels/{channel_id}/telegram-token", response_model=TenantAdmin)
async def replace_telegram_token(
    ctx: OpsCtx, request: Request, channel_id: uuid.UUID, body: TelegramTokenIn
) -> TenantAdmin:
    """After /revoke in BotFather. Must be the same bot; the webhook gets a new secret."""
    ch = await _load_channel(ctx, channel_id)
    if ch.kind != "telegram":
        raise _bad("not_telegram")
    client, bot = await _check_bot(request, body.token)
    if str(bot.id) != ch.telegram_bot_id:
        raise _bad("different_bot")
    secret_hash: bytes | None = None
    error: str | None = None
    if ch.is_active:
        secret_hash, error = await _register_webhook(client, ch.channel_key)
    async with ctx.db.platform_session() as s:
        row = await s.get(TenantChannel, channel_id, with_for_update=True)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
        row.access_token_encrypted = encrypt_secret(body.token)
        row.telegram_username = bot.username
        if ch.is_active:
            row.webhook_secret_hash = secret_hash
            row.webhook_set_at = datetime.now(UTC)
            row.webhook_error = error
    await _tg_router(request).invalidate(ch.channel_key)
    await _audit(
        ctx, ch.tenant_id, "set_telegram_token", "tenant_channel", channel_id,
        after={"bot_id": ch.telegram_bot_id, "bot_username": bot.username},
    )  # fmt: skip
    log.info("telegram_token_set", channel_id=str(channel_id), by=ctx.staff.actor)
    return await _tenant_admin(ctx, ch.tenant_id)


@router.post("/channels/{channel_id}/telegram-webhook", response_model=TenantAdmin)
async def reregister_telegram_webhook(
    ctx: OpsCtx, request: Request, channel_id: uuid.UUID
) -> TenantAdmin:
    """Repair: register the webhook again (new secret), e.g. after the domain changed."""
    ch = await _load_channel(ctx, channel_id)
    if not ch.is_active:
        raise _bad("channel_inactive")
    client = await _stored_client(request, ch)
    secret_hash, error = await _register_webhook(client, ch.channel_key)
    async with ctx.db.platform_session() as s:
        row = await s.get(TenantChannel, channel_id, with_for_update=True)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
        row.webhook_secret_hash = secret_hash
        row.webhook_set_at = datetime.now(UTC)
        row.webhook_error = error
    await _tg_router(request).invalidate(ch.channel_key)
    await _audit(ctx, ch.tenant_id, "register_telegram_webhook", "tenant_channel", channel_id)
    return await _tenant_admin(ctx, ch.tenant_id)


# ---------------------------------------------------------------- client dashboard users


@router.post(
    "/tenants/{tenant_id}/users", response_model=TenantAdmin, status_code=status.HTTP_201_CREATED
)
async def add_user(ctx: OpsCtx, tenant_id: uuid.UUID, body: UserCreate) -> TenantAdmin:
    password_hash = await asyncio.to_thread(hash_password, body.password)
    async with ctx.db.platform_session() as s:
        if await s.get(Tenant, tenant_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
        try:
            u = await team.add_user(
                s, tenant_id, email=body.email, name=body.name, role=body.role,
                password_hash=password_hash,
            )  # fmt: skip
        except team.TeamError as exc:
            raise _bad(exc.code) from exc
        user_id = u.id
    await _audit(ctx, tenant_id, "add_member", "tenant_user", user_id, after={"role": body.role})
    return await _tenant_admin(ctx, tenant_id)


@router.patch("/tenants/{tenant_id}/users/{user_id}", response_model=TenantAdmin)
async def update_user(
    ctx: OpsCtx, tenant_id: uuid.UUID, user_id: uuid.UUID, body: UserPatch
) -> TenantAdmin:
    changes = body.model_dump(exclude_unset=True)
    password = changes.pop("password", None)
    for k in ("role", "is_active"):
        if k in changes and changes[k] is None:
            raise _bad("required", UNPROCESSABLE, field=k)
    new_hash = await asyncio.to_thread(hash_password, password) if password else None
    async with ctx.db.platform_session() as s:
        try:
            done = await team.update_user(s, tenant_id, user_id, changes, new_hash=new_hash)
        except team.TeamError as exc:
            if exc.code == "not_found":
                raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found") from exc
            raise _bad(exc.code) from exc
    if done.access_changed:
        await revoke_access(ctx.redis, get_settings(), user_id)
    after = dict(changes) | ({"password": "reset"} if new_hash else {})
    if after:
        await _audit(
            ctx, tenant_id, "update_member", "tenant_user", user_id,
            before=done.before, after=after,
        )  # fmt: skip
    return await _tenant_admin(ctx, tenant_id)


# ---------------------------------------------------------------- HMH Labz staff (owner only)


@router.get("/staff", response_model=list[StaffOut])
async def list_staff(ctx: OwnerCtx) -> list[StaffOut]:
    async with ctx.db.platform_session() as s:
        rows = (await s.scalars(select(PlatformUser).order_by(PlatformUser.email))).all()
    return [_staff(u) for u in rows]


@router.post("/staff", response_model=Enrolment, status_code=status.HTTP_201_CREATED)
async def add_staff(ctx: OwnerCtx, body: StaffCreate) -> Enrolment:
    email = body.email.lower()
    password_hash = await asyncio.to_thread(hash_password, body.password)
    secret = pyotp.random_base32()
    async with ctx.db.platform_session() as s:
        exists = await s.scalar(
            select(PlatformUser.id).where(func.lower(PlatformUser.email) == email)
        )
        if exists is not None:
            raise _bad("email_exists")
        u = PlatformUser(
            email=email, name=body.name, role=body.role, password_hash=password_hash,
            totp_secret=secret,
        )  # fmt: skip
        s.add(u)
        await s.flush()
        await s.refresh(u)
    log.info("staff_added", staff_id=str(u.id), role=body.role, by=ctx.staff.actor)
    return _enrolment(u, secret)


async def _owners_left(s: Any, excluding: uuid.UUID) -> int:
    n = await s.scalar(
        select(func.count())
        .select_from(PlatformUser)
        .where(
            PlatformUser.role == "owner",
            PlatformUser.is_active.is_(True),
            PlatformUser.id != excluding,
        )
    )
    return int(n or 0)


@router.patch("/staff/{staff_id}", response_model=StaffOut)
async def update_staff(ctx: OwnerCtx, staff_id: uuid.UUID, body: StaffPatch) -> StaffOut:
    changes = body.model_dump(exclude_unset=True)
    password = changes.pop("password", None)
    for k in ("role", "is_active"):
        if k in changes and changes[k] is None:
            raise _bad("required", UNPROCESSABLE, field=k)
    new_hash = await asyncio.to_thread(hash_password, password) if password else None
    async with ctx.db.platform_session() as s:
        u = await s.get(PlatformUser, staff_id, with_for_update=True)
        if u is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "staff not found")
        losing_owner = (
            u.role == "owner"
            and u.is_active
            and (changes.get("role", "owner") != "owner" or changes.get("is_active") is False)
        )
        if losing_owner and staff_id == ctx.staff.user_id:
            raise _bad("not_yourself")  # another owner has to do it
        if losing_owner and await _owners_left(s, staff_id) == 0:
            raise _bad("last_owner")
        for k, v in changes.items():
            setattr(u, k, v)
        if new_hash is not None:
            u.password_hash = new_hash
        await s.flush()
        out = _staff(u)
    log.info(
        "staff_updated",
        staff_id=str(staff_id),
        fields=sorted(changes) + (["password"] if new_hash else []),
        by=ctx.staff.actor,
    )
    return out


@router.post("/staff/{staff_id}/totp", response_model=Enrolment)
async def reset_staff_totp(ctx: OwnerCtx, staff_id: uuid.UUID) -> Enrolment:
    secret = pyotp.random_base32()
    async with ctx.db.platform_session() as s:
        u = await s.get(PlatformUser, staff_id, with_for_update=True)
        if u is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "staff not found")
        u.totp_secret = secret
        await s.flush()
    log.info("staff_totp_reset", staff_id=str(staff_id), by=ctx.staff.actor)
    return _enrolment(u, secret)

"""/api/v1/auth — login, refresh, logout, me, password.

Login is the only place a tenant is chosen, and it is chosen by the credential: the email and
password must match an active login of an active tenant. If the same email+password is valid for
more than one tenant, the client is asked to pick (409 choose_tenant) and sends the slug back —
the slug only narrows candidates whose password already verified, it grants nothing on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update

from api.auth.deps import Ctx, Viewer, revoke_access, unauthorized
from api.auth.tokens import (
    REFRESH_COOKIE,
    REFRESH_COOKIE_PATH,
    Role,
    hash_refresh_token,
    issue_access,
    new_refresh_token,
)
from api.config import Settings, get_settings
from api.core.logging import get_logger
from api.core.passwords import hash_password, verify_password
from api.db.models import AuthRefreshToken, Tenant, TenantUser
from api.db.session import Database
from api.deps import get_database, get_redis

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = get_logger(__name__)

LIVE_TENANT_STATUSES = ("trial", "active")
# Two tabs refreshing with the same cookie at once is normal, not theft: a rotated token presented
# again within this window gets a fresh sibling instead of revoking the family.
REUSE_GRACE = timedelta(seconds=30)
CSRF_HEADER = "x-hmh-csrf"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginIn(_In):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    tenant: str | None = Field(default=None, max_length=80)  # slug, only after a 409


class PasswordIn(_In):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    role: Role


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    meta_charges_borne_by_us_until: str | None


class SessionOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — the OAuth scheme name, not a secret
    expires_at: datetime
    user: UserOut
    tenant: TenantOut


class MeOut(BaseModel):
    user: UserOut
    tenant: TenantOut


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Verified against when no login matches, so an unknown email costs the same as a wrong
    password (no account enumeration by timing)."""
    return hash_password(uuid.uuid4().hex)


def _fail_key(email: str) -> str:
    return "auth:fail:" + hashlib.sha256(email.encode()).hexdigest()[:32]


def _settings_or_503() -> Settings:
    settings = get_settings()
    if settings.jwt_secret is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "auth not configured")
    return settings


def _require_csrf(value: str | None) -> None:
    # The refresh cookie is SameSite=Strict already; a custom header also rules out a plain
    # cross-site form post, which cannot set headers.
    if value != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-HMH-CSRF header")


def _set_cookie(response: Response, raw: str, settings: Settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=settings.jwt_refresh_ttl_s,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, secure=True, httponly=True, samesite="strict"
    )


def _user_out(u: TenantUser) -> UserOut:
    return UserOut(id=u.id, email=u.email, name=u.name, role=cast(Role, u.role))


def _tenant_out(t: Tenant) -> TenantOut:
    borne = t.meta_charges_borne_by_us_until
    return TenantOut(
        id=t.id,
        name=t.name,
        slug=t.slug,
        timezone=t.timezone,
        meta_charges_borne_by_us_until=borne.isoformat() if borne else None,
    )


def _refresh_row(
    settings: Settings, user: TenantUser, now: datetime, family_id: uuid.UUID | None = None
) -> tuple[AuthRefreshToken, str]:
    raw, digest = new_refresh_token()
    row = AuthRefreshToken(
        id=uuid.uuid4(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        token_hash=digest,
        expires_at=now + timedelta(seconds=settings.jwt_refresh_ttl_s),
    )
    return row, raw


def _session_out(
    settings: Settings,
    response: Response,
    user: TenantUser,
    tenant: Tenant,
    raw: str,
    now: datetime,
) -> SessionOut:
    access, exp = issue_access(
        settings, user_id=user.id, tenant_id=tenant.id, role=cast(Role, user.role), now=now
    )
    _set_cookie(response, raw, settings)
    return SessionOut(
        access_token=access, expires_at=exp, user=_user_out(user), tenant=_tenant_out(tenant)
    )


async def _new_session(
    db: Database, settings: Settings, response: Response, user: TenantUser, tenant: Tenant
) -> SessionOut:
    now = datetime.now(UTC)
    row, raw = _refresh_row(settings, user, now)
    async with db.platform_session() as s:
        s.add(row)
    return _session_out(settings, response, user, tenant, raw, now)


async def _bump_failures(redis: Redis, key: str, window_s: int) -> None:
    try:
        async with redis.pipeline(transaction=True) as p:
            p.incr(key)
            p.expire(key, window_s, nx=True)
            await p.execute()
    except (RedisError, OSError) as exc:
        log.warning("auth_failure_counter_unavailable", error=type(exc).__name__)


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginIn,
    response: Response,
    db: Annotated[Database, Depends(get_database)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> SessionOut:
    settings = _settings_or_503()
    email = body.email.lower()
    fail_key = _fail_key(email)
    try:
        failures = int(await redis.get(fail_key) or 0)
    except (RedisError, OSError):
        failures = 0
    if failures >= settings.login_max_failures:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts — try again in a few minutes"
        )

    async with db.platform_session() as s:
        rows = (
            await s.execute(
                select(TenantUser, Tenant)
                .join(Tenant, Tenant.id == TenantUser.tenant_id)
                .where(
                    func.lower(TenantUser.email) == email,
                    TenantUser.is_active.is_(True),
                    Tenant.status.in_(LIVE_TENANT_STATUSES),
                )
            )
        ).all()

    matches: list[tuple[TenantUser, Tenant]] = []
    for user, tenant in rows:
        if await asyncio.to_thread(verify_password, body.password, user.password_hash):
            matches.append((user, tenant))
    if not rows:
        await asyncio.to_thread(verify_password, body.password, _dummy_hash())
    if body.tenant:
        matches = [(u, t) for u, t in matches if t.slug == body.tenant]

    if not matches:
        await _bump_failures(redis, fail_key, settings.login_failure_window_s)
        log.info("login_failed")
        raise unauthorized("invalid email or password")
    if len(matches) > 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "choose_tenant",
                "tenants": [{"slug": t.slug, "name": t.name} for _, t in matches],
            },
        )

    user, tenant = matches[0]
    with contextlib.suppress(RedisError, OSError):
        await redis.delete(fail_key)
    async with db.platform_session() as s:
        await s.execute(
            update(TenantUser)
            .where(TenantUser.id == user.id, TenantUser.tenant_id == tenant.id)
            .values(last_login_at=func.now())
        )
    out = await _new_session(db, settings, response, user, tenant)
    log.info("login", tenant_id=str(tenant.id), user_id=str(user.id), role=user.role)
    return out


@router.post("/refresh", response_model=SessionOut)
async def refresh(
    request: Request,
    response: Response,
    db: Annotated[Database, Depends(get_database)],
    x_hmh_csrf: Annotated[str | None, Header()] = None,
) -> SessionOut:
    settings = _settings_or_503()
    _require_csrf(x_hmh_csrf)
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise unauthorized("no session")
    now = datetime.now(UTC)
    digest = hash_refresh_token(raw)

    # One transaction: lock the presented token, validate, insert its successor and mark it
    # replaced — so a concurrent refresh with the same cookie sees a consistent rotated state.
    out: SessionOut | None = None
    async with db.platform_session() as s:
        tok = await s.scalar(
            select(AuthRefreshToken).where(AuthRefreshToken.token_hash == digest).with_for_update()
        )
        state = "missing" if tok is None else _token_state(tok, now)
        if tok is not None and state == "reused":
            # a rotated token came back after the grace window: assume it was stolen
            await s.execute(
                update(AuthRefreshToken)
                .where(
                    AuthRefreshToken.family_id == tok.family_id,
                    AuthRefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            log.warning(
                "refresh_token_reuse", tenant_id=str(tok.tenant_id), user_id=str(tok.user_id)
            )
        if tok is not None and state in ("live", "grace"):
            user = await s.get(TenantUser, tok.user_id)
            tenant = await s.get(Tenant, tok.tenant_id)
            if (
                user is not None
                and tenant is not None
                and user.tenant_id == tok.tenant_id
                and user.is_active
                and tenant.status in LIVE_TENANT_STATUSES
            ):
                row, new_raw = _refresh_row(settings, user, now, tok.family_id)
                s.add(row)
                if state == "live":
                    tok.revoked_at = now
                    tok.replaced_by = row.id
                out = _session_out(settings, response, user, tenant, new_raw, now)
    if out is None:
        _clear_cookie(response)
        raise unauthorized("session ended")
    return out


def _token_state(tok: AuthRefreshToken, now: datetime) -> str:
    if tok.expires_at <= now:
        return "expired"
    if tok.revoked_at is None:
        return "live"
    if tok.replaced_by is None:
        return "revoked"  # logged out / password changed / family revoked
    return "grace" if now - tok.revoked_at <= REUSE_GRACE else "reused"


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: Annotated[Database, Depends(get_database)],
    x_hmh_csrf: Annotated[str | None, Header()] = None,
) -> Response:
    _require_csrf(x_hmh_csrf)
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        async with db.platform_session() as s:
            family = await s.scalar(
                select(AuthRefreshToken.family_id).where(
                    AuthRefreshToken.token_hash == hash_refresh_token(raw)
                )
            )
            if family is not None:
                await s.execute(
                    update(AuthRefreshToken)
                    .where(
                        AuthRefreshToken.family_id == family,
                        AuthRefreshToken.revoked_at.is_(None),
                    )
                    .values(revoked_at=func.now())
                )
    response.status_code = status.HTTP_204_NO_CONTENT
    _clear_cookie(response)
    return response


async def _load_self(ctx: Ctx) -> tuple[TenantUser, Tenant]:
    async with ctx.platform() as s:
        user = await s.scalar(
            select(TenantUser).where(
                TenantUser.id == ctx.principal.user_id, TenantUser.tenant_id == ctx.tenant_id
            )
        )
        tenant = await s.get(Tenant, ctx.tenant_id)
    if user is None or tenant is None or not user.is_active:
        raise unauthorized("session ended")
    return user, tenant


@router.get("/me", response_model=MeOut)
async def me(ctx: Viewer) -> MeOut:
    user, tenant = await _load_self(ctx)
    return MeOut(user=_user_out(user), tenant=_tenant_out(tenant))


@router.post("/password", response_model=SessionOut)
async def change_password(body: PasswordIn, response: Response, ctx: Viewer) -> SessionOut:
    """Change your own password. Ends every other session of this login (all refresh tokens and
    access tokens issued so far) and returns a fresh one for this device."""
    settings = _settings_or_503()
    user, tenant = await _load_self(ctx)
    if not await asyncio.to_thread(verify_password, body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "current password is wrong")
    new_hash = await asyncio.to_thread(hash_password, body.new_password)
    async with ctx.platform() as s:
        await s.execute(
            update(TenantUser)
            .where(TenantUser.id == user.id, TenantUser.tenant_id == ctx.tenant_id)
            .values(password_hash=new_hash)
        )
        await s.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.user_id == user.id, AuthRefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
    await revoke_access(ctx.redis, settings, user.id)
    out = await _new_session(ctx.db, settings, response, user, tenant)
    log.info("password_changed", tenant_id=str(ctx.tenant_id), user_id=str(user.id))
    return out

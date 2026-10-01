"""Platform console sign-in for HMH Labz staff (platform_users) — separate from tenant logins.

The console sees every tenant, so it asks for more than a tenant login does:
* email + password + a TOTP code (authenticator app). An account without a TOTP secret cannot
  sign in at all; `python -m api.scripts.platform_users add` sets one up.
* A TOTP code is accepted once (the last used time-step is remembered in Redis).
* Access token: HS256 JWT, typ "platform", audience "hmh-console", 15 minutes. It carries no
  tenant. A tenant token can never pass for a platform token or the other way round (typ + aud).
* Refresh: random token in an HttpOnly, Secure, SameSite=Strict cookie scoped to
  /api/v1/platform/auth; sha256 of it in Redis, rotated on every use, with an absolute sign-in
  lifetime (platform_session_ttl_s) that rotation never extends.
* Every request re-reads the staff row, so deactivating someone takes effect immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast, get_args

import jwt
import pyotp
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select

from api.auth.routes import _dummy_hash
from api.config import Settings, get_settings
from api.core.logging import get_logger
from api.core.passwords import verify_password
from api.db.models import PlatformUser
from api.db.session import Database
from api.deps import get_database, get_redis

router = APIRouter(prefix="/api/v1/platform/auth", tags=["platform-auth"])
log = get_logger(__name__)

PlatformRole = Literal["support", "ops", "owner"]
ROLES: tuple[PlatformRole, ...] = get_args(PlatformRole)
RANK: dict[PlatformRole, int] = {"support": 0, "ops": 1, "owner": 2}
TYP = "platform"
AUDIENCE = "hmh-console"
COOKIE = "hmh_prt"
COOKIE_PATH = "/api/v1/platform/auth"
CSRF_HEADER = "x-hmh-csrf"
TOTP_STEP_S = 30


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginIn(_In):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(pattern=r"^\d{6}$")


class StaffOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    role: PlatformRole


class PlatformSessionOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — the OAuth scheme name, not a secret
    expires_at: datetime
    user: StaffOut


@dataclass(frozen=True)
class Staff:
    user_id: uuid.UUID
    email: str
    role: PlatformRole

    @property
    def actor(self) -> str:
        return f"staff:{self.user_id}"

    def at_least(self, role: PlatformRole) -> bool:
        return RANK[self.role] >= RANK[role]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def _key(settings: Settings) -> str:
    if settings.jwt_secret is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "auth not configured")
    return settings.jwt_secret.get_secret_value()


def issue(settings: Settings, user: PlatformUser, now: datetime) -> tuple[str, datetime]:
    exp = now + timedelta(seconds=settings.platform_access_ttl_s)
    token = jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "aud": AUDIENCE,
            "sub": str(user.id),
            "role": user.role,
            "typ": TYP,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        },
        _key(settings),
        algorithm="HS256",
    )
    return token, exp


def decode(settings: Settings, token: str) -> tuple[uuid.UUID, PlatformRole]:
    try:
        claims = jwt.decode(
            token,
            _key(settings),
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "sub", "role", "typ", "iss", "aud"]},
        )
        if claims.get("typ") != TYP or claims.get("role") not in ROLES:
            raise _unauthorized("invalid token")
        return uuid.UUID(claims["sub"]), cast(PlatformRole, claims["role"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise _unauthorized("invalid or expired token") from exc


# ---------------------------------------------------------------- TOTP


def totp_step(secret: str, code: str, now: float) -> int | None:
    """The time-step `code` belongs to (current ±1 for clock drift), or None."""
    totp = pyotp.TOTP(secret)
    base = int(now) // TOTP_STEP_S
    for step in (base, base - 1, base + 1):
        if secrets.compare_digest(totp.at(step * TOTP_STEP_S), code):
            return step
    return None


async def _fresh_step(redis: Redis, user_id: uuid.UUID, step: int) -> bool:
    """True the first time a time-step is used for this user; a replayed code is refused."""
    key = f"platform:totp:{user_id}"
    try:
        last = await redis.get(key)
        if last is not None and int(last) >= step:
            return False
        await redis.set(key, step, ex=TOTP_STEP_S * 4)
    except (RedisError, OSError):
        return False  # cannot prove the code is fresh: refuse rather than allow a replay
    return True


# ---------------------------------------------------------------- refresh tokens (Redis)


def _rt_key(raw: str) -> str:
    return "platform:rt:" + hashlib.sha256(raw.encode()).hexdigest()


async def _new_refresh(redis: Redis, user_id: uuid.UUID, expires: float) -> str:
    raw = secrets.token_urlsafe(32)
    ttl = max(1, int(expires - time.time()))
    await redis.set(_rt_key(raw), json.dumps({"uid": str(user_id), "exp": expires}), ex=ttl)
    return raw


def _set_cookie(response: Response, raw: str, max_age: int) -> None:
    response.set_cookie(
        COOKIE, raw, max_age=max_age, path=COOKIE_PATH, secure=True, httponly=True,
        samesite="strict",
    )  # fmt: skip


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="strict")


def _out(user: PlatformUser) -> StaffOut:
    return StaffOut(
        id=user.id, email=user.email, name=user.name, role=cast(PlatformRole, user.role)
    )


async def _session(
    settings: Settings, redis: Redis, response: Response, user: PlatformUser, expires: float
) -> PlatformSessionOut:
    raw = await _new_refresh(redis, user.id, expires)
    _set_cookie(response, raw, max(1, int(expires - time.time())))
    token, exp = issue(settings, user, datetime.now(UTC))
    return PlatformSessionOut(access_token=token, expires_at=exp, user=_out(user))


def _fail_key(email: str) -> str:
    return "auth:pfail:" + hashlib.sha256(email.encode()).hexdigest()[:32]


@router.post("/login", response_model=PlatformSessionOut)
async def login(
    body: LoginIn,
    response: Response,
    db: Annotated[Database, Depends(get_database)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> PlatformSessionOut:
    settings = get_settings()
    _key(settings)
    email = body.email.lower()
    fail_key = _fail_key(email)
    try:
        failures = int(await redis.get(fail_key) or 0)
    except (RedisError, OSError):
        failures = 0
    if failures >= settings.login_max_failures:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts")

    async with db.platform_session() as s:
        user = await s.scalar(
            select(PlatformUser).where(
                func.lower(PlatformUser.email) == email, PlatformUser.is_active.is_(True)
            )
        )
    ok = await asyncio.to_thread(
        verify_password, body.password, user.password_hash if user else _dummy_hash()
    )
    step = (
        totp_step(user.totp_secret, body.code, time.time())
        if ok and user and user.totp_secret
        else None
    )
    if not (ok and user and step is not None and await _fresh_step(redis, user.id, step)):
        try:
            async with redis.pipeline(transaction=True) as p:
                p.incr(fail_key)
                p.expire(fail_key, settings.login_failure_window_s, nx=True)
                await p.execute()
        except (RedisError, OSError) as exc:
            log.warning("auth_failure_counter_unavailable", error=type(exc).__name__)
        log.info("platform_login_failed")
        raise _unauthorized("invalid email, password or code")

    with contextlib.suppress(RedisError, OSError):
        await redis.delete(fail_key)
    async with db.platform_session() as s:
        row = await s.get(PlatformUser, user.id)
        if row is not None:
            row.last_login_at = datetime.now(UTC)
    log.info("platform_login", user_id=str(user.id), role=user.role)
    return await _session(
        settings, redis, response, user, time.time() + settings.platform_session_ttl_s
    )


def _require_csrf(value: str | None) -> None:
    if value != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-HMH-CSRF header")


@router.post("/refresh", response_model=PlatformSessionOut)
async def refresh(
    request: Request,
    response: Response,
    db: Annotated[Database, Depends(get_database)],
    redis: Annotated[Redis, Depends(get_redis)],
    x_hmh_csrf: Annotated[str | None, Header()] = None,
) -> PlatformSessionOut:
    _require_csrf(x_hmh_csrf)
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise _unauthorized("no session")
    try:
        stored = await redis.getdel(_rt_key(raw))
    except (RedisError, OSError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "session store unavailable"
        ) from exc
    if stored is None:
        _clear_cookie(response)
        raise _unauthorized("session expired")
    data: dict[str, Any] = json.loads(stored)
    async with db.platform_session() as s:
        user = await s.get(PlatformUser, uuid.UUID(data["uid"]))
    if user is None or not user.is_active or float(data["exp"]) <= time.time():
        _clear_cookie(response)
        raise _unauthorized("session expired")
    return await _session(get_settings(), redis, response, user, float(data["exp"]))


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    redis: Annotated[Redis, Depends(get_redis)],
    x_hmh_csrf: Annotated[str | None, Header()] = None,
) -> Response:
    _require_csrf(x_hmh_csrf)
    raw = request.cookies.get(COOKIE)
    if raw:
        try:
            await redis.delete(_rt_key(raw))
        except (RedisError, OSError) as exc:
            log.warning("platform_logout_store_unavailable", error=type(exc).__name__)
    out = Response(status_code=204)
    _clear_cookie(out)
    return out


# ---------------------------------------------------------------- request dependency


@dataclass(frozen=True)
class PlatformCtx:
    staff: Staff
    db: Database
    redis: Redis


async def current_staff(
    request: Request,
    db: Annotated[Database, Depends(get_database)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> PlatformCtx:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized("missing bearer token")
    user_id, _ = decode(get_settings(), token)
    async with db.platform_session() as s:
        user = await s.get(PlatformUser, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("account disabled")
    role = cast(PlatformRole, user.role)  # the row, not the token: a demotion applies at once
    return PlatformCtx(staff=Staff(user.id, user.email, role), db=db, redis=redis)


def require_staff(role: PlatformRole = "support") -> Any:
    async def dep(ctx: Annotated[PlatformCtx, Depends(current_staff)]) -> PlatformCtx:
        if not ctx.staff.at_least(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires the {role} role")
        return ctx

    return dep


StaffCtx = Annotated[PlatformCtx, Depends(require_staff("support"))]
OpsCtx = Annotated[PlatformCtx, Depends(require_staff("ops"))]
OwnerCtx = Annotated[PlatformCtx, Depends(require_staff("owner"))]


@router.get("/me", response_model=StaffOut)
async def me(ctx: StaffCtx) -> StaffOut:
    async with ctx.db.platform_session() as s:
        user = await s.get(PlatformUser, ctx.staff.user_id)
    assert user is not None
    return _out(user)

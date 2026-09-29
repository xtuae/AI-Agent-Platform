"""Dashboard tokens.

* Access token — HS256 JWT, 15 minutes, held in memory by the dashboard and sent as
  `Authorization: Bearer`. Claims: sub (tenant_users.id), tid (tenant), role, iat, exp, typ.
  The tenant a request acts on comes from `tid` and nowhere else.
* Refresh token — 256 random bits in an HttpOnly, Secure, SameSite=Strict cookie scoped to
  /api/v1/auth. Only sha256(token) is stored. Rotated on every use.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast, get_args

import jwt

from api.config import Settings

Role = Literal["viewer", "agent", "admin"]
ROLES: tuple[Role, ...] = get_args(Role)
ROLE_RANK: dict[Role, int] = {"viewer": 0, "agent": 1, "admin": 2}

ALGORITHM = "HS256"
ACCESS = "access"
REFRESH_COOKIE = "hmh_rt"
REFRESH_COOKIE_PATH = "/api/v1/auth"


class AuthNotConfiguredError(RuntimeError):
    """JWT_SECRET is not set; dashboard auth is unavailable."""


class InvalidTokenError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    """The authenticated dashboard user. Built only from a verified access token."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
    issued_at_ms: int  # millisecond precision so a token issued right after a revocation survives
    expires_at: datetime

    @property
    def actor(self) -> str:
        """audit_log.actor for anything this person does."""
        return f"user:{self.user_id}"

    def at_least(self, role: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def _key(settings: Settings) -> str:
    if settings.jwt_secret is None:
        raise AuthNotConfiguredError("JWT_SECRET is not set")
    return settings.jwt_secret.get_secret_value()


def issue_access(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: Role,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    now = now or datetime.now(UTC)
    exp = now + timedelta(seconds=settings.jwt_access_ttl_s)
    token = jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "sub": str(user_id),
            "tid": str(tenant_id),
            "role": role,
            "typ": ACCESS,
            "iat": int(now.timestamp()),
            "iatms": int(now.timestamp() * 1000),
            "exp": int(exp.timestamp()),
        },
        _key(settings),
        algorithm=ALGORITHM,
    )
    return token, exp


def decode_access(settings: Settings, token: str) -> Principal:
    try:
        claims = jwt.decode(
            token,
            _key(settings),
            algorithms=[ALGORITHM],  # pinned: never accept 'none' or an asymmetric confusion
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "iatms", "sub", "tid", "role", "typ", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(type(exc).__name__) from exc
    if claims.get("typ") != ACCESS or claims.get("role") not in ROLES:
        raise InvalidTokenError("wrong token type or role")
    try:
        return Principal(
            user_id=uuid.UUID(claims["sub"]),
            tenant_id=uuid.UUID(claims["tid"]),
            role=cast(Role, claims["role"]),
            issued_at_ms=int(claims["iatms"]),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
        )
    except (ValueError, TypeError) as exc:
        raise InvalidTokenError("malformed claims") from exc


def new_refresh_token() -> tuple[str, bytes]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()

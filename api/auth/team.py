"""A tenant's dashboard users: the rules shared by the client's own Settings → Team page and the
HMH Labz console. Callers own the session, the password hashing (off the event loop), Redis
revocation and the audit entry; this module owns the rules (unique email, never zero admins,
sessions end when access changes)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import AuthRefreshToken, TenantUser


class TeamError(Exception):
    """`code` is the stable machine-readable reason the API returns."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Updated:
    user: TenantUser
    before: dict[str, Any]
    access_changed: bool  # True: the caller must also revoke access tokens (Redis)


async def add_user(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    email: str,
    name: str | None,
    role: str,
    password_hash: str,
) -> TenantUser:
    email = email.lower()
    exists = await s.scalar(
        select(TenantUser.id).where(
            TenantUser.tenant_id == tenant_id, func.lower(TenantUser.email) == email
        )
    )
    if exists is not None:
        raise TeamError("email_exists")
    u = TenantUser(
        tenant_id=tenant_id, email=email, name=name, role=role, password_hash=password_hash
    )
    s.add(u)
    await s.flush()
    await s.refresh(u)
    return u


async def update_user(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    new_hash: str | None = None,
) -> Updated:
    u = await s.scalar(
        select(TenantUser)
        .where(TenantUser.id == user_id, TenantUser.tenant_id == tenant_id)
        .with_for_update()
    )
    if u is None:
        raise TeamError("not_found")
    demoting = changes.get("role", "admin") != "admin" or changes.get("is_active") is False
    if u.role == "admin" and u.is_active and demoting:
        admins = await s.scalar(
            select(func.count())
            .select_from(TenantUser)
            .where(
                TenantUser.tenant_id == tenant_id,
                TenantUser.role == "admin",
                TenantUser.is_active.is_(True),
            )
        )
        if int(admins or 0) <= 1:
            raise TeamError("last_admin")
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
    if access_changed:  # end their sessions: refresh tokens here, access tokens via Redis
        await s.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.user_id == u.id, AuthRefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
    await s.flush()
    return Updated(user=u, before=before, access_changed=access_changed)

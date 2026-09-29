"""Dashboard auth: login, refresh rotation, revocation, role gating, token integrity."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from sqlalchemy import select, update

from api.auth.tokens import REFRESH_COOKIE, issue_access
from api.config import Settings
from api.db.models import AuthRefreshToken, Tenant
from api.tests.conftest import DEFAULT_PASSWORD, DashHarness, TenantPair, make_tenant

CSRF = {"x-hmh-csrf": "1"}


async def _login(
    h: DashHarness, email: str, password: str = DEFAULT_PASSWORD, **extra: str
) -> httpx.Response:
    return await h.client.post(
        "/api/v1/auth/login", json={"email": email, "password": password, **extra}
    )


async def _with_cookie(
    h: DashHarness, action: str, cookie: str, *, csrf: bool = True
) -> httpx.Response:
    h.client.cookies.clear()
    h.client.cookies.set(REFRESH_COOKIE, cookie)
    try:
        return await h.client.post(f"/api/v1/auth/{action}", headers=CSRF if csrf else {})
    finally:
        h.client.cookies.clear()


async def test_login_returns_token_for_the_users_own_tenant(
    dash: DashHarness, tenants: TenantPair, settings: Settings
) -> None:
    u = await dash.user(tenants.a, "agent", email=f"Zameer-{uuid.uuid4().hex[:6]}@Example.test")
    r = await _login(dash, f"  {u.email.upper()} ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "agent"
    assert body["tenant"]["id"] == str(tenants.a)
    assert settings.jwt_secret is not None
    claims = jwt.decode(
        body["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
    )
    assert claims["tid"] == str(tenants.a)
    assert claims["sub"] == str(u.id)
    cookie = r.headers["set-cookie"]
    for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/api/v1/auth"):
        assert attr.lower() in cookie.lower()
    me = await dash.client.get(
        "/api/v1/auth/me", headers={"authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["tenant"]["id"] == str(tenants.a)


@pytest.mark.parametrize("case", ["wrong_password", "unknown_email", "inactive", "suspended"])
async def test_login_refusals_look_identical(
    dash: DashHarness, tenants: TenantPair, case: str
) -> None:
    email = f"{case}-{uuid.uuid4().hex[:6]}@example.test"
    if case != "unknown_email":
        await dash.user(tenants.a, "admin", email=email, active=case != "inactive")
    if case == "suspended":
        async with dash.db.platform_session() as s:
            await s.execute(update(Tenant).where(Tenant.id == tenants.a).values(status="suspended"))
    r = await _login(
        dash, email, "wrong password!!" if case == "wrong_password" else DEFAULT_PASSWORD
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid email or password"


async def test_repeated_failures_lock_the_email(
    dash: DashHarness, tenants: TenantPair, settings: Settings
) -> None:
    u = await dash.user(tenants.a)
    for _ in range(settings.login_max_failures):
        assert (await _login(dash, u.email, "nope nope nope")).status_code == 401
    r = await _login(dash, u.email)  # even the right password, until the window passes
    assert r.status_code == 429


async def test_same_email_in_two_tenants_must_choose(
    dash: DashHarness, tenants: TenantPair
) -> None:
    email = f"shared-{uuid.uuid4().hex[:6]}@example.test"
    await dash.user(tenants.a, "admin", email=email)
    await dash.user(tenants.b, "viewer", email=email)
    r = await _login(dash, email)
    assert r.status_code == 409
    slugs = {t["slug"] for t in r.json()["detail"]["tenants"]}
    assert len(slugs) == 2
    async with dash.db.platform_session() as s:
        slug_b = (await s.get(Tenant, tenants.b)).slug  # type: ignore[union-attr]
    r = await _login(dash, email, tenant=slug_b)
    assert r.status_code == 200
    assert r.json()["tenant"]["id"] == str(tenants.b)
    assert r.json()["user"]["role"] == "viewer"
    # a slug does not grant anything by itself: wrong password + slug is still 401
    assert (await _login(dash, email, "wrong password!!", tenant=slug_b)).status_code == 401


async def test_login_body_cannot_carry_a_tenant_id(dash: DashHarness, tenants: TenantPair) -> None:
    u = await dash.user(tenants.a)
    r = await _login(dash, u.email, tenant_id=str(tenants.b))
    assert r.status_code == 422


async def test_refresh_rotates_and_old_token_is_single_use(
    dash: DashHarness, tenants: TenantPair
) -> None:
    u = await dash.user(tenants.a)
    first = await _login(dash, u.email)
    old = first.cookies[REFRESH_COOKIE]

    r = await _with_cookie(dash, "refresh", old)
    assert r.status_code == 200
    new = r.cookies[REFRESH_COOKIE]
    assert new != old
    assert r.json()["tenant"]["id"] == str(tenants.a)

    # within the grace window a second tab using the old cookie still gets in…
    again = await _with_cookie(dash, "refresh", old)
    assert again.status_code == 200

    # …after it, the old cookie is treated as stolen and the whole family dies
    async with dash.db.platform_session() as s:
        await s.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.user_id == u.id, AuthRefreshToken.replaced_by.is_not(None))
            .values(revoked_at=datetime.now(UTC) - timedelta(minutes=5))
        )
    stolen = await _with_cookie(dash, "refresh", old)
    assert stolen.status_code == 401
    legit = await _with_cookie(dash, "refresh", new)
    assert legit.status_code == 401
    async with dash.db.platform_session() as s:
        live = (
            await s.scalars(
                select(AuthRefreshToken).where(
                    AuthRefreshToken.user_id == u.id, AuthRefreshToken.revoked_at.is_(None)
                )
            )
        ).all()
    assert live == []


async def test_refresh_needs_csrf_header_and_a_cookie(
    dash: DashHarness, tenants: TenantPair
) -> None:
    u = await dash.user(tenants.a)
    cookie = (await _login(dash, u.email)).cookies[REFRESH_COOKIE]
    dash.client.cookies.clear()
    r = await _with_cookie(dash, "refresh", cookie, csrf=False)
    assert r.status_code == 403
    assert (await dash.client.post("/api/v1/auth/refresh", headers=CSRF)).status_code == 401
    garbage = await _with_cookie(dash, "refresh", "x" * 43)
    assert garbage.status_code == 401


async def test_refresh_refused_once_user_deactivated_or_tenant_suspended(
    dash: DashHarness, tenants: TenantPair
) -> None:
    admin = await dash.login(tenants.a, "admin")
    u = await dash.user(tenants.a, "agent")
    cookie = (await _login(dash, u.email)).cookies[REFRESH_COOKIE]
    r = await dash.client.patch(f"/api/v1/team/{u.id}", headers=admin, json={"is_active": False})
    assert r.status_code == 200
    r = await _with_cookie(dash, "refresh", cookie)
    assert r.status_code == 401

    v = await dash.user(tenants.a, "viewer")
    cookie = (await _login(dash, v.email)).cookies[REFRESH_COOKIE]
    async with dash.db.platform_session() as s:
        await s.execute(update(Tenant).where(Tenant.id == tenants.a).values(status="suspended"))
    r = await _with_cookie(dash, "refresh", cookie)
    assert r.status_code == 401


async def test_logout_revokes_the_session(dash: DashHarness, tenants: TenantPair) -> None:
    u = await dash.user(tenants.a)
    cookie = (await _login(dash, u.email)).cookies[REFRESH_COOKIE]
    r = await _with_cookie(dash, "logout", cookie)
    assert r.status_code == 204
    r = await _with_cookie(dash, "refresh", cookie)
    assert r.status_code == 401


async def test_deactivation_kills_live_access_tokens_immediately(
    dash: DashHarness, tenants: TenantPair
) -> None:
    admin = await dash.login(tenants.a, "admin")
    u = await dash.user(tenants.a, "agent")
    token = (await _login(dash, u.email)).json()["access_token"]
    headers = {"authorization": f"Bearer {token}"}
    assert (await dash.client.get("/api/v1/m/orders", headers=headers)).status_code == 200
    await dash.client.patch(f"/api/v1/team/{u.id}", headers=admin, json={"role": "viewer"})
    assert (await dash.client.get("/api/v1/m/orders", headers=headers)).status_code == 401


async def test_password_change_ends_other_sessions_but_keeps_this_one(
    dash: DashHarness, tenants: TenantPair
) -> None:
    u = await dash.user(tenants.a, "viewer")
    other = await _login(dash, u.email)  # e.g. a lost phone
    this = await _login(dash, u.email)
    h = {"authorization": f"Bearer {this.json()['access_token']}"}
    r = await dash.client.post(
        "/api/v1/auth/password",
        headers=h,
        json={"current_password": DEFAULT_PASSWORD, "new_password": "a much better passphrase"},
    )
    assert r.status_code == 200, r.text
    fresh = {"authorization": f"Bearer {r.json()['access_token']}"}
    assert (await dash.client.get("/api/v1/auth/me", headers=fresh)).status_code == 200
    assert (await dash.client.get("/api/v1/auth/me", headers=h)).status_code == 401
    old_cookie = other.cookies[REFRESH_COOKIE]
    r = await _with_cookie(dash, "refresh", old_cookie)
    assert r.status_code == 401
    assert (await _login(dash, u.email)).status_code == 401
    assert (await _login(dash, u.email, "a much better passphrase")).status_code == 200

    bad = await dash.client.post(
        "/api/v1/auth/password",
        headers=fresh,
        json={"current_password": "not it at all", "new_password": "another good passphrase"},
    )
    assert bad.status_code == 400


def _forge(settings: Settings, **overrides: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": settings.jwt_issuer,
        "sub": str(uuid.uuid4()),
        "tid": str(uuid.uuid4()),
        "role": "admin",
        "typ": "access",
        "iat": int(now.timestamp()),
        "iatms": int(now.timestamp() * 1000),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    claims.update(overrides)
    key = settings.jwt_secret.get_secret_value()  # type: ignore[union-attr]
    return jwt.encode(claims, key, algorithm="HS256")


async def test_token_integrity(dash: DashHarness, tenants: TenantPair, settings: Settings) -> None:
    good = _forge(settings, tid=str(tenants.a))
    head, _body, sig = good.split(".")
    # re-point a valid token at tenant B by editing the payload: signature no longer matches
    other = _forge(settings, tid=str(tenants.b)).split(".")[1]
    cases = {
        "tampered_tenant": f"{head}.{other}.{sig}",
        "alg_none": jwt.encode(
            {"sub": str(uuid.uuid4()), "tid": str(tenants.b), "role": "admin"},
            None,  # type: ignore[arg-type]  # unsigned on purpose
            algorithm="none",
        ),
        "wrong_key": jwt.encode(
            jwt.decode(good, options={"verify_signature": False}), "k" * 48, algorithm="HS256"
        ),
        "expired": _forge(
            settings, exp=int((datetime.now(UTC) - timedelta(seconds=5)).timestamp())
        ),
        "wrong_type": _forge(settings, typ="refresh"),
        "wrong_issuer": _forge(settings, iss="someone-else"),
        "bad_role": _forge(settings, role="owner"),
        "no_tenant": jwt.encode(
            {
                k: v
                for k, v in jwt.decode(good, options={"verify_signature": False}).items()
                if k != "tid"
            },
            settings.jwt_secret.get_secret_value(),  # type: ignore[union-attr]
            algorithm="HS256",
        ),
    }
    for name, token in cases.items():
        r = await dash.client.get("/api/v1/m/orders", headers={"authorization": f"Bearer {token}"})
        assert r.status_code == 401, name
    assert (await dash.client.get("/api/v1/m/orders")).status_code == 401
    r = await dash.client.get("/api/v1/m/orders", headers={"authorization": f"Basic {good}"})
    assert r.status_code == 401


async def test_role_gating(dash: DashHarness, tenants: TenantPair) -> None:
    viewer = await dash.login(tenants.a, "viewer")
    agent = await dash.login(tenants.a, "agent")
    admin = await dash.login(tenants.a, "admin")
    cust = {"wa_id": "+971 50 765 4321", "name": "Walk-in"}
    product = {
        "sku": f"S{uuid.uuid4().hex[:6]}",
        "name_en": "Chips",
        "category": "snack",
        "price_aed": "2.50",
    }

    assert (await dash.client.get("/api/v1/m/orders", headers=viewer)).status_code == 200
    assert (
        await dash.client.post("/api/v1/contacts", headers=viewer, json=cust)
    ).status_code == 403
    assert (await dash.client.post("/api/v1/contacts", headers=agent, json=cust)).status_code == 201
    assert (await dash.client.get("/api/v1/team", headers=agent)).status_code == 403
    assert (
        await dash.client.post("/api/v1/m/catalog/products", headers=agent, json=product)
    ).status_code == 403
    assert (await dash.client.patch("/api/v1/settings", headers=agent, json={})).status_code == 403
    assert (
        await dash.client.post("/api/v1/m/catalog/products", headers=admin, json=product)
    ).status_code == 201
    assert (await dash.client.get("/api/v1/team", headers=admin)).status_code == 200


async def test_auth_answers_503_without_a_jwt_secret(
    dash: DashHarness, tenants: TenantPair, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = issue_access(settings, user_id=uuid.uuid4(), tenant_id=tenants.a, role="admin")
    monkeypatch.setattr(settings, "jwt_secret", None)
    assert (await _login(dash, "x@example.test")).status_code == 503
    r = await dash.client.get("/api/v1/m/orders", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 503


async def test_last_admin_cannot_be_removed(dash: DashHarness, db_tenant: uuid.UUID) -> None:
    admin_user = await dash.user(db_tenant, "admin")
    r = await _login(dash, admin_user.email)
    h = {"authorization": f"Bearer {r.json()['access_token']}"}
    for change in ({"role": "agent"}, {"is_active": False}):
        r = await dash.client.patch(f"/api/v1/team/{admin_user.id}", headers=h, json=change)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "last_admin"


@pytest.fixture
async def db_tenant(dash: DashHarness) -> uuid.UUID:
    return await make_tenant(dash.db, "solo")

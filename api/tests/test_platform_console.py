"""Phase 5: the platform console — staff sign-in with TOTP, cross-tenant views that still go
through RLS, margin, statement pulls and the reimbursement ledger."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pyotp
import pytest
from sqlalchemy import select, update

from api.billing.periods import month_start
from api.core.crypto import encrypt_secret
from api.db.models import AuditLog, PlatformUser, Tenant, TenantChannel, TenantSettings
from api.metering import record_usage
from api.platform import console
from api.platform.auth import COOKIE, totp_step
from api.tests.conftest import DashHarness, _password_hash, make_tenant, meta_id

PASSWORD = "a long staff password"


async def _staff(dash: DashHarness, role: str = "owner", *, totp: bool = True) -> tuple[str, str]:
    email = f"{role}-{uuid.uuid4().hex[:6]}@hmhlabz.test"
    secret = pyotp.random_base32()
    async with dash.db.platform_session() as s:
        s.add(
            PlatformUser(
                email=email,
                role=role,
                password_hash=_password_hash(PASSWORD),
                totp_secret=secret if totp else None,
            )
        )
    return email, secret


async def _login(dash: DashHarness, email: str, secret: str) -> dict[str, str]:
    r = await dash.client.post(
        "/api/v1/platform/auth/login",
        json={"email": email, "password": PASSWORD, "code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 200, r.text
    return {"authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------------- sign-in


async def test_sign_in_needs_password_and_a_fresh_code(dash: DashHarness) -> None:
    email, secret = await _staff(dash)
    code = pyotp.TOTP(secret).now()
    bad = [
        {"email": email, "password": "wrong password", "code": code},
        {"email": email, "password": PASSWORD, "code": "000000" if code != "000000" else "111111"},
        {"email": "nobody@hmhlabz.test", "password": PASSWORD, "code": code},
    ]
    for body in bad:
        r = await dash.client.post("/api/v1/platform/auth/login", json=body)
        assert r.status_code == 401
    r = await dash.client.post(
        "/api/v1/platform/auth/login", json={"email": email, "password": PASSWORD, "code": code}
    )
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "owner"
    assert COOKIE in r.cookies
    # the same code cannot be used twice
    r = await dash.client.post(
        "/api/v1/platform/auth/login", json={"email": email, "password": PASSWORD, "code": code}
    )
    assert r.status_code == 401


async def test_an_account_without_totp_cannot_sign_in(dash: DashHarness) -> None:
    email, secret = await _staff(dash, totp=False)
    r = await dash.client.post(
        "/api/v1/platform/auth/login",
        json={"email": email, "password": PASSWORD, "code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 401


def test_totp_accepts_one_step_of_drift_only() -> None:
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    base = now // 30
    assert totp_step(secret, totp.at(now), now) == base
    assert totp_step(secret, totp.at(now - 30), now) == base - 1
    assert totp_step(secret, totp.at(now - 120), now) is None


async def test_tenant_and_platform_tokens_never_cross(dash: DashHarness) -> None:
    tid = await make_tenant(dash.db, "cross")
    tenant_headers = await dash.login(tid, "admin")
    r = await dash.client.get("/api/v1/platform/overview", headers=tenant_headers)
    assert r.status_code == 401
    staff = await _login(dash, *(await _staff(dash)))
    r = await dash.client.get("/api/v1/today", headers=staff)
    assert r.status_code == 401
    assert (await dash.client.get("/api/v1/platform/overview")).status_code == 401


async def test_refresh_rotates_and_logout_ends_the_session(dash: DashHarness) -> None:
    await _login(dash, *(await _staff(dash)))
    csrf = {"x-hmh-csrf": "1"}
    assert (await dash.client.post("/api/v1/platform/auth/refresh")).status_code == 403
    first = dash.client.cookies.get(COOKIE)
    r = await dash.client.post("/api/v1/platform/auth/refresh", headers=csrf)
    assert r.status_code == 200
    assert dash.client.cookies.get(COOKIE) != first
    # the rotated-away token is dead
    current = dash.client.cookies.get(COOKIE)
    dash.client.cookies.set(COOKIE, first or "", path="/api/v1/platform/auth")
    r = await dash.client.post("/api/v1/platform/auth/refresh", headers=csrf)
    assert r.status_code == 401
    dash.client.cookies.clear()
    dash.client.cookies.set(COOKIE, current or "", path="/api/v1/platform/auth")
    r = await dash.client.post("/api/v1/platform/auth/logout", headers=csrf)
    assert r.status_code == 204
    assert (
        await dash.client.post("/api/v1/platform/auth/refresh", headers=csrf)
    ).status_code == 401


async def test_disabling_staff_takes_effect_at_once(dash: DashHarness) -> None:
    email, secret = await _staff(dash)
    headers = await _login(dash, email, secret)
    assert (await dash.client.get("/api/v1/platform/auth/me", headers=headers)).status_code == 200
    async with dash.db.platform_session() as s:
        await s.execute(
            update(PlatformUser).where(PlatformUser.email == email).values(is_active=False)
        )
    assert (await dash.client.get("/api/v1/platform/auth/me", headers=headers)).status_code == 401


# ---------------------------------------------------------------- console views


async def _client_tenant(
    dash: DashHarness,
    name: str,
    *,
    fee: str | None = "2500.00",
    start: date = date(2026, 9, 15),
    borne_until: date = date(2026, 10, 14),
    usage_month: date = date(2026, 10, 1),
) -> uuid.UUID:
    tid = await make_tenant(dash.db, name)
    async with dash.db.platform_session() as s:
        await s.execute(
            update(Tenant)
            .where(Tenant.id == tid)
            .values(
                name=name,
                status="active",
                monthly_fee_aed=Decimal(fee) if fee else None,
                service_start=start,
                free_months_until=borne_until,
                meta_charges_borne_by_us_until=borne_until,
            )
        )
        s.add(TenantSettings(tenant_id=tid, monthly_message_cap_aed=Decimal("10.00")))
        s.add(
            TenantChannel(
                tenant_id=tid,
                phone_number_id=meta_id(),
                waba_id=meta_id(),
                display_phone="+971500000100",
                access_token_encrypted=encrypt_secret("tok"),
                quality_rating="GREEN",
            )
        )
    async with dash.db.tenant_session(tid) as s:
        for day, cost in ((10, "3.00"), (20, "6.00")):
            await record_usage(
                s, tid, at=datetime(usage_month.year, usage_month.month, day, 12, tzinfo=UTC),
                msgs_out=1,
                category="marketing", meta_cost_aed=Decimal(cost),
                llm_prompt_tokens=1000, llm_completion_tokens=200, llm_cost_usd=Decimal("1.00"),
            )  # fmt: skip
    return tid


def test_revenue_counts_only_days_after_the_free_period() -> None:
    oct1 = date(2026, 10, 1)
    fee = Decimal("3100")
    assert console.revenue(fee, oct1, None) == fee
    assert console.revenue(fee, oct1, date(2026, 9, 30)) == fee
    assert console.revenue(fee, oct1, date(2026, 10, 14)) == Decimal("1700")  # 17 of 31 days
    assert console.revenue(fee, oct1, date(2026, 10, 31)) == 0
    assert console.revenue(None, oct1, None) is None


async def test_overview_shows_every_tenant_with_usage_margin_and_health(dash: DashHarness) -> None:
    a = await _client_tenant(dash, "Alpha Water")
    b = await _client_tenant(dash, "Beta Realty", fee=None)
    headers = await _login(dash, *(await _staff(dash, "support")))
    r = await dash.client.get("/api/v1/platform/overview?month=2026-10", headers=headers)
    assert r.status_code == 200, r.text
    rows = {t["id"]: t for t in r.json()["tenants"]}
    alpha = rows[str(a)]
    assert alpha["usage"]["meta_cost_aed"] == "9.00"
    assert alpha["usage"]["borne_aed"] == "3.00"  # 10 Oct borne, 20 Oct the client's
    assert alpha["usage"]["llm_cost_usd"] == "2.0000"
    assert alpha["usage"]["llm_cost_aed"] == "7.35"  # 2 USD at the peg
    # fee 2500 for the 17 of 31 days after the free period; cost = LLM 7.345 + borne 3.00
    assert alpha["margin"] == {
        "fee_aed": "2500.00",
        "revenue_aed": "1370.97",
        "direct_cost_aed": "10.35",
        "margin_aed": "1360.62",
        "margin_pct": 99.2,
    }
    assert alpha["pct_of_cap"] == 90.0
    assert alpha["health"]["status"] == "degraded"
    assert "at 80% or more of the message cap" in alpha["health"]["problems"]
    assert alpha["statement"] == "not_pulled"
    assert rows[str(b)]["margin"]["margin_aed"] is None  # no fee recorded: no margin claimed
    assert r.json()["platform"]["database"] is True


async def test_support_reads_ops_pulls_owner_pays(dash: DashHarness) -> None:
    # a service month that has ended, whatever today is
    tid = await _client_tenant(
        dash, "Gamma Water", start=date(2025, 6, 15), borne_until=date(2025, 7, 14),
        usage_month=date(2025, 7, 1),
    )  # fmt: skip
    support = await _login(dash, *(await _staff(dash, "support")))
    ops = await _login(dash, *(await _staff(dash, "ops")))
    owner = await _login(dash, *(await _staff(dash, "owner")))

    points = [
        {"start": int(datetime(2025, 7, d, tzinfo=UTC).timestamp()), "end": 0, "country": "AE",
         "pricing_category": "MARKETING", "pricing_type": "REGULAR", "volume": 1, "cost": c}
        for d, c in ((10, 3.0), (20, 6.0))
    ]  # fmt: skip
    seen: list[str] = []

    def meta(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, json={"currency": "AED", "pricing_analytics": {"data": [{"data_points": points}]}}
        )

    dash.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(meta))
    url = f"/api/v1/platform/tenants/{tid}/statement/pull?month=2025-07"
    assert (await dash.client.post(url, headers=support)).status_code == 403
    r = await dash.client.post(url, headers=ops)
    assert r.status_code == 200, r.text
    assert r.json()["statement"]["total"]["status"] == "ok"
    assert seen
    async with dash.db.tenant_session(tid) as s:
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "pull_meta_statement"))
    assert entry is not None

    r = await dash.client.get(f"/api/v1/platform/tenants/{tid}?month=2025-07", headers=support)
    ledger = r.json()["ledger"]
    assert [(m["service_month"], m["payable_aed"], m["state"]) for m in ledger] == [
        (1, "3.00", "due")
    ]

    body = {"tenant_id": str(tid), "service_month": 1, "reference": "BANK-1"}
    assert (
        await dash.client.post("/api/v1/platform/reimbursements", json=body, headers=ops)
    ).status_code == 403
    r = await dash.client.post("/api/v1/platform/reimbursements", json=body, headers=owner)
    assert r.status_code == 201, r.text
    assert (r.json()["state"], r.json()["paid_aed"], r.json()["reference"]) == (
        "paid",
        "3.00",
        "BANK-1",
    )
    r = await dash.client.post("/api/v1/platform/reimbursements", json=body, headers=owner)
    assert r.status_code == 409

    r = await dash.client.get("/api/v1/platform/reimbursements", headers=support)
    mine = [x for x in r.json()["rows"] if x["tenant_id"] == str(tid)]
    assert [x["state"] for x in mine] == ["paid"]


async def test_unknown_tenant_and_bad_month(dash: DashHarness) -> None:
    headers = await _login(dash, *(await _staff(dash)))
    r = await dash.client.get(f"/api/v1/platform/tenants/{uuid.uuid4()}", headers=headers)
    assert r.status_code == 404
    r = await dash.client.get("/api/v1/platform/overview?month=oct", headers=headers)
    assert r.status_code == 422


@pytest.mark.parametrize("body", [{"tenant_id": "x"}, {"extra": 1}])
async def test_reimbursement_input_is_validated(dash: DashHarness, body: dict[str, Any]) -> None:
    headers = await _login(dash, *(await _staff(dash)))
    r = await dash.client.post(
        "/api/v1/platform/reimbursements",
        json={"tenant_id": str(uuid.uuid4()), "service_month": 1, **body},
        headers=headers,
    )
    assert r.status_code == 422


def test_month_start() -> None:
    assert month_start(date(2026, 10, 31)) == date(2026, 10, 1)

"""The console's admin API: creating and editing clients, modules, WhatsApp numbers and tokens,
client dashboard users, and HMH Labz staff — with the role each action needs."""

from __future__ import annotations

import uuid

import httpx
import pyotp
from sqlalchemy import select

from api.core.crypto import decrypt_secret
from api.db.models import AuditLog, PlatformUser, TenantChannel, TenantUser
from api.tests.conftest import DEFAULT_PASSWORD, DashHarness, make_tenant, meta_id
from api.tests.test_platform_console import PASSWORD, _login, _staff

A = "/api/v1/platform/admin"


async def _as(dash: DashHarness, role: str) -> dict[str, str]:
    return await _login(dash, *(await _staff(dash, role)))


def _slug() -> str:
    return f"acme-{uuid.uuid4().hex[:8]}"


async def _audit_actions(dash: DashHarness, tenant_id: str) -> list[str]:
    async with dash.db.tenant_session(uuid.UUID(tenant_id)) as s:
        return list((await s.scalars(select(AuditLog.action).order_by(AuditLog.id))).all())


# ---------------------------------------------------------------- clients


async def test_ops_creates_a_client_with_modules_and_support_cannot(dash: DashHarness) -> None:
    support, ops = await _as(dash, "support"), await _as(dash, "ops")
    body = {
        "slug": _slug(),
        "name": "Acme Water",
        "legal_name": "Acme Water L.L.C",
        "monthly_fee_aed": "1500.00",
        "monthly_message_cap_aed": "900",
        "modules": ["water_delivery"],
    }
    assert (await dash.client.post(f"{A}/tenants", json=body, headers=support)).status_code == 403
    r = await dash.client.post(f"{A}/tenants", json=body, headers=ops)
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["profile"]["slug"] == body["slug"]
    assert t["profile"]["status"] == "trial"
    assert t["profile"]["monthly_fee_aed"] == "1500.00"
    assert t["profile"]["monthly_message_cap_aed"] == "900.00"
    assert set(t["modules"]) == {"catalog", "orders", "coupons", "campaigns"}
    assert await _audit_actions(dash, t["profile"]["id"]) == ["create_tenant"]

    # the same slug twice, a reserved slug, a malformed one
    assert (await dash.client.post(f"{A}/tenants", json=body, headers=ops)).status_code == 409
    for bad in ("admin", "api", "Not A Slug", "-x", ""):
        r = await dash.client.post(f"{A}/tenants", json={"slug": bad, "name": "x"}, headers=ops)
        assert r.status_code == 422, bad


async def test_editing_a_client_records_before_and_after(dash: DashHarness) -> None:
    ops = await _as(dash, "ops")
    tid = str(await make_tenant(dash.db, "edit"))
    r = await dash.client.patch(
        f"{A}/tenants/{tid}",
        json={"status": "active", "contract_ref": "HMH-1", "escalation_phone": "971500000009"},
        headers=ops,
    )
    assert r.status_code == 200, r.text
    p = r.json()["profile"]
    assert (p["status"], p["contract_ref"], p["escalation_phone"]) == (
        "active",
        "HMH-1",
        "971500000009",
    )
    async with dash.db.tenant_session(uuid.UUID(tid)) as s:
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "update_tenant"))
    assert entry is not None
    assert entry.after is not None
    assert entry.before is not None
    assert entry.after["status"] == "active"
    assert entry.before["status"] == "trial"
    # a required column cannot be cleared
    r = await dash.client.patch(f"{A}/tenants/{tid}", json={"name": None}, headers=ops)
    assert r.status_code == 422


async def test_modules_are_switched_with_their_dependencies(dash: DashHarness) -> None:
    ops = await _as(dash, "ops")
    tid = str(await make_tenant(dash.db, "mods", modules=()))
    url = f"{A}/tenants/{tid}/modules"
    r = await dash.client.put(url, json={"enabled": ["orders"]}, headers=ops)  # needs catalog
    assert r.status_code == 409
    r = await dash.client.put(url, json={"enabled": ["catalog", "orders"]}, headers=ops)
    assert r.status_code == 200
    assert set(r.json()["modules"]) == {"catalog", "orders"}
    r = await dash.client.put(url, json={"enabled": ["orders"]}, headers=ops)  # orders needs it
    assert r.status_code == 409
    r = await dash.client.put(url, json={"enabled": []}, headers=ops)
    assert r.status_code == 200
    assert r.json()["modules"] == []
    assert "set_modules" in await _audit_actions(dash, tid)


async def test_dpa_downloads_as_markdown(dash: DashHarness) -> None:
    support = await _as(dash, "support")
    tid = await make_tenant(dash.db, "dpa")
    r = await dash.client.get(f"{A}/tenants/{tid}/dpa", headers=support)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]
    assert "Amazon Web Services" in r.text


# ---------------------------------------------------------------- WhatsApp numbers


async def test_a_number_is_never_rerouted_to_another_client(dash: DashHarness) -> None:
    ops = await _as(dash, "ops")
    a, b = await make_tenant(dash.db, "cha"), await make_tenant(dash.db, "chb")
    pid = meta_id()
    r = await dash.client.post(
        f"{A}/tenants/{a}/channels", json={"phone_number_id": pid, "waba_id": meta_id()},
        headers=ops,
    )  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["channels"][0]["has_token"] is False
    r = await dash.client.post(
        f"{A}/tenants/{b}/channels", json={"phone_number_id": pid}, headers=ops
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "phone_number_taken"
    r = await dash.client.post(
        f"{A}/tenants/{a}/channels", json={"phone_number_id": "not-a-number"}, headers=ops
    )
    assert r.status_code == 422


async def test_token_is_checked_with_meta_encrypted_and_never_returned(dash: DashHarness) -> None:
    ops = await _as(dash, "ops")
    tid = await make_tenant(dash.db, "tok")
    pid = meta_id()
    r = await dash.client.post(
        f"{A}/tenants/{tid}/channels", json={"phone_number_id": pid}, headers=ops
    )
    channel_id = r.json()["channels"][0]["id"]
    token = "EAAG" + "x" * 60
    accept = {"ok": True}

    def meta(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {token}"
        if not accept["ok"]:
            return httpx.Response(401, json={"error": {"message": "Invalid OAuth access token"}})
        return httpx.Response(200, json={"id": pid, "quality_rating": "GREEN"})

    dash.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(meta))
    url = f"{A}/channels/{channel_id}/token"

    accept["ok"] = False  # a token Meta rejects is not stored
    r = await dash.client.put(url, json={"token": token}, headers=ops)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "meta_rejected"
    async with dash.db.platform_session() as s:
        ch = await s.get(TenantChannel, uuid.UUID(channel_id))
    assert ch is not None
    assert ch.access_token_encrypted is None

    accept["ok"] = True
    r = await dash.client.put(url, json={"token": token, "expires": "2027-01-31"}, headers=ops)
    assert r.status_code == 200, r.text
    assert token not in r.text
    out = r.json()["channels"][0]
    assert out["has_token"] is True
    assert out["quality_rating"] == "GREEN"
    assert out["token_expires_at"].startswith("2027-01-31")
    async with dash.db.platform_session() as s:
        ch = await s.get(TenantChannel, uuid.UUID(channel_id))
    assert ch is not None
    assert ch.access_token_encrypted is not None
    assert decrypt_secret(ch.access_token_encrypted) == token
    async with dash.db.tenant_session(tid) as s:
        entry = await s.scalar(select(AuditLog).where(AuditLog.action == "set_channel_token"))
    assert entry is not None
    assert token not in str(entry.after)


# ---------------------------------------------------------------- client dashboard users


async def test_console_manages_client_users_and_keeps_one_admin(dash: DashHarness) -> None:
    ops = await _as(dash, "ops")
    tid = await make_tenant(dash.db, "users")
    url = f"{A}/tenants/{tid}/users"
    r = await dash.client.post(
        url, json={"email": "Owner@Acme.test", "role": "admin", "password": "temporary password"},
        headers=ops,
    )  # fmt: skip
    assert r.status_code == 201, r.text
    user = r.json()["users"][0]
    assert user["email"] == "owner@acme.test"
    r = await dash.client.post(
        url, json={"email": "owner@acme.test", "role": "agent", "password": "temporary password"},
        headers=ops,
    )  # fmt: skip
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "email_exists"

    # the only admin cannot be demoted or disabled
    for change in ({"role": "agent"}, {"is_active": False}):
        r = await dash.client.patch(f"{url}/{user['id']}", json=change, headers=ops)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "last_admin"

    # they can sign in with the password the console set; a reset ends that session
    login = await dash.client.post(
        "/api/v1/auth/login", json={"email": "owner@acme.test", "password": "temporary password"}
    )
    assert login.status_code == 200
    r = await dash.client.patch(
        f"{url}/{user['id']}", json={"password": "a brand new password"}, headers=ops
    )
    assert r.status_code == 200
    me = await dash.client.get(
        "/api/v1/team",
        headers={"authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.status_code == 401
    assert "update_member" in await _audit_actions(dash, str(tid))

    # another tenant's user id is simply not found
    other = await make_tenant(dash.db, "users2")
    stranger = await dash.user(other, "agent")
    r = await dash.client.patch(f"{url}/{stranger.id}", json={"is_active": False}, headers=ops)
    assert r.status_code == 404
    async with dash.db.platform_session() as s:
        row = await s.get(TenantUser, stranger.id)
    assert row is not None
    assert row.is_active


# ---------------------------------------------------------------- staff


async def test_only_owners_manage_staff(dash: DashHarness) -> None:
    ops = await _as(dash, "ops")
    assert (await dash.client.get(f"{A}/staff", headers=ops)).status_code == 403
    body = {"email": "new@hmhlabz.test", "role": "support", "password": PASSWORD}
    assert (await dash.client.post(f"{A}/staff", json=body, headers=ops)).status_code == 403


async def test_new_staff_get_a_one_time_enrolment_and_can_sign_in(dash: DashHarness) -> None:
    owner = await _as(dash, "owner")
    email = f"joiner-{uuid.uuid4().hex[:6]}@hmhlabz.test"
    r = await dash.client.post(
        f"{A}/staff", json={"email": email, "role": "support", "password": PASSWORD}, headers=owner
    )
    assert r.status_code == 201, r.text
    enrol = r.json()
    assert enrol["qr_svg"].startswith("data:image/svg+xml")
    secret = pyotp.parse_uri(enrol["otpauth_uri"]).secret
    assert enrol["staff"]["has_totp"] is True
    # the secret is never readable again
    listing = await dash.client.get(f"{A}/staff", headers=owner)
    assert secret not in listing.text
    login = await dash.client.post(
        "/api/v1/platform/auth/login",
        json={"email": email, "password": PASSWORD, "code": pyotp.TOTP(secret).now()},
    )
    assert login.status_code == 200

    # disabling them takes effect on their next request
    staff_id = enrol["staff"]["id"]
    r = await dash.client.patch(f"{A}/staff/{staff_id}", json={"is_active": False}, headers=owner)
    assert r.status_code == 200
    me = await dash.client.get(
        "/api/v1/platform/auth/me",
        headers={"authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.status_code == 401


async def test_the_last_owner_stays_and_owners_cannot_demote_themselves(
    dash: DashHarness,
) -> None:
    email, secret = await _staff(dash, "owner")
    owner = await _login(dash, email, secret)
    async with dash.db.platform_session() as s:
        me = await s.scalar(select(PlatformUser).where(PlatformUser.email == email))
        others = (
            await s.scalars(
                select(PlatformUser).where(
                    PlatformUser.role == "owner", PlatformUser.id != (me.id if me else None)
                )
            )
        ).all()
        for o in others:  # make this the only active owner
            o.is_active = False
    assert me is not None
    r = await dash.client.patch(f"{A}/staff/{me.id}", json={"role": "ops"}, headers=owner)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_yourself"
    r = await dash.client.post(f"{A}/staff/{me.id}/totp", headers=owner)
    assert r.status_code == 200  # resetting your own authenticator is allowed
    assert pyotp.parse_uri(r.json()["otpauth_uri"]).secret != secret


async def test_tenant_password_default_is_untouched_by_admin_routes(dash: DashHarness) -> None:
    """Regression guard: the shared team rules still serve the client's own Team page."""
    tid = await make_tenant(dash.db, "own")
    headers = await dash.login(tid, "admin")
    r = await dash.client.post(
        "/api/v1/team",
        json={"email": "agent@own.test", "role": "agent", "password": DEFAULT_PASSWORD},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    r = await dash.client.post(
        "/api/v1/team",
        json={"email": "AGENT@own.test", "role": "agent", "password": DEFAULT_PASSWORD},
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "email_exists"

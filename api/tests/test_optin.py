"""Phase 5: consent pages — dashboard management, the public page, the visit it records."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select, update

from api.config import get_settings
from api.db.models import OptinVisit, Tenant, TenantChannel
from api.optin.service import find_ref
from api.tests.conftest import DashHarness, make_tenant, meta_id

BODY = {
    "label": "Van 3 sticker",
    "source": "qr_van",
    "language": "en",
    "heading": "Offers on WhatsApp",
    "wording": "Yes, I'd like offers from <b>Acme</b> on WhatsApp. Reply STOP to stop.",
    "prefill": "Yes, please send me offers",
}


async def _tenant(dash: DashHarness, *, phone: str | None = "+971 50 000 1234") -> uuid.UUID:
    tid = await make_tenant(dash.db, "optin")
    async with dash.db.platform_session() as s:
        await s.execute(update(Tenant).where(Tenant.id == tid).values(name="Acme Water"))
        s.add(TenantChannel(tenant_id=tid, phone_number_id=meta_id(), display_phone=phone))
    return tid


async def test_admin_creates_a_link_and_everyone_sees_its_qr(dash: DashHarness) -> None:
    tid = await _tenant(dash)
    admin = await dash.login(tid, "admin")
    viewer = await dash.login(tid, "viewer")

    r = await dash.client.post("/api/v1/optin/links", json=BODY, headers=viewer)
    assert r.status_code == 403
    r = await dash.client.post("/api/v1/optin/links", json=BODY, headers=admin)
    assert r.status_code == 201, r.text
    link = r.json()
    assert link["url"] == f"https://test/q/{link['code']}"
    assert (link["visits"], link["opted_in"]) == (0, 0)

    r = await dash.client.get(f"/api/v1/optin/links/{link['id']}/qr", headers=viewer)
    assert r.status_code == 200
    assert r.json()["svg"].startswith("<svg")

    r = await dash.client.get("/api/v1/optin/links", headers=viewer)
    assert r.json()["whatsapp_ready"] is True
    assert [x["id"] for x in r.json()["links"]] == [link["id"]]

    r = await dash.client.get("/api/v1/optin/defaults?language=ar", headers=viewer)
    assert "Acme Water" in r.json()["wording"]


async def test_links_are_invisible_to_other_tenants(dash: DashHarness) -> None:
    a = await _tenant(dash)
    b = await _tenant(dash)
    r = await dash.client.post("/api/v1/optin/links", json=BODY, headers=await dash.login(a))
    link_id = r.json()["id"]
    other = await dash.login(b)
    assert (
        await dash.client.get(f"/api/v1/optin/links/{link_id}/qr", headers=other)
    ).status_code == 404
    r = await dash.client.patch(
        f"/api/v1/optin/links/{link_id}", json={"is_active": False}, headers=other
    )
    assert r.status_code == 404
    assert (await dash.client.get("/api/v1/optin/links", headers=other)).json()["links"] == []


async def test_page_shows_wording_escaped_and_is_not_indexed(dash: DashHarness) -> None:
    tid = await _tenant(dash)
    admin = await dash.login(tid)
    code = (await dash.client.post("/api/v1/optin/links", json=BODY, headers=admin)).json()["code"]

    r = await dash.client.get(f"/q/{code}")
    assert r.status_code == 200
    assert "&lt;b&gt;Acme&lt;/b&gt;" in r.text  # wording is text, never markup
    assert "Acme Water" in r.text
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    async with dash.db.tenant_session(tid) as s:
        assert (
            await s.scalars(select(OptinVisit))
        ).all() == []  # a view (or preview) is not a visit

    assert (await dash.client.get("/q/nosuchcode")).status_code == 404
    assert (await dash.client.get("/q/bad!code")).status_code == 404


async def test_continue_records_the_wording_and_hands_over_to_whatsapp(dash: DashHarness) -> None:
    tid = await _tenant(dash)
    admin = await dash.login(tid)
    link = (await dash.client.post("/api/v1/optin/links", json=BODY, headers=admin)).json()

    r = await dash.client.post(f"/q/{link['code']}")
    assert r.status_code == 303
    target = urlparse(r.headers["location"])
    assert (target.netloc, target.path) == ("wa.me", "/971500001234")
    text = parse_qs(target.query)["text"][0]
    assert text.startswith("Yes, please send me offers (Ref ")
    ref = find_ref(text)
    async with dash.db.tenant_session(tid) as s:
        visit = await s.scalar(select(OptinVisit).where(OptinVisit.token == ref))
    assert visit is not None
    assert visit.wording == BODY["wording"]
    assert visit.claimed_at is None

    # later edits change future visits only
    r = await dash.client.patch(
        f"/api/v1/optin/links/{link['id']}",
        json={"wording": "New wording for offers on WhatsApp, reply STOP to stop."},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["visits"] == 1
    async with dash.db.tenant_session(tid) as s:
        again = await s.get(OptinVisit, visit.id)
    assert again is not None
    assert again.wording == BODY["wording"]

    # a deactivated link stops working
    await dash.client.patch(
        f"/api/v1/optin/links/{link['id']}", json={"is_active": False}, headers=admin
    )
    assert (await dash.client.get(f"/q/{link['code']}")).status_code == 404
    assert (await dash.client.post(f"/q/{link['code']}")).status_code == 404


async def test_no_whatsapp_number_no_page(dash: DashHarness) -> None:
    tid = await _tenant(dash, phone=None)
    admin = await dash.login(tid)
    code = (await dash.client.post("/api/v1/optin/links", json=BODY, headers=admin)).json()["code"]
    assert (await dash.client.get(f"/q/{code}")).status_code == 404
    r = await dash.client.get("/api/v1/optin/links", headers=admin)
    assert r.json()["whatsapp_ready"] is False


async def test_continue_is_rate_limited(dash: DashHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "optin_rate_limit", 2)
    tid = await _tenant(dash)
    code = (
        await dash.client.post("/api/v1/optin/links", json=BODY, headers=await dash.login(tid))
    ).json()["code"]
    redis = dash.app.state.redis
    for key in await redis.keys("optin:rl:*"):
        await redis.delete(key)
    codes = [(await dash.client.post(f"/q/{code}")).status_code for _ in range(3)]
    assert codes == [303, 303, 429]


@pytest.mark.parametrize(
    "bad",
    [
        {"source": "QR Van"},
        {"wording": "too short"},
        {"language": "fr"},
        {"tenant_id": str(uuid.uuid4())},
    ],
)
async def test_link_input_is_validated(dash: DashHarness, bad: dict[str, str]) -> None:
    tid = await _tenant(dash)
    r = await dash.client.post(
        "/api/v1/optin/links", json={**BODY, **bad}, headers=await dash.login(tid)
    )
    assert r.status_code == 422

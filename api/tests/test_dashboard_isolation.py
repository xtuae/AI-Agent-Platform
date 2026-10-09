"""Phase 3 acceptance: a tenant-A token cannot read or change any tenant-B record — EVERY endpoint.

`test_every_route_has_a_cross_tenant_case` compares the app's real route table with CASES, so a
route added later without a case here fails the build.

Each case runs with tenant A's admin token (the strongest role) against a world where tenant B has
one of everything, all marked with B_MARK. Reads must not leak B ids or B_MARK; reads of B ids
must 404; writes aimed at B ids must 404 and leave B's rows as they were.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from api.core.crypto import encrypt_secret
from api.db.models import (
    Appointment,
    AppointmentResource,
    AppointmentType,
    AuditLog,
    AvailabilityException,
    AvailabilityRule,
    Campaign,
    CampaignRecipient,
    Conversation,
    CouponBook,
    Customer,
    Listing,
    Message,
    MessageTemplate,
    OptinLink,
    Order,
    Product,
    TenantChannel,
    TenantSettings,
    TenantUser,
    UsageDaily,
)
from api.metering import record_usage
from api.modules import admin as modules_admin
from api.optin.service import new_code, record_visit
from api.tests.conftest import DEFAULT_PASSWORD, DashHarness, TenantPair, make_channel, wamid

B_MARK = "BSECRET"


@dataclass
class Side:
    tenant: uuid.UUID
    customer: uuid.UUID
    product: uuid.UUID
    sku: str
    book: uuid.UUID
    order: uuid.UUID
    conversation: uuid.UUID
    user: uuid.UUID
    listing: uuid.UUID
    listing_ref: str
    atype: uuid.UUID
    resource: uuid.UUID
    exception: uuid.UUID
    appointment: uuid.UUID
    template: uuid.UUID
    campaign: uuid.UUID
    waba: str
    ids: set[str]


@dataclass
class World:
    a: Side
    b: Side
    headers: dict[str, str]  # tenant A, admin


async def _seed(h: DashHarness, tenant: uuid.UUID, customer: uuid.UUID, mark: str) -> Side:
    today = datetime.now(UTC).date()
    channel: TenantChannel = await make_channel(h.db, tenant)
    async with h.db.platform_session() as s:
        ch = await s.get(TenantChannel, channel.id)
        assert ch is not None
        ch.access_token_encrypted = encrypt_secret(f"token-{mark}")
        s.add(
            TenantSettings(
                tenant_id=tenant,
                escalation_phone="971500000999",
                business_hours={"mon": "08:00-20:00"},
                monthly_message_cap_aed=Decimal("100"),
            )
        )
    async with h.db.platform_session() as s:
        await modules_admin.enable(s, tenant, ["listings", "appointments", "campaigns"])
    user = await h.user(
        tenant, "agent", email=f"{mark.lower()}-{uuid.uuid4().hex[:6]}@example.test"
    )
    sku = f"{mark}-SKU-{uuid.uuid4().hex[:4]}"
    now = datetime.now(UTC)
    async with h.db.tenant_session(tenant) as s:
        c = await s.get(Customer, customer)
        assert c is not None
        c.name, c.area, c.address_note = f"{mark} Customer", f"{mark} Area", f"{mark} villa"
        c.opt_in_status = "opted_in"
        p = Product(sku=sku, name_en=f"{mark} Water", category="water", price_aed=Decimal("7"))
        s.add(p)
        book = CouponBook(
            customer_id=customer, sku=sku, bottles_total=10, bottles_free=1, bottles_remaining=9
        )
        s.add(book)
        order = Order(
            customer_id=customer,
            order_no=f"{mark}-1",
            status="confirmed",
            items=[{"sku": sku, "name": f"{mark} Water", "qty": 2, "unit_price_aed": "7.00"}],
            total_aed=Decimal("14.00"),
            source="agent",
            area=f"{mark} Area",
            delivery_date=today,
            notes=f"{mark} note",
        )
        s.add(order)
        conv = Conversation(
            customer_id=customer,
            channel_id=channel.id,
            state="awaiting_human",
            last_inbound_at=now,
            service_window_expires_at=now + timedelta(hours=23),
            summary=f"{mark} summary",
        )
        s.add(conv)
        await s.flush()
        s.add_all(
            [
                Message(
                    conversation_id=conv.id,
                    wamid=wamid(),
                    direction="in",
                    msg_type="text",
                    body=f"{mark} inbound body",
                    status="received",
                ),
                Message(
                    conversation_id=conv.id,
                    wamid=wamid(),
                    direction="out",
                    msg_type="text",
                    body=f"{mark} outbound body",
                    status="sent",
                ),
                AuditLog(
                    actor="agent",
                    action="escalate",
                    entity="conversation",
                    entity_id=conv.id,
                    after={"reason": "complaint", "summary": f"{mark} escalation"},
                ),
                AuditLog(actor="agent", action="create_order", entity="order", entity_id=order.id),
                UsageDaily(day=now.date(), msgs_out=3, meta_cost_aed=Decimal("0.5")),
            ]
        )
        await s.flush()

        # listings + appointments: one of everything, bookable every day 09:00-17:00
        listing = Listing(
            ref=f"{mark}-L1",
            title=f"{mark} flat",
            description=f"{mark} description",
            purpose="rent",
            property_type="apartment",
            area=f"{mark} Area",
            bedrooms=2,
            price_aed=Decimal("90000"),
            rent_period="year",
            status="available",
        )
        atype = AppointmentType(name_en=f"{mark} viewing", duration_min=30, location_kind="onsite")
        resource = AppointmentResource(name=f"{mark} agent")
        s.add_all([listing, atype, resource])
        await s.flush()
        s.add_all(
            AvailabilityRule(
                resource_id=resource.id, weekday=d, start_time=time(9), end_time=time(17)
            )
            for d in range(7)
        )
        exc = AvailabilityException(
            resource_id=resource.id, day=today + timedelta(days=20), reason=f"{mark} leave"
        )
        start = datetime.combine(today + timedelta(days=3), time(10), UTC)
        appt = Appointment(
            ref=f"A-{mark}",
            customer_id=customer,
            type_id=atype.id,
            resource_id=resource.id,
            subject_module="listings",
            subject_id=listing.id,
            subject_label=f"{mark} flat",
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            status="confirmed",
            notes=f"{mark} appointment note",
            source="dashboard",
        )
        template = MessageTemplate(
            name=f"{mark.lower()}_offer",
            language="en",
            category="MARKETING",
            body=f"Hi {{{{1}}}}, {mark} offer this week.",
            variables=[{"index": 1, "meaning": "name", "example": "Sara"}],
            meta_status="APPROVED",
        )
        s.add_all([exc, appt, template])
        await s.flush()
        camp = Campaign(
            name=f"{mark} campaign",
            template_id=template.id,
            segment_query={"opt_in_status": "opted_in"},
            variable_bindings=[{"source": "contact.first_name", "fallback": "there"}],
        )
        s.add(camp)
        await s.flush()
        s.add(
            CampaignRecipient(
                campaign_id=camp.id, customer_id=customer, status="skipped", skip_reason=mark
            )
        )
        ids = {
            str(x)
            for x in (
                customer, p.id, book.id, order.id, conv.id, user.id, channel.id,
                listing.id, atype.id, resource.id, exc.id, appt.id, template.id, camp.id,
            )
        }  # fmt: skip
        return Side(
            tenant, customer, p.id, sku, book.id, order.id, conv.id, user.id,
            listing.id, listing.ref, atype.id, resource.id, exc.id, appt.id, template.id, camp.id,
            channel.waba_id or "", ids,
        )  # fmt: skip


@pytest.fixture
async def world(dash: DashHarness, tenants: TenantPair) -> World:
    a = await _seed(dash, tenants.a, tenants.customer_a, "ASIDE")
    b = await _seed(dash, tenants.b, tenants.customer_b, B_MARK)
    return World(a=a, b=b, headers=await dash.login(tenants.a, "admin"))


def assert_clean(payload: Any, w: World) -> None:
    text = json.dumps(payload)
    assert B_MARK not in text, "tenant B data leaked"
    leaked = [i for i in w.b.ids | {str(w.b.tenant)} if i in text]
    assert not leaked, f"tenant B ids leaked: {leaked}"


async def b_snapshot(h: DashHarness, w: World) -> str:
    """Everything of tenant B's that an endpoint could change."""
    async with h.db.tenant_session(w.b.tenant) as s:
        rows: list[Any] = []
        for model in (
            Customer,
            Product,
            CouponBook,
            Order,
            Conversation,
            Message,
            Listing,
            AppointmentType,
            AppointmentResource,
            AvailabilityRule,
            AvailabilityException,
            Appointment,
            MessageTemplate,
            Campaign,
            CampaignRecipient,
        ):
            for obj in (await s.scalars(select(model))).all():
                rows.append({k: str(v) for k, v in vars(obj).items() if not k.startswith("_")})
    async with h.db.platform_session() as s:
        for obj in (
            await s.scalars(select(TenantUser).where(TenantUser.tenant_id == w.b.tenant))
        ).all():
            rows.append({k: str(v) for k, v in vars(obj).items() if not k.startswith("_")})
        st = await s.get(TenantSettings, w.b.tenant)
        rows.append({k: str(v) for k, v in vars(st).items() if not k.startswith("_")})
    return json.dumps(sorted(rows, key=json.dumps))


Case = Callable[[DashHarness, World], Awaitable[None]]
CASES: dict[tuple[str, str], Case] = {}


def case(method: str, path: str) -> Callable[[Case], Case]:
    def register(fn: Case) -> Case:
        CASES[(method, path)] = fn
        return fn

    return register


async def get_clean(h: DashHarness, w: World, url: str, **params: Any) -> Any:
    r = await h.client.get(url, headers=w.headers, params=params)
    assert r.status_code == 200, r.text
    assert_clean(r.json(), w)
    return r.json()


async def write_refused(
    h: DashHarness, w: World, method: str, url: str, body: dict[str, Any] | None = None
) -> None:
    before = await b_snapshot(h, w)
    r = await h.client.request(method, url, headers=w.headers, json=body)
    assert r.status_code == 404, (url, r.status_code, r.text)
    assert_clean(r.json(), w)
    assert await b_snapshot(h, w) == before, "tenant B changed"


# ---------------------------------------------------------------- auth


@case("POST", "/api/v1/auth/login")
async def _login(h: DashHarness, w: World) -> None:
    # B's user's email with A's password (and vice versa) never yields a session in B
    async with h.db.platform_session() as s:
        b_user = await s.get(TenantUser, w.b.user)
    assert b_user is not None
    r = await h.client.post(
        "/api/v1/auth/login", json={"email": b_user.email, "password": "not the password"}
    )
    assert r.status_code == 401


@case("POST", "/api/v1/auth/refresh")
async def _refresh(h: DashHarness, w: World) -> None:
    # A's cookie refreshes into A, never B (the cookie from `world`'s login is in the jar)
    r = await h.client.post("/api/v1/auth/refresh", headers={"x-hmh-csrf": "1"})
    assert r.status_code == 200
    assert r.json()["tenant"]["id"] == str(w.a.tenant)
    assert_clean(r.json(), w)


@case("POST", "/api/v1/auth/logout")
async def _logout(h: DashHarness, w: World) -> None:
    r = await h.client.post("/api/v1/auth/logout", headers={"x-hmh-csrf": "1"})
    assert r.status_code == 204


@case("GET", "/api/v1/auth/me")
async def _me(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/auth/me")
    assert body["tenant"]["id"] == str(w.a.tenant)


@case("POST", "/api/v1/auth/password")
async def _password(h: DashHarness, w: World) -> None:
    before = await b_snapshot(h, w)
    r = await h.client.post(
        "/api/v1/auth/password",
        headers=w.headers,
        json={
            "current_password": "correct horse battery staple",
            "new_password": "a different long passphrase",
        },
    )
    assert r.status_code == 200
    assert r.json()["tenant"]["id"] == str(w.a.tenant)
    assert await b_snapshot(h, w) == before


# ---------------------------------------------------------------- today + stream


@case("GET", "/api/v1/today")
async def _today(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/today")
    # B has identical data; if B leaked in, every count would double
    assert body["modules"]["orders"]["deliveries_due"] == 1
    assert body["awaiting_human"] == 1
    assert body["live_conversations"] == 1
    assert body["spend"]["meta_cost_aed"] == "0.50"
    assert body["spend"]["messages_out"] == 3


@case("GET", "/api/v1/stream")
async def _stream(h: DashHarness, w: World) -> None:
    # Full SSE behaviour is in test_events; here: the route is tenant-scoped end to end.
    from api.tests.test_events import stream_events

    events = await stream_events(h, w.a.tenant, w.b.tenant, w.a.customer, w.b.customer)
    ids = {e.get("id") for e in events}
    assert str(w.a.customer) in ids
    assert str(w.b.customer) not in ids


# ---------------------------------------------------------------- orders


@case("GET", "/api/v1/m/orders")
async def _orders(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/m/orders")
    assert [o["id"] for o in body["items"]] == [str(w.a.order)]
    # searching for B's markers finds nothing
    for q in (B_MARK, f"{B_MARK}-1", "971500000002"):
        assert (await get_clean(h, w, "/api/v1/m/orders", q=q))["total"] == 0
    assert (await get_clean(h, w, "/api/v1/m/orders", area=f"{B_MARK} Area"))["total"] == 0


@case("GET", "/api/v1/m/orders/areas")
async def _areas(h: DashHarness, w: World) -> None:
    assert await get_clean(h, w, "/api/v1/m/orders/areas") == ["ASIDE Area"]


@case("GET", "/api/v1/m/orders/delivery-list")
async def _delivery(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/m/orders/delivery-list")
    assert [g["area"] for g in body["groups"]] == ["ASIDE Area"]
    assert (await get_clean(h, w, "/api/v1/m/orders/delivery-list", area=f"{B_MARK} Area"))[
        "groups"
    ] == []


@case("GET", "/api/v1/m/orders/{order_id}")
async def _order(h: DashHarness, w: World) -> None:
    await get_clean(h, w, f"/api/v1/m/orders/{w.a.order}")
    r = await h.client.get(f"/api/v1/m/orders/{w.b.order}", headers=w.headers)
    assert r.status_code == 404
    assert_clean(r.json(), w)


@case("POST", "/api/v1/m/orders")
async def _create_order(h: DashHarness, w: World) -> None:
    # B's customer, and A's customer with B's SKU: both refused, nothing created anywhere
    await write_refused(
        h,
        w,
        "POST",
        "/api/v1/m/orders",
        {"customer_id": str(w.b.customer), "items": [{"sku": w.a.sku, "qty": 1}]},
    )
    before = await b_snapshot(h, w)
    r = await h.client.post(
        "/api/v1/m/orders",
        headers=w.headers,
        json={"customer_id": str(w.a.customer), "items": [{"sku": w.b.sku, "qty": 1}]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_sku"
    assert await b_snapshot(h, w) == before
    # and a body that names a tenant is rejected outright
    r = await h.client.post(
        "/api/v1/m/orders",
        headers=w.headers,
        json={
            "customer_id": str(w.a.customer),
            "items": [{"sku": w.a.sku, "qty": 1}],
            "tenant_id": str(w.b.tenant),
        },
    )
    assert r.status_code == 422


@case("PATCH", "/api/v1/m/orders/{order_id}")
async def _patch_order(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "PATCH", f"/api/v1/m/orders/{w.b.order}", {"status": "cancelled"})
    await write_refused(h, w, "PATCH", f"/api/v1/m/orders/{w.b.order}", {"notes": "x"})


# ---------------------------------------------------------------- conversations


@case("GET", "/api/v1/conversations")
async def _convs(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/conversations")
    assert [c["id"] for c in body["items"]] == [str(w.a.conversation)]
    for params in ({"state": "awaiting_human"}, {"live": "true"}, {"q": B_MARK}):
        await get_clean(h, w, "/api/v1/conversations", **params)


@case("GET", "/api/v1/conversations/{conversation_id}")
async def _thread(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, f"/api/v1/conversations/{w.a.conversation}")
    assert len(body["messages"]) == 2
    r = await h.client.get(f"/api/v1/conversations/{w.b.conversation}", headers=w.headers)
    assert r.status_code == 404


@case("POST", "/api/v1/conversations/{conversation_id}/takeover")
async def _takeover(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "POST", f"/api/v1/conversations/{w.b.conversation}/takeover")


@case("POST", "/api/v1/conversations/{conversation_id}/handback")
async def _handback(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "POST", f"/api/v1/conversations/{w.b.conversation}/handback")


@case("POST", "/api/v1/conversations/{conversation_id}/messages")
async def _reply(h: DashHarness, w: World) -> None:
    def never(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("no WhatsApp send for another tenant's conversation")

    h.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(never))
    await write_refused(
        h, w, "POST", f"/api/v1/conversations/{w.b.conversation}/messages", {"text": "hi"}
    )


# ---------------------------------------------------------------- customers


@case("GET", "/api/v1/contacts")
async def _customers(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/contacts")
    assert [c["id"] for c in body["items"]] == [str(w.a.customer)]
    for q in (B_MARK, "971500000002", "500000002"):
        assert (await get_clean(h, w, "/api/v1/contacts", q=q))["total"] == 0


@case("GET", "/api/v1/contacts/{customer_id}")
async def _customer(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, f"/api/v1/contacts/{w.a.customer}")
    assert body["modules"]["coupons"]["bottles_remaining"] == 9
    r = await h.client.get(f"/api/v1/contacts/{w.b.customer}", headers=w.headers)
    assert r.status_code == 404


@case("POST", "/api/v1/contacts")
async def _create_customer(h: DashHarness, w: World) -> None:
    # B already has 971500000002; in A that number is new, so A gets its own row and B is untouched
    before = await b_snapshot(h, w)
    r = await h.client.post(
        "/api/v1/contacts", headers=w.headers, json={"wa_id": "971500000002", "name": "Same phone"}
    )
    assert r.status_code == 201, r.text
    assert_clean(r.json(), w)
    assert await b_snapshot(h, w) == before
    r = await h.client.post(
        "/api/v1/contacts",
        headers=w.headers,
        json={"wa_id": "971500000777", "tenant_id": str(w.b.tenant)},
    )
    assert r.status_code == 422


@case("PATCH", "/api/v1/contacts/{customer_id}")
async def _patch_customer(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "PATCH", f"/api/v1/contacts/{w.b.customer}", {"opt_out": True})
    await write_refused(h, w, "PATCH", f"/api/v1/contacts/{w.b.customer}", {"name": "x"})


# ---------------------------------------------------------------- settings, products, team


@case("GET", "/api/v1/settings")
async def _settings(h: DashHarness, w: World) -> None:
    await get_clean(h, w, "/api/v1/settings")


@case("PATCH", "/api/v1/settings")
async def _patch_settings(h: DashHarness, w: World) -> None:
    before = await b_snapshot(h, w)
    r = await h.client.patch(
        "/api/v1/settings", headers=w.headers, json={"escalation_phone": "+971 50 111 2222"}
    )
    assert r.status_code == 200
    assert r.json()["escalation_phone"] == "971501112222"
    assert await b_snapshot(h, w) == before


@case("GET", "/api/v1/m/catalog/products")
async def _products(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/m/catalog/products")
    assert [p["id"] for p in body] == [str(w.a.product)]


@case("POST", "/api/v1/m/catalog/products")
async def _create_product(h: DashHarness, w: World) -> None:
    before = await b_snapshot(h, w)
    r = await h.client.post(
        "/api/v1/m/catalog/products",
        headers=w.headers,
        json={"sku": w.b.sku, "name_en": "Same SKU as B", "category": "snack", "price_aed": "1"},
    )
    assert r.status_code == 201  # SKUs are per tenant: B's SKU is free in A
    assert await b_snapshot(h, w) == before


@case("PATCH", "/api/v1/m/catalog/products/{product_id}")
async def _patch_product(h: DashHarness, w: World) -> None:
    await write_refused(
        h, w, "PATCH", f"/api/v1/m/catalog/products/{w.b.product}", {"price_aed": "0.01"}
    )


@case("GET", "/api/v1/team")
async def _team(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/team")
    assert str(w.a.user) in {m["id"] for m in body}


@case("POST", "/api/v1/team")
async def _add_member(h: DashHarness, w: World) -> None:
    before = await b_snapshot(h, w)
    r = await h.client.post(
        "/api/v1/team",
        headers=w.headers,
        json={
            "email": f"new-{uuid.uuid4().hex[:6]}@example.test",
            "role": "viewer",
            "password": "temporary passphrase",
        },
    )
    assert r.status_code == 201
    assert await b_snapshot(h, w) == before


@case("PATCH", "/api/v1/team/{user_id}")
async def _patch_member(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "PATCH", f"/api/v1/team/{w.b.user}", {"is_active": False})
    await write_refused(h, w, "PATCH", f"/api/v1/team/{w.b.user}", {"password": "x" * 20})


# ---------------------------------------------------------------- listings


@case("GET", "/api/v1/m/listings")
async def _listings(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/m/listings")
    assert [x["id"] for x in body["items"]] == [str(w.a.listing)]
    for q in (B_MARK, w.b.listing_ref):
        assert (await get_clean(h, w, "/api/v1/m/listings", q=q))["total"] == 0


@case("GET", "/api/v1/m/listings/{listing_id}")
async def _listing(h: DashHarness, w: World) -> None:
    await get_clean(h, w, f"/api/v1/m/listings/{w.a.listing}")
    r = await h.client.get(f"/api/v1/m/listings/{w.b.listing}", headers=w.headers)
    assert r.status_code == 404


@case("POST", "/api/v1/m/listings")
async def _create_listing(h: DashHarness, w: World) -> None:
    before = await b_snapshot(h, w)
    body = {"ref": w.b.listing_ref, "title": "Same ref as B", "purpose": "sale"}
    r = await h.client.post(
        "/api/v1/m/listings", headers=w.headers, json={**body, "property_type": "villa"}
    )
    assert r.status_code == 201, r.text  # refs are per tenant
    assert await b_snapshot(h, w) == before
    r = await h.client.post(
        "/api/v1/m/listings",
        headers=w.headers,
        json={**body, "ref": "X-2", "property_type": "villa", "tenant_id": str(w.b.tenant)},
    )
    assert r.status_code == 422


@case("PATCH", "/api/v1/m/listings/{listing_id}")
async def _patch_listing(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "PATCH", f"/api/v1/m/listings/{w.b.listing}", {"status": "sold"})


# ---------------------------------------------------------------- appointments


@case("GET", "/api/v1/m/appointments")
async def _appointments(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/m/appointments", to=_in_days(7))
    assert [x["id"] for x in body["items"]] == [str(w.a.appointment)]
    for params in (
        {"resource_id": str(w.b.resource)},
        {"type_id": str(w.b.atype)},
        {"customer_id": str(w.b.customer)},
    ):
        assert (await get_clean(h, w, "/api/v1/m/appointments", **params))["total"] == 0


def _in_days(n: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=n)).isoformat()


@case("GET", "/api/v1/m/appointments/availability")
async def _availability(h: DashHarness, w: World) -> None:
    body = await get_clean(
        h, w, "/api/v1/m/appointments/availability", type_id=str(w.a.atype), to=_in_days(3)
    )
    assert body
    assert {r for x in body for r in x["resource_ids"]} == {str(w.a.resource)}
    r = await h.client.get(
        "/api/v1/m/appointments/availability",
        headers=w.headers,
        params={"type_id": str(w.b.atype)},
    )
    assert r.status_code == 404
    # A's type on B's resource: B's resource is invisible, so no slots
    assert (
        await get_clean(
            h,
            w,
            "/api/v1/m/appointments/availability",
            type_id=str(w.a.atype),
            resource_id=str(w.b.resource),
        )
        == []
    )


@case("GET", "/api/v1/m/appointments/{appointment_id}")
async def _appointment(h: DashHarness, w: World) -> None:
    await get_clean(h, w, f"/api/v1/m/appointments/{w.a.appointment}")
    r = await h.client.get(f"/api/v1/m/appointments/{w.b.appointment}", headers=w.headers)
    assert r.status_code == 404


@case("POST", "/api/v1/m/appointments")
async def _book(h: DashHarness, w: World) -> None:
    start = f"{_in_days(5)}T11:00"
    ok = {"customer_id": str(w.a.customer), "type_id": str(w.a.atype), "starts_at": start}
    await write_refused(
        h, w, "POST", "/api/v1/m/appointments", {**ok, "customer_id": str(w.b.customer)}
    )
    await write_refused(h, w, "POST", "/api/v1/m/appointments", {**ok, "type_id": str(w.b.atype)})
    await write_refused(
        h, w, "POST", "/api/v1/m/appointments", {**ok, "resource_id": str(w.b.resource)}
    )
    before = await b_snapshot(h, w)
    r = await h.client.post(
        "/api/v1/m/appointments",
        headers=w.headers,
        json={**ok, "subject": {"listing_ref": w.b.listing_ref}},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "listing_not_available"
    assert await b_snapshot(h, w) == before
    r = await h.client.post("/api/v1/m/appointments", headers=w.headers, json=ok)
    assert r.status_code == 201, r.text
    assert_clean(r.json(), w)
    assert r.json()["resource"]["id"] == str(w.a.resource)


@case("PATCH", "/api/v1/m/appointments/{appointment_id}")
async def _patch_appointment(h: DashHarness, w: World) -> None:
    url = f"/api/v1/m/appointments/{w.b.appointment}"
    await write_refused(h, w, "PATCH", url, {"status": "cancelled"})
    await write_refused(h, w, "PATCH", url, {"starts_at": f"{_in_days(6)}T12:00"})
    # A's appointment onto B's resource
    await write_refused(
        h,
        w,
        "PATCH",
        f"/api/v1/m/appointments/{w.a.appointment}",
        {"resource_id": str(w.b.resource)},
    )


@case("GET", "/api/v1/m/appointments/types")
async def _types(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/m/appointments/types")
    assert [x["id"] for x in body] == [str(w.a.atype)]


@case("POST", "/api/v1/m/appointments/types")
async def _create_type(h: DashHarness, w: World) -> None:
    before = await b_snapshot(h, w)
    r = await h.client.post(
        "/api/v1/m/appointments/types",
        headers=w.headers,
        json={"name_en": "Consultation", "duration_min": 45, "location_kind": "office"},
    )
    assert r.status_code == 201
    assert await b_snapshot(h, w) == before


@case("PATCH", "/api/v1/m/appointments/types/{type_id}")
async def _patch_type(h: DashHarness, w: World) -> None:
    await write_refused(
        h, w, "PATCH", f"/api/v1/m/appointments/types/{w.b.atype}", {"is_active": False}
    )


@case("GET", "/api/v1/m/appointments/resources")
async def _resources(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/m/appointments/resources")
    assert [x["id"] for x in body] == [str(w.a.resource)]


@case("POST", "/api/v1/m/appointments/resources")
async def _create_resource(h: DashHarness, w: World) -> None:
    # linking a resource to B's team member is refused
    await write_refused(
        h,
        w,
        "POST",
        "/api/v1/m/appointments/resources",
        {"name": "Linked", "tenant_user_id": str(w.b.user)},
    )
    r = await h.client.post(
        "/api/v1/m/appointments/resources",
        headers=w.headers,
        json={"name": "Room 2", "tenant_user_id": str(w.a.user)},
    )
    assert r.status_code == 201


@case("PATCH", "/api/v1/m/appointments/resources/{resource_id}")
async def _patch_resource(h: DashHarness, w: World) -> None:
    await write_refused(
        h, w, "PATCH", f"/api/v1/m/appointments/resources/{w.b.resource}", {"is_active": False}
    )
    await write_refused(
        h,
        w,
        "PATCH",
        f"/api/v1/m/appointments/resources/{w.a.resource}",
        {"tenant_user_id": str(w.b.user)},
    )


@case("GET", "/api/v1/m/appointments/resources/{resource_id}/hours")
async def _hours(h: DashHarness, w: World) -> None:
    assert len(await get_clean(h, w, f"/api/v1/m/appointments/resources/{w.a.resource}/hours")) == 7
    r = await h.client.get(
        f"/api/v1/m/appointments/resources/{w.b.resource}/hours", headers=w.headers
    )
    assert r.status_code == 404


@case("PUT", "/api/v1/m/appointments/resources/{resource_id}/hours")
async def _set_hours(h: DashHarness, w: World) -> None:
    await write_refused(
        h,
        w,
        "PUT",
        f"/api/v1/m/appointments/resources/{w.b.resource}/hours",
        {"hours": []},
    )


@case("GET", "/api/v1/m/appointments/exceptions")
async def _exceptions(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, "/api/v1/m/appointments/exceptions")
    assert [x["id"] for x in body] == [str(w.a.exception)]


@case("POST", "/api/v1/m/appointments/exceptions")
async def _create_exception(h: DashHarness, w: World) -> None:
    await write_refused(
        h,
        w,
        "POST",
        "/api/v1/m/appointments/exceptions",
        {"resource_id": str(w.b.resource), "day": _in_days(9)},
    )
    r = await h.client.post(
        "/api/v1/m/appointments/exceptions", headers=w.headers, json={"day": _in_days(9)}
    )
    assert r.status_code == 201  # "everyone" means everyone in A


@case("DELETE", "/api/v1/m/appointments/exceptions/{exception_id}")
async def _delete_exception(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "DELETE", f"/api/v1/m/appointments/exceptions/{w.b.exception}")


# ---------------------------------------------------------------- campaigns


C = "/api/v1/m/campaigns"


class _Meta:
    """Graph API stand-in that remembers every URL (to prove only A's account is touched)."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if request.url.path.endswith("/message_templates") and request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"id": "1", "status": "PENDING"})


@case("GET", f"{C}/guardrails")
async def _guardrails(h: DashHarness, w: World) -> None:
    await get_clean(h, w, f"{C}/guardrails")


@case("GET", f"{C}/templates")
async def _templates(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, f"{C}/templates")
    assert [t["id"] for t in body] == [str(w.a.template)]


@case("POST", f"{C}/templates/check")
async def _check(h: DashHarness, w: World) -> None:
    r = await h.client.post(f"{C}/templates/check", headers=w.headers, json={"body": "Hi there."})
    assert r.status_code == 200


@case("POST", f"{C}/templates/draft")
async def _draft(h: DashHarness, w: World) -> None:
    from api.llm.router import LLMResult

    prompts: list[str] = []

    class LLM:
        async def chat(self, **kw: Any) -> LLMResult:
            prompts.append(kw["messages"][0]["content"])
            out = {
                "name": "x_offer",
                "category": "MARKETING",
                "language": "en",
                "body": "Hi there, new stock.",
            }
            return LLMResult(json.dumps(out), [], "gemini", "m", 1, 1, 1, Decimal(0))

    h.app.state.llm = LLM()
    before = await b_snapshot(h, w)
    r = await h.client.post(
        f"{C}/templates/draft", headers=w.headers, json={"objective": "restock news"}
    )
    assert r.status_code == 200, r.text
    assert B_MARK not in prompts[0]
    assert await b_snapshot(h, w) == before


@case("POST", f"{C}/templates")
async def _create_template(h: DashHarness, w: World) -> None:
    before = await b_snapshot(h, w)
    r = await h.client.post(
        f"{C}/templates",
        headers=w.headers,
        json={
            "name": f"{B_MARK.lower()}_offer",
            "language": "en",
            "body": "Hi there, new stock this week.",
        },
    )
    assert r.status_code == 201, r.text  # template names are per tenant
    assert await b_snapshot(h, w) == before


@case("PATCH", f"{C}/templates/{{template_id}}")
async def _patch_template(h: DashHarness, w: World) -> None:
    await write_refused(
        h, w, "PATCH", f"{C}/templates/{w.b.template}", {"body": "Hi there, changed."}
    )


@case("POST", f"{C}/templates/{{template_id}}/submit")
async def _submit_template(h: DashHarness, w: World) -> None:
    meta = _Meta()
    h.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(meta))
    await write_refused(h, w, "POST", f"{C}/templates/{w.b.template}/submit")
    assert meta.urls == []


@case("POST", f"{C}/templates/sync")
async def _sync(h: DashHarness, w: World) -> None:
    meta = _Meta()
    h.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(meta))
    before = await b_snapshot(h, w)
    r = await h.client.post(f"{C}/templates/sync", headers=w.headers)
    assert r.status_code == 200, r.text
    assert meta.urls
    assert all(w.a.waba in u and w.b.waba not in u for u in meta.urls)
    assert await b_snapshot(h, w) == before


@case("GET", f"{C}/segment-fields")
async def _segment_fields(h: DashHarness, w: World) -> None:
    await get_clean(h, w, f"{C}/segment-fields")


@case("POST", f"{C}/segments/count")
async def _count(h: DashHarness, w: World) -> None:
    r = await h.client.post(f"{C}/segments/count", headers=w.headers, json={"definition": {}})
    assert r.json()["recipients"] == 1  # A's one opted-in customer, not B's
    r = await h.client.post(
        f"{C}/segments/count",
        headers=w.headers,
        json={"definition": {"area_in": [f"{B_MARK} Area"]}},
    )
    assert r.json()["recipients"] == 0


@case("GET", C)
async def _campaigns(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, C)
    assert [c["id"] for c in body] == [str(w.a.campaign)]


@case("POST", C)
async def _create_campaign(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "POST", C, {"name": "x", "template_id": str(w.b.template)})
    r = await h.client.post(
        C, headers=w.headers, json={"name": "x", "segment": {}, "tenant_id": str(w.b.tenant)}
    )
    assert r.status_code == 422


@case("GET", f"{C}/{{campaign_id}}")
async def _campaign(h: DashHarness, w: World) -> None:
    await get_clean(h, w, f"{C}/{w.a.campaign}")
    assert (await h.client.get(f"{C}/{w.b.campaign}", headers=w.headers)).status_code == 404


@case("PATCH", f"{C}/{{campaign_id}}")
async def _patch_campaign(h: DashHarness, w: World) -> None:
    await write_refused(h, w, "PATCH", f"{C}/{w.b.campaign}", {"name": "x"})
    # A's draft pointed at B's template
    await write_refused(h, w, "PATCH", f"{C}/{w.a.campaign}", {"template_id": str(w.b.template)})


@case("GET", f"{C}/{{campaign_id}}/preview")
async def _preview(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, f"{C}/{w.a.campaign}/preview")
    assert body["recipients"] == 1
    assert (await h.client.get(f"{C}/{w.b.campaign}/preview", headers=w.headers)).status_code == 404


@case("GET", f"{C}/{{campaign_id}}/recipients")
async def _recipients(h: DashHarness, w: World) -> None:
    body = await get_clean(h, w, f"{C}/{w.a.campaign}/recipients")
    assert body["total"] == 1
    r = await h.client.get(f"{C}/{w.b.campaign}/recipients", headers=w.headers)
    assert r.status_code == 404


def _transition(action: str) -> Case:
    async def run(h: DashHarness, w: World) -> None:
        await write_refused(h, w, "POST", f"{C}/{w.b.campaign}/{action}")

    return run


for _action in ("approve", "start", "pause", "resume", "cancel"):
    case("POST", f"{C}/{{campaign_id}}/{_action}")(_transition(_action))


# ---------------------------------------------------------------- Phase 5: opt-in links, costs

OPT = "/api/v1/optin"
LINK_BODY = {
    "label": "Van",
    "source": "qr_van",
    "heading": "Offers on WhatsApp",
    "wording": "Yes, send me offers on WhatsApp. Reply STOP to stop.",
    "prefill": "Yes please",
}


async def _b_link(h: DashHarness, w: World) -> OptinLink:
    async with h.db.platform_session() as s:
        link = OptinLink(
            tenant_id=w.b.tenant,
            code=new_code(),
            label=B_MARK,
            source="qr_van",
            heading=B_MARK,
            wording=f"{B_MARK} consent wording for offers on WhatsApp.",
            prefill=B_MARK,
        )
        s.add(link)
    async with h.db.tenant_session(w.b.tenant) as s:
        await record_visit(s, link)
    return link


async def _b_link_state(h: DashHarness, link_id: uuid.UUID) -> tuple[str, str, bool]:
    async with h.db.platform_session() as s:
        link = await s.get(OptinLink, link_id)
    assert link is not None
    return link.label, link.wording, link.is_active


@case("GET", f"{OPT}/links")
async def _optin_links(h: DashHarness, w: World) -> None:
    await _b_link(h, w)
    body = await get_clean(h, w, f"{OPT}/links")
    assert body["links"] == []


@case("GET", f"{OPT}/defaults")
async def _optin_defaults(h: DashHarness, w: World) -> None:
    await get_clean(h, w, f"{OPT}/defaults")


@case("POST", f"{OPT}/links")
async def _optin_create(h: DashHarness, w: World) -> None:
    r = await h.client.post(
        f"{OPT}/links", headers=w.headers, json={**LINK_BODY, "tenant_id": str(w.b.tenant)}
    )
    assert r.status_code == 422
    r = await h.client.post(f"{OPT}/links", headers=w.headers, json=LINK_BODY)
    assert r.status_code == 201
    async with h.db.platform_session() as s:
        link = await s.get(OptinLink, uuid.UUID(r.json()["id"]))
    assert link is not None
    assert link.tenant_id == w.a.tenant


@case("PATCH", f"{OPT}/links/{{link_id}}")
async def _optin_patch(h: DashHarness, w: World) -> None:
    link = await _b_link(h, w)
    before = await _b_link_state(h, link.id)
    r = await h.client.patch(f"{OPT}/links/{link.id}", headers=w.headers, json={"is_active": False})
    assert r.status_code == 404
    assert_clean(r.json(), w)
    assert await _b_link_state(h, link.id) == before


@case("GET", f"{OPT}/links/{{link_id}}/qr")
async def _optin_qr(h: DashHarness, w: World) -> None:
    link = await _b_link(h, w)
    r = await h.client.get(f"{OPT}/links/{link.id}/qr", headers=w.headers)
    assert r.status_code == 404
    assert link.code not in r.text


@case("GET", "/api/v1/costs")
async def _costs(h: DashHarness, w: World) -> None:
    day = datetime.now(UTC)
    async with h.db.tenant_session(w.b.tenant) as s:
        await record_usage(
            s, w.b.tenant, at=day, msgs_out=1, category="marketing", meta_cost_aed=Decimal("777")
        )
    body = await get_clean(h, w, "/api/v1/costs")
    assert "777" not in json.dumps(body)


# The console is HMH Labz's, not a tenant's: a tenant token (or tenant credentials) gets nowhere.
P = "/api/v1/platform"
A = f"{P}/admin"
_X = "00000000-0000-4000-8000-000000000001"  # any id: the token is refused first


def _console_refused(method: str, url: str, body: dict[str, Any] | None = None) -> Case:
    async def run(h: DashHarness, w: World) -> None:
        r = await h.client.request(method, url.format(tid=w.b.tenant), headers=w.headers, json=body)
        assert r.status_code == 401, (url, r.status_code)
        assert_clean(r.json(), w)

    return run


for _m, _path, _url, _body in (
    ("GET", f"{P}/overview", f"{P}/overview", None),
    ("GET", f"{P}/tenants/{{tenant_id}}", f"{P}/tenants/{{tid}}", None),
    (
        "POST",
        f"{P}/tenants/{{tenant_id}}/statement/pull",
        f"{P}/tenants/{{tid}}/statement/pull",
        None,
    ),
    ("GET", f"{P}/reimbursements", f"{P}/reimbursements", None),
    (
        "POST",
        f"{P}/reimbursements",
        f"{P}/reimbursements",
        {"tenant_id": "{tid}", "service_month": 1},
    ),
    ("GET", f"{P}/auth/me", f"{P}/auth/me", None),
    # the admin API (create/edit clients, numbers, tokens, users, staff)
    ("GET", f"{A}/catalogue", f"{A}/catalogue", None),
    ("POST", f"{A}/tenants", f"{A}/tenants", {"slug": "x", "name": "x"}),
    ("GET", f"{A}/tenants/{{tenant_id}}", f"{A}/tenants/{{tid}}", None),
    ("PATCH", f"{A}/tenants/{{tenant_id}}", f"{A}/tenants/{{tid}}", {"status": "churned"}),
    ("PUT", f"{A}/tenants/{{tenant_id}}/modules", f"{A}/tenants/{{tid}}/modules", {"enabled": []}),
    ("GET", f"{A}/tenants/{{tenant_id}}/dpa", f"{A}/tenants/{{tid}}/dpa", None),
    ("POST", f"{A}/tenants/{{tenant_id}}/channels", f"{A}/tenants/{{tid}}/channels", None),
    ("PATCH", f"{A}/channels/{{channel_id}}", f"{A}/channels/{_X}", {"is_active": False}),
    ("PUT", f"{A}/channels/{{channel_id}}/token", f"{A}/channels/{_X}/token", None),
    ("POST", f"{A}/tenants/{{tenant_id}}/users", f"{A}/tenants/{{tid}}/users", None),
    (
        "PATCH",
        f"{A}/tenants/{{tenant_id}}/users/{{user_id}}",
        f"{A}/tenants/{{tid}}/users/{_X}",
        {"is_active": False},
    ),
    ("GET", f"{A}/staff", f"{A}/staff", None),
    ("POST", f"{A}/staff", f"{A}/staff", None),
    ("PATCH", f"{A}/staff/{{staff_id}}", f"{A}/staff/{_X}", {"is_active": False}),
    ("POST", f"{A}/staff/{{staff_id}}/totp", f"{A}/staff/{_X}/totp", None),
):
    case(_m, _path)(_console_refused(_m, _url, _body))


@case("POST", f"{P}/auth/login")
async def _console_login(h: DashHarness, w: World) -> None:
    async with h.db.platform_session() as s:
        user = await s.get(TenantUser, w.a.user)
    assert user is not None
    r = await h.client.post(
        f"{P}/auth/login",
        json={"email": user.email, "password": DEFAULT_PASSWORD, "code": "123456"},
    )
    assert r.status_code == 401


@case("POST", f"{P}/auth/refresh")
async def _console_refresh(h: DashHarness, w: World) -> None:
    r = await h.client.post(f"{P}/auth/refresh", headers={**w.headers, "x-hmh-csrf": "1"})
    assert r.status_code == 401


@case("POST", f"{P}/auth/logout")
async def _console_logout(h: DashHarness, w: World) -> None:
    r = await h.client.post(f"{P}/auth/logout", headers={**w.headers, "x-hmh-csrf": "1"})
    assert r.status_code == 204  # nothing to end; harmless


# ---------------------------------------------------------------- the tests


def test_every_route_has_a_cross_tenant_case(dash_app_routes: set[tuple[str, str]]) -> None:
    assert dash_app_routes == set(CASES), (
        f"missing cases: {sorted(dash_app_routes - set(CASES))}; "
        f"stale cases: {sorted(set(CASES) - dash_app_routes)}"
    )


@pytest.fixture(scope="module")
def dash_app_routes() -> set[tuple[str, str]]:
    from api.main import create_app

    # The OpenAPI document lists every mounted operation, however routers are nested.
    paths: dict[str, dict[str, Any]] = create_app().openapi()["paths"]
    return {
        (method.upper(), path)
        for path, ops in paths.items()
        if path.startswith("/api/")
        for method in ops
        if method in {"get", "post", "put", "patch", "delete"}
    }


@pytest.mark.parametrize("key", sorted(CASES), ids=lambda k: f"{k[0]} {k[1]}")
async def test_tenant_a_token_cannot_reach_tenant_b(
    dash: DashHarness, world: World, key: tuple[str, str]
) -> None:
    await CASES[key](dash, world)

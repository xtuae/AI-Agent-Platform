"""Dashboard flows: an order end to end without Excel, status rules, coupons, conversations
take-over / reply / hand-back, customers, settings, and the Today tiles."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from api.core.crypto import encrypt_secret
from api.db.models import (
    AuditLog,
    Conversation,
    CouponBook,
    Customer,
    Message,
    Order,
    Product,
    Tenant,
    TenantChannel,
    TenantSettings,
    UsageDaily,
)
from api.tests.conftest import DashHarness, TenantPair, make_channel, wamid


@pytest.fixture
async def shop(dash: DashHarness, tenants: TenantPair) -> dict[str, Any]:
    async with dash.db.tenant_session(tenants.a) as s:
        s.add_all(
            [
                Product(
                    sku="CAN-5G",
                    name_en="5 gallon can",
                    category="water",
                    price_aed=Decimal("7.00"),
                ),
                Product(sku="CHIPS", name_en="Chips", category="snack", price_aed=Decimal("2.50")),
                Product(
                    sku="OLD",
                    name_en="Retired",
                    category="snack",
                    price_aed=Decimal("1"),
                    is_active=False,
                ),
            ]
        )
    return {
        "agent": await dash.login(tenants.a, "agent"),
        "viewer": await dash.login(tenants.a, "viewer"),
        "admin": await dash.login(tenants.a, "admin"),
    }


async def _today(dash: DashHarness, tenant: uuid.UUID) -> date:
    from zoneinfo import ZoneInfo

    async with dash.db.platform_session() as s:
        t = await s.get(Tenant, tenant)
    assert t is not None
    return datetime.now(UTC).astimezone(ZoneInfo(t.timezone)).date()


# ---------------------------------------------------------------- acceptance: an order end to end


async def test_phone_order_end_to_end_without_excel(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    """Zameer takes a phone order from a new customer, prints the van list, and closes it out."""
    h, agent = dash.client, shop["agent"]
    today = await _today(dash, tenants.a)

    # 1. new customer, typed the way people type UAE numbers
    r = await h.post(
        "/api/v1/customers",
        headers=agent,
        json={
            "wa_id": "050 123 4567",
            "name": "Fatima",
            "area": "JLT",
            "address_note": "Cluster D, 1204",
        },
    )
    assert r.status_code == 201, r.text
    customer = r.json()
    assert customer["wa_id"] == "971501234567"
    assert customer["source"] == "dashboard"
    assert customer["opt_in_status"] == "pending"

    # 2. the order — the client sends SKUs and quantities, the server prices it
    r = await h.post(
        "/api/v1/orders",
        headers={**agent, "idempotency-key": "tap-1"},
        json={
            "customer_id": customer["id"],
            "items": [
                {"sku": "CAN-5G", "qty": 3},
                {"sku": "CHIPS", "qty": 2},
                {"sku": "CAN-5G", "qty": 1},
            ],
            "delivery_date": today.isoformat(),
            "delivery_slot": "evening",
        },
    )
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["total_aed"] == "33.00"  # 4 x 7.00 + 2 x 2.50, from the products table
    assert {(i["sku"], i["qty"]) for i in order["items"]} == {("CAN-5G", 4), ("CHIPS", 2)}
    assert order["area"] == "JLT"
    assert order["source"] == "dashboard"
    assert order["status"] == "confirmed"
    assert order["next_statuses"] == ["cancelled", "out_for_delivery"]
    assert int(order["order_no"]) >= 1001

    # a double-tap on "Place order" returns the same order
    again = await h.post(
        "/api/v1/orders",
        headers={**agent, "idempotency-key": "tap-1"},
        json={"customer_id": customer["id"], "items": [{"sku": "CAN-5G", "qty": 4}]},
    )
    assert again.status_code == 200
    assert again.json()["id"] == order["id"]

    # 3. it is on today's delivery list, grouped by area, with what to collect
    dl = (await h.get("/api/v1/orders/delivery-list", headers=agent)).json()
    jlt = next(g for g in dl["groups"] if g["area"] == "JLT")
    assert jlt["bottles"] == 6
    assert jlt["to_collect_aed"] == "33.00"
    assert jlt["stops"][0]["address_note"] == "Cluster D, 1204"

    # 4. out for delivery → delivered
    for status in ("out_for_delivery", "delivered"):
        r = await h.patch(f"/api/v1/orders/{order['id']}", headers=agent, json={"status": status})
        assert r.status_code == 200, r.text
    detail = (await h.get(f"/api/v1/orders/{order['id']}", headers=agent)).json()
    assert detail["status"] == "delivered"
    assert detail["next_statuses"] == []
    assert [e["action"] for e in detail["history"]] == [
        "create_order",
        "update_order",
        "update_order",
    ]
    assert all(e["actor"].startswith("user:") for e in detail["history"])

    # 5. the customer's history shows it
    c = (await h.get(f"/api/v1/customers/{customer['id']}", headers=agent)).json()
    assert c["lifetime_orders"] == 1
    assert c["orders"][0]["order_no"] == order["order_no"]

    # 6. and the Today screen counts it
    t = (await h.get("/api/v1/today", headers=agent)).json()
    assert t["orders"]["count"] == 1
    assert t["orders"]["value_aed"] == "33.00"
    assert t["orders_by_day"][-1] == {"day": today.isoformat(), "orders": 1, "value_aed": "33.00"}


async def test_client_cannot_price_an_order(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    r = await dash.client.post(
        "/api/v1/orders",
        headers=shop["agent"],
        json={
            "customer_id": str(tenants.customer_a),
            "items": [{"sku": "CAN-5G", "qty": 1, "unit_price_aed": "0.01"}],
            "total_aed": "0.01",
        },
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("items", "extra", "code"),
    [
        ([{"sku": "NOPE", "qty": 1}], {}, "unknown_sku"),
        ([{"sku": "OLD", "qty": 1}], {}, "unknown_sku"),
        ([{"sku": "CHIPS", "qty": 1}], {"use_coupon_book": True}, "coupon_not_applicable"),
        ([{"sku": "CAN-5G", "qty": 2}], {"use_coupon_book": True}, "insufficient_coupon_balance"),
        ([{"sku": "CAN-5G", "qty": 1}], {"delivery_date": "2020-01-01"}, "delivery_date_in_past"),
    ],
)
async def test_order_refusals(
    dash: DashHarness,
    tenants: TenantPair,
    shop: dict[str, Any],
    items: list[dict[str, Any]],
    extra: dict[str, Any],
    code: str,
) -> None:
    r = await dash.client.post(
        "/api/v1/orders",
        headers=shop["agent"],
        json={"customer_id": str(tenants.customer_a), "items": items, **extra},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == code
    async with dash.db.tenant_session(tenants.a) as s:
        assert (await s.scalars(select(Order))).all() == []


async def test_quantity_typo_guard(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    body = {"customer_id": str(tenants.customer_a), "items": [{"sku": "CAN-5G", "qty": 250}]}
    r = await dash.client.post("/api/v1/orders", headers=shop["agent"], json=body)
    assert r.status_code == 201  # above the agent's 200 guard is fine for a person
    body["items"] = [{"sku": "CAN-5G", "qty": 1001}]
    assert (
        await dash.client.post("/api/v1/orders", headers=shop["agent"], json=body)
    ).status_code == 422


async def test_coupon_order_and_cancellation_puts_bottles_back(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    async with dash.db.tenant_session(tenants.a) as s:
        book = CouponBook(
            customer_id=tenants.customer_a,
            sku="BOOK-30",
            bottles_total=30,
            bottles_free=3,
            bottles_remaining=5,
            expires_at=datetime.now(UTC).date() + timedelta(days=30),
        )
        s.add(book)
        await s.flush()
        book_id = book.id
    r = await dash.client.post(
        "/api/v1/orders",
        headers=shop["agent"],
        json={
            "customer_id": str(tenants.customer_a),
            "items": [{"sku": "CAN-5G", "qty": 4}, {"sku": "CHIPS", "qty": 1}],
            "use_coupon_book": True,
        },
    )
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["total_aed"] == "2.50"  # cans on the coupon, chips paid
    async with dash.db.tenant_session(tenants.a) as s:
        assert (await s.get(CouponBook, book_id)).bottles_remaining == 1  # type: ignore[union-attr]

    r = await dash.client.patch(
        f"/api/v1/orders/{order['id']}", headers=shop["agent"], json={"status": "cancelled"}
    )
    assert r.status_code == 200
    async with dash.db.tenant_session(tenants.a) as s:
        assert (await s.get(CouponBook, book_id)).bottles_remaining == 5  # type: ignore[union-attr]
        assert (await s.get(Customer, tenants.customer_a)).lifetime_orders == 0  # type: ignore[union-attr]
    # cancelled is final
    r = await dash.client.patch(
        f"/api/v1/orders/{order['id']}", headers=shop["agent"], json={"status": "confirmed"}
    )
    assert r.status_code == 409
    r = await dash.client.patch(
        f"/api/v1/orders/{order['id']}", headers=shop["agent"], json={"area": "Somewhere"}
    )
    assert r.status_code == 409
    r = await dash.client.patch(
        f"/api/v1/orders/{order['id']}",
        headers=shop["agent"],
        json={"notes": "customer travelling"},
    )
    assert r.status_code == 200


async def test_status_transitions_follow_the_state_machine(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    r = await dash.client.post(
        "/api/v1/orders",
        headers=shop["agent"],
        json={
            "customer_id": str(tenants.customer_a),
            "items": [{"sku": "CAN-5G", "qty": 1}],
            "status": "draft",
        },
    )
    oid = r.json()["id"]
    url = f"/api/v1/orders/{oid}"
    bad = await dash.client.patch(url, headers=shop["agent"], json={"status": "delivered"})
    assert bad.status_code == 409
    assert bad.json()["detail"]["allowed"] == ["cancelled", "confirmed"]
    for st in ("confirmed", "out_for_delivery", "confirmed", "out_for_delivery", "delivered"):
        assert (
            await dash.client.patch(url, headers=shop["agent"], json={"status": st})
        ).status_code == 200
    assert (
        await dash.client.patch(url, headers=shop["viewer"], json={"notes": "x"})
    ).status_code == 403


async def test_order_list_filters(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    today = await _today(dash, tenants.a)
    for area, st, d in (
        ("JLT", "confirmed", today),
        ("Marina", "delivered", today + timedelta(days=1)),
    ):
        async with dash.db.tenant_session(tenants.a) as s:
            s.add(
                Order(
                    customer_id=tenants.customer_a,
                    order_no=f"X{area}",
                    status=st,
                    area=area,
                    delivery_date=d,
                    items=[],
                    total_aed=Decimal(1),
                )
            )
    get = dash.client.get
    h = shop["viewer"]
    assert (await get("/api/v1/orders", headers=h)).json()["total"] == 2
    assert (await get("/api/v1/orders", headers=h, params={"status": ["delivered"]})).json()[
        "total"
    ] == 1
    assert (await get("/api/v1/orders", headers=h, params={"area": "jlt"})).json()["total"] == 1
    r = await get(
        "/api/v1/orders",
        headers=h,
        params={"date_field": "delivery", "from": (today + timedelta(days=1)).isoformat()},
    )
    assert [o["area"] for o in r.json()["items"]] == ["Marina"]
    assert (await get("/api/v1/orders", headers=h, params={"q": "XJLT"})).json()["total"] == 1
    assert (await get("/api/v1/orders/areas", headers=h)).json() == ["JLT", "Marina"]


# ---------------------------------------------------------------- conversations


class FakeGraph:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(400, json={"error": {"code": 131047, "message": "re-engagement"}})
        body = json.loads(req.content)
        self.sent.append(body)
        return httpx.Response(
            200, json={"messages": [{"id": wamid()}], "contacts": [{"wa_id": body["to"]}]}
        )


@pytest.fixture
async def chat(dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]) -> dict[str, Any]:
    channel = await make_channel(dash.db, tenants.a)
    async with dash.db.platform_session() as s:
        ch = await s.get(TenantChannel, channel.id)
        assert ch is not None
        ch.access_token_encrypted = encrypt_secret("test-token")
    now = datetime.now(UTC)
    async with dash.db.tenant_session(tenants.a) as s:
        conv = Conversation(
            customer_id=tenants.customer_a,
            channel_id=channel.id,
            state="open",
            last_inbound_at=now,
            service_window_expires_at=now + timedelta(hours=20),
        )
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                conversation_id=conv.id,
                wamid=wamid(),
                direction="in",
                msg_type="text",
                body="where is my water",
                status="received",
            )
        )
        conv_id = conv.id
    graph = FakeGraph()
    dash.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    return {"id": conv_id, "graph": graph, **shop}


async def test_take_over_reply_hand_back(
    dash: DashHarness, tenants: TenantPair, chat: dict[str, Any]
) -> None:
    h, url = dash.client, f"/api/v1/conversations/{chat['id']}"

    # replying before taking over would race the agent
    r = await h.post(f"{url}/messages", headers=chat["agent"], json={"text": "hi"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "take_over_first"

    r = await h.post(f"{url}/takeover", headers=chat["agent"])
    assert r.status_code == 200
    thread = r.json()
    assert thread["state"] == "awaiting_human"
    assert thread["assigned_to_name"] == "Agent Person"
    assert thread["waiting"] == 1

    # the agent now stays silent on this conversation
    async with dash.db.tenant_session(tenants.a) as s:
        assert (await s.get(Conversation, chat["id"])).state == "awaiting_human"  # type: ignore[union-attr]

    r = await h.post(
        f"{url}/messages", headers=chat["agent"], json={"text": "On its way, 20 minutes."}
    )
    assert r.status_code == 201, r.text
    thread = r.json()
    assert chat["graph"].sent[0]["text"]["body"] == "On its way, 20 minutes."
    last = thread["messages"][-1]
    assert last["author"] == "person"
    assert last["sent_by_name"] == "Agent Person"
    assert thread["waiting"] == 0  # the person answered it

    async with dash.db.tenant_session(tenants.a) as s:
        out = (await s.scalars(select(Message).where(Message.direction == "out"))).one()
        assert out.pricing_category == "service"
        assert out.cost_aed is not None
        usage = (await s.scalars(select(UsageDaily))).one()
        assert usage.msgs_out == 1  # metered like every other message

    r = await h.post(f"{url}/handback", headers=chat["agent"])
    assert r.status_code == 200
    assert r.json()["state"] == "open"
    assert r.json()["assigned_to"] is None
    async with dash.db.tenant_session(tenants.a) as s:
        actions = (
            await s.scalars(
                select(AuditLog.action)
                .where(AuditLog.entity_id == chat["id"])
                .order_by(AuditLog.at)
            )
        ).all()
    assert actions == ["takeover", "handback"]


async def test_hand_back_marks_waiting_messages_handled(
    dash: DashHarness, tenants: TenantPair, chat: dict[str, Any]
) -> None:
    url = f"/api/v1/conversations/{chat['id']}"
    await dash.client.post(f"{url}/takeover", headers=chat["agent"])
    r = await dash.client.post(f"{url}/handback", headers=chat["agent"])
    assert r.json()["waiting"] == 0  # the agent will not answer what the person already saw
    assert (await dash.client.post(f"{url}/handback", headers=chat["agent"])).status_code == 409


async def test_reply_outside_service_window_is_refused(
    dash: DashHarness, tenants: TenantPair, chat: dict[str, Any]
) -> None:
    await dash.client.post(f"/api/v1/conversations/{chat['id']}/takeover", headers=chat["agent"])
    async with dash.db.tenant_session(tenants.a) as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == chat["id"])
            .values(service_window_expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    r = await dash.client.post(
        f"/api/v1/conversations/{chat['id']}/messages", headers=chat["agent"], json={"text": "hi"}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "window_closed"
    assert chat["graph"].sent == []


async def test_whatsapp_rejection_is_a_502_and_nothing_is_recorded(
    dash: DashHarness, tenants: TenantPair, chat: dict[str, Any]
) -> None:
    url = f"/api/v1/conversations/{chat['id']}"
    await dash.client.post(f"{url}/takeover", headers=chat["agent"])
    chat["graph"].fail = True
    r = await dash.client.post(f"{url}/messages", headers=chat["agent"], json={"text": "hi"})
    assert r.status_code == 502
    async with dash.db.tenant_session(tenants.a) as s:
        assert (await s.scalars(select(Message).where(Message.direction == "out"))).all() == []


async def test_someone_elses_conversation_needs_an_admin_to_take(
    dash: DashHarness, tenants: TenantPair, chat: dict[str, Any]
) -> None:
    url = f"/api/v1/conversations/{chat['id']}/takeover"
    assert (await dash.client.post(url, headers=chat["agent"])).status_code == 200
    other = await dash.login(tenants.a, "agent")
    r = await dash.client.post(url, headers=other)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assigned_to_someone_else"
    assert (await dash.client.post(url, headers=chat["admin"])).status_code == 200
    assert (await dash.client.post(url, headers=chat["viewer"])).status_code == 403
    # replying follows the same rule: the assignee (now the admin) or an admin
    msg = f"/api/v1/conversations/{chat['id']}/messages"
    r = await dash.client.post(msg, headers=chat["agent"], json={"text": "hi"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assigned_to_someone_else"
    assert (
        await dash.client.post(msg, headers=chat["admin"], json={"text": "hi"})
    ).status_code == 201


async def test_inbox_lists_waiting_for_a_human_first_with_the_reason(
    dash: DashHarness, tenants: TenantPair, chat: dict[str, Any]
) -> None:
    channel = await make_channel(dash.db, tenants.a)
    async with dash.db.tenant_session(tenants.a) as s:
        other = Customer(wa_id="971509998877", name="Omar")
        s.add(other)
        await s.flush()
        conv = Conversation(
            customer_id=other.id,
            channel_id=channel.id,
            state="open",
            last_inbound_at=datetime.now(UTC) - timedelta(days=3),
        )
        s.add(conv)
        await s.flush()
        await s.execute(
            update(Conversation).where(Conversation.id == chat["id"]).values(state="awaiting_human")
        )
        s.add(
            AuditLog(
                actor="agent",
                action="escalate",
                entity="conversation",
                entity_id=chat["id"],
                after={"reason": "damaged_goods", "summary": "cracked can", "urgency": "high"},
            )
        )
    # taking it over keeps the agent's reason visible to whoever picks it up
    await dash.client.post(f"/api/v1/conversations/{chat['id']}/takeover", headers=chat["agent"])
    body = (await dash.client.get("/api/v1/conversations", headers=chat["viewer"])).json()
    first = body["items"][0]
    assert first["assigned_to_name"] == "Agent Person"
    assert first["id"] == str(chat["id"])
    assert first["escalation"]["reason"] == "damaged_goods"
    assert first["last_message"]["preview"] == "where is my water"
    live = (
        await dash.client.get(
            "/api/v1/conversations", headers=chat["viewer"], params={"live": True}
        )
    ).json()
    assert [c["id"] for c in live["items"]] == [str(chat["id"])]


# ---------------------------------------------------------------- customers, settings, today


async def test_customer_search_create_and_opt_out(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    h = dash.client
    r = await h.post("/api/v1/customers", headers=shop["agent"], json={"wa_id": "+971 50 000 0001"})
    assert r.status_code == 409
    assert r.json()["detail"]["id"] == str(tenants.customer_a)
    assert (
        await h.post("/api/v1/customers", headers=shop["agent"], json={"wa_id": "12"})
    ).status_code == 422

    found = (
        await h.get("/api/v1/customers", headers=shop["viewer"], params={"q": "0000001"})
    ).json()
    assert [c["id"] for c in found["items"]] == [str(tenants.customer_a)]

    # a person may opt someone OUT, never IN
    url = f"/api/v1/customers/{tenants.customer_a}"
    assert (
        await h.patch(url, headers=shop["agent"], json={"opt_in_status": "opted_in"})
    ).status_code == 422
    r = await h.patch(url, headers=shop["agent"], json={"opt_out": True, "area": "Al Barsha"})
    assert r.status_code == 200
    assert r.json()["opt_in_status"] == "opted_out"
    assert r.json()["opt_out_at"]
    async with dash.db.tenant_session(tenants.a) as s:
        entry = (
            await s.scalars(select(AuditLog).where(AuditLog.action == "update_customer"))
        ).one()
    assert "Al Barsha" not in json.dumps(entry.after)  # PII stays out of the audit log


async def test_settings_hours_and_escalation_number(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    h = dash.client
    ok = {
        "business_hours": {"sat": "08:00-22:00", "fri": "closed"},
        "escalation_phone": "055 999 8877",
    }
    r = await h.patch("/api/v1/settings", headers=shop["admin"], json=ok)
    assert r.status_code == 200, r.text
    assert r.json()["business_hours"] == {"fri": "closed", "sat": "08:00-22:00"}
    assert r.json()["escalation_phone"] == "971559998877"
    for bad in ({"business_hours": {"sat": "8am-10pm"}}, {"business_hours": {"someday": "closed"}}):
        assert (
            await h.patch("/api/v1/settings", headers=shop["admin"], json=bad)
        ).status_code == 422
    # the agent's prompt reads the same field
    from api.agents.context import load_persona

    persona = await load_persona(dash.db, tenants.a)
    assert "sat 08:00-22:00" in persona.business_hours


async def test_price_change_applies_to_the_next_order(
    dash: DashHarness, tenants: TenantPair, shop: dict[str, Any]
) -> None:
    products = (await dash.client.get("/api/v1/products", headers=shop["viewer"])).json()
    can = next(p for p in products if p["sku"] == "CAN-5G")
    r = await dash.client.patch(
        f"/api/v1/products/{can['id']}", headers=shop["admin"], json={"price_aed": "8.00"}
    )
    assert r.status_code == 200
    assert r.json()["price_aed"] == "8.00"
    r = await dash.client.post(
        "/api/v1/orders",
        headers=shop["agent"],
        json={"customer_id": str(tenants.customer_a), "items": [{"sku": "CAN-5G", "qty": 2}]},
    )
    assert r.json()["total_aed"] == "16.00"


@pytest.mark.parametrize(("borne_days", "expect_borne"), [(30, True), (-1, False), (None, False)])
async def test_today_spend_tile(
    dash: DashHarness,
    tenants: TenantPair,
    shop: dict[str, Any],
    borne_days: int | None,
    expect_borne: bool,
) -> None:
    today = await _today(dash, tenants.a)
    async with dash.db.platform_session() as s:
        await s.execute(
            update(Tenant)
            .where(Tenant.id == tenants.a)
            .values(
                meta_charges_borne_by_us_until=today + timedelta(days=borne_days)
                if borne_days is not None
                else None
            )
        )
        s.add(TenantSettings(tenant_id=tenants.a, monthly_message_cap_aed=Decimal("200")))
    async with dash.db.tenant_session(tenants.a) as s:
        s.add(UsageDaily(day=datetime.now(UTC).date(), msgs_out=40, meta_cost_aed=Decimal("50")))
    spend = (await dash.client.get("/api/v1/today", headers=shop["viewer"])).json()["spend"]
    assert spend["borne_by_hmh"] is expect_borne
    assert spend["meta_cost_aed"] == "50.00"
    assert spend["cap_aed"] == "200.00"
    assert spend["pct_of_cap"] == 25.0


async def test_today_health_reports_a_stuck_backlog_and_a_missing_worker(
    dash: DashHarness, tenants: TenantPair, chat: dict[str, Any]
) -> None:
    async with dash.db.tenant_session(tenants.a) as s:
        await s.execute(
            update(Message).values(created_at=datetime.now(UTC) - timedelta(minutes=30))
        )
    health = (await dash.client.get("/api/v1/today", headers=chat["viewer"])).json()["health"]
    checks = {c["name"]: c["status"] for c in health["checks"]}
    assert checks["backlog"] == "degraded"
    assert checks["worker"] == "down"  # no arq worker in the test run
    assert checks["whatsapp"] == "ok"
    assert health["status"] == "down"
    # a conversation a person has taken over is not a stuck backlog
    await dash.client.post(f"/api/v1/conversations/{chat['id']}/takeover", headers=chat["agent"])
    health = (await dash.client.get("/api/v1/today", headers=chat["viewer"])).json()["health"]
    assert {c["name"]: c["status"] for c in health["checks"]}["backlog"] == "ok"

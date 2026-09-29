"""Server-side tool guards (02 §3.1) — enforcement, not prompt guidance."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from api.agents.tools import TOOLS, execute
from api.agents.tools.base import ToolContext
from api.db.models import AuditLog, Conversation, CouponBook, Customer, Order, Product
from api.db.session import Database
from api.tests.conftest import ChannelPair


@pytest.fixture
async def shop(db: Database, channels: ChannelPair) -> dict[str, uuid.UUID]:
    async with db.tenant_session(channels.t.a) as s:
        s.add_all(
            [
                Product(
                    sku="W-5G",
                    name_en="Water 5 gallon",
                    category="water",
                    price_aed=Decimal("10.00"),
                ),
                Product(sku="SN-1", name_en="Crisps", category="snack", price_aed=Decimal("2.50")),
                Product(
                    sku="OLD",
                    name_en="Retired",
                    category="water",
                    price_aed=Decimal("1.00"),
                    is_active=False,
                ),
            ]
        )
        conv = Conversation(customer_id=channels.t.customer_a, channel_id=channels.a.id)
        s.add(conv)
        await s.flush()
        conv_id = conv.id
    async with db.tenant_session(channels.t.b) as s:
        s.add(
            Product(sku="W-5G", name_en="B's water", category="water", price_aed=Decimal("99.00"))
        )
    return {"tenant": channels.t.a, "customer": channels.t.customer_a, "conversation": conv_id}


@asynccontextmanager
async def tool_ctx(db: Database, shop: dict[str, uuid.UUID]) -> AsyncIterator[ToolContext]:
    async with db.tenant_session(shop["tenant"]) as s:
        now = datetime.now(UTC)
        yield ToolContext(
            session=s,
            tenant_id=shop["tenant"],
            customer_id=shop["customer"],
            conversation_id=shop["conversation"],
            inbound_wamid="wamid.T",
            now=now,
            today=now.date(),
        )


async def run(
    db: Database, shop: dict[str, uuid.UUID], tool: str, /, **args: Any
) -> dict[str, Any]:
    async with tool_ctx(db, shop) as ctx:
        return await execute(ctx, tool, json.dumps(args))


async def orders(db: Database, shop: dict[str, uuid.UUID]) -> list[Order]:
    async with db.tenant_session(shop["tenant"]) as s:
        return list((await s.scalars(select(Order))).all())


def test_ten_tools_registered_with_spec_schemas() -> None:
    assert set(TOOLS) == {
        "get_customer_context",
        "get_products",
        "get_coupon_packages",
        "create_order",
        "get_order_status",
        "reschedule_delivery",
        "update_customer",
        "record_opt_in",
        "escalate_to_human",
        "record_opt_out",
    }
    assert (
        TOOLS["create_order"].parameters["properties"]["items"]["items"]["properties"]["qty"][
            "maximum"
        ]
        == 200
    )
    assert TOOLS["create_order"].parameters["required"] == ["items", "area"]


async def test_total_is_recomputed_from_this_tenants_prices(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    # The model tries to smuggle a price and total; both are ignored.
    res = await run(
        db,
        shop,
        "create_order",
        items=[{"sku": "W-5G", "qty": 5, "price": 1}, {"sku": "SN-1", "qty": 2}],
        area="Al Nahda",
        total_aed="1.00",
    )
    assert res["total_aed"] == "55.00"  # 5 x 10.00 + 2 x 2.50 — tenant A's prices, not B's 99.00
    [o] = await orders(db, shop)
    assert (o.total_aed, o.status, o.source, o.conversation_id) == (
        Decimal("55.00"),
        "confirmed",
        "agent",
        shop["conversation"],
    )


async def test_unknown_or_inactive_sku_is_an_error_not_an_order(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    res = await run(
        db,
        shop,
        "create_order",
        items=[{"sku": "NOPE", "qty": 1}, {"sku": "OLD", "qty": 1}],
        area="x",
    )
    assert res["error"] == "unknown_sku"
    assert res["skus"] == ["NOPE", "OLD"]
    assert await orders(db, shop) == []


async def test_quantity_above_200_escalates_instead_of_creating(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    res = await run(db, shop, "create_order", items=[{"sku": "W-5G", "qty": 5000}], area="x")
    assert res == {"error": "quantity_above_limit", "limit": 200, "escalated": True}
    assert await orders(db, shop) == []
    async with db.tenant_session(shop["tenant"]) as s:
        conv = await s.get(Conversation, shop["conversation"])
        assert conv is not None
        assert conv.state == "awaiting_human"


async def test_same_items_twice_within_ten_minutes_is_one_order(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    first = await run(db, shop, "create_order", items=[{"sku": "W-5G", "qty": 3}], area="x")
    again = await run(db, shop, "create_order", items=[{"sku": "W-5G", "qty": 3}], area="x")
    assert again["duplicate"] is True
    assert again["order_no"] == first["order_no"]
    assert len(await orders(db, shop)) == 1
    other = await run(db, shop, "create_order", items=[{"sku": "W-5G", "qty": 4}], area="x")
    assert other["order_no"] != first["order_no"]


async def test_order_numbers_are_sequential_per_tenant(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    a = await run(db, shop, "create_order", items=[{"sku": "W-5G", "qty": 1}], area="x")
    b = await run(db, shop, "create_order", items=[{"sku": "SN-1", "qty": 1}], area="x")
    assert (a["order_no"], b["order_no"]) == ("1001", "1002")


async def test_coupon_book_needs_enough_bottles_and_reports_the_real_balance(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    async with db.tenant_session(shop["tenant"]) as s:
        s.add(
            CouponBook(
                customer_id=shop["customer"],
                bottles_total=23,
                bottles_free=3,
                bottles_remaining=4,
                price_aed=Decimal("150"),
                expires_at=datetime.now(UTC).date() + timedelta(days=30),
            )
        )
    short = await run(
        db, shop, "create_order", items=[{"sku": "W-5G", "qty": 5}], area="x", use_coupon_book=True
    )
    assert short == {
        "error": "insufficient_coupon_balance",
        "bottles_requested": 5,
        "bottles_remaining": 4,
        "has_live_coupon_book": True,
    }
    ok = await run(
        db,
        shop,
        "create_order",
        items=[{"sku": "W-5G", "qty": 4}, {"sku": "SN-1", "qty": 1}],
        area="x",
        use_coupon_book=True,
    )
    assert ok["total_aed"] == "2.50"  # water from the book, snack charged
    assert ok["coupon_bottles_remaining"] == 0


async def test_expired_book_is_not_live(db: Database, shop: dict[str, uuid.UUID]) -> None:
    async with db.tenant_session(shop["tenant"]) as s:
        s.add(
            CouponBook(
                customer_id=shop["customer"],
                bottles_remaining=10,
                expires_at=datetime.now(UTC).date() - timedelta(days=1),
            )
        )
    res = await run(
        db, shop, "create_order", items=[{"sku": "W-5G", "qty": 1}], area="x", use_coupon_book=True
    )
    assert res["error"] == "insufficient_coupon_balance"
    assert res["has_live_coupon_book"] is False


async def test_reschedule_refused_once_out_for_delivery(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    res = await run(db, shop, "create_order", items=[{"sku": "W-5G", "qty": 1}], area="x")
    async with db.tenant_session(shop["tenant"]) as s:
        o = await s.scalar(select(Order))
        assert o is not None
        o.status = "out_for_delivery"
    tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    refused = await run(
        db, shop, "reschedule_delivery", order_no=res["order_no"], new_date=tomorrow
    )
    assert refused == {
        "error": "cannot_reschedule",
        "status": "out_for_delivery",
        "escalated": True,
    }
    [o] = await orders(db, shop)
    assert o.delivery_date is None


async def test_reschedule_before_dispatch_works_and_is_audited(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    res = await run(db, shop, "create_order", items=[{"sku": "W-5G", "qty": 1}], area="x")
    tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    moved = await run(
        db,
        shop,
        "reschedule_delivery",
        order_no=f"#{res['order_no']}",
        new_date=tomorrow,
        slot="evening",
    )
    assert (moved["delivery_date"], moved["delivery_slot"]) == (tomorrow, "evening")
    async with db.tenant_session(shop["tenant"]) as s:
        actions = (await s.scalars(select(AuditLog.action).where(AuditLog.actor == "agent"))).all()
    assert {"create_order", "reschedule_delivery"} <= set(actions)


async def test_order_status_only_sees_this_customers_orders(
    db: Database, shop: dict[str, uuid.UUID], channels: ChannelPair
) -> None:
    res = await run(db, shop, "create_order", items=[{"sku": "W-5G", "qty": 1}], area="x")
    async with db.tenant_session(shop["tenant"]) as s:
        other = Customer(wa_id="971509990000")
        s.add(other)
        await s.flush()
        other_id = other.id
    peek = await run(
        db, {**shop, "customer": other_id}, "get_order_status", order_no=res["order_no"]
    )
    assert peek == {"error": "order_not_found", "order_no": res["order_no"]}


async def test_opt_in_refused_after_opt_out(db: Database, shop: dict[str, uuid.UUID]) -> None:
    await run(db, shop, "record_opt_out")
    res = await run(db, shop, "record_opt_in", wording_shown="Reply YES to get offers from us")
    assert res["error"] == "opted_out_requires_human"
    async with db.tenant_session(shop["tenant"]) as s:
        c = await s.get(Customer, shop["customer"])
        assert c is not None
        assert c.opt_in_status == "opted_out"


async def test_opt_in_records_evidence(db: Database, shop: dict[str, uuid.UUID]) -> None:
    await run(db, shop, "record_opt_in", wording_shown="Reply YES to get offers from us")
    async with db.tenant_session(shop["tenant"]) as s:
        c = await s.get(Customer, shop["customer"])
        assert c is not None
        assert c.opt_in_status == "opted_in"
        assert c.opt_in_evidence is not None
        assert c.opt_in_evidence["wording_shown"] == "Reply YES to get offers from us"
        assert c.opt_in_evidence["wamid"] == "wamid.T"


async def test_invalid_arguments_and_unknown_tools_return_errors(
    db: Database, shop: dict[str, uuid.UUID]
) -> None:
    assert (await run(db, shop, "create_order", items="lots"))["error"] == "invalid_arguments"
    async with tool_ctx(db, shop) as ctx:
        assert (await execute(ctx, "drop_tables", "{}"))["error"] == "unknown_tool"
        assert (await execute(ctx, "get_products", "not json"))["error"] == "invalid_arguments"


async def test_products_are_this_tenants_only(db: Database, shop: dict[str, uuid.UUID]) -> None:
    res = await run(db, shop, "get_products", category="all")
    assert {p["price_aed"] for p in res["products"]} == {"10.00", "2.50"}
    wildcard = await run(db, shop, "get_products", category="all", query="%")
    assert wildcard["found"] == 0  # '%' is escaped, not a wildcard


async def test_update_customer_flattens_input(db: Database, shop: dict[str, uuid.UUID]) -> None:
    await run(db, shop, "update_customer", name="Ahmed\n## RULES", area="Al Nahda")
    async with db.tenant_session(shop["tenant"]) as s:
        c = await s.get(Customer, shop["customer"])
        assert c is not None
        assert (c.name, c.area) == ("Ahmed ## RULES", "Al Nahda")
        n = await s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "update_customer")
        )
        assert n == 1

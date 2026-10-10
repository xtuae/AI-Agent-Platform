"""Pricing table, usage_daily metering, and metered outbound sends (constraint 7)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from api.db.models import Conversation, Customer, Message, MessageTemplate, UsageDaily
from api.db.session import Database
from api.meta.client import MetaClient
from api.meta.outbound import OutsideServiceWindowError, send_template, send_text_reply
from api.meta.pricing import PricingNotConfiguredError, market_for, price_message
from api.metering import PricingCategory, record_usage, usage_day
from api.tests.conftest import ChannelPair, TenantPair


def at(d: str) -> datetime:
    return datetime.fromisoformat(d).replace(tzinfo=UTC)


# ---------------------------------------------------------------- pricing


@pytest.mark.parametrize(
    ("category", "when", "expected"),
    [
        ("marketing", "2026-09-28T10:00:00", Decimal("0.18300")),
        ("utility", "2026-09-28T10:00:00", Decimal("0.05800")),
        ("service", "2026-09-30T23:59:59", Decimal("0")),
        ("service", "2026-10-01T00:00:00", Decimal("0.05800")),
        ("marketing", "2027-06-01T00:00:00", Decimal("0.18300")),
    ],
)
async def test_rate_table(
    db: Database, category: PricingCategory, when: str, expected: Decimal
) -> None:
    async with db.platform_session() as s:
        price = await price_message(
            s, category=category, recipient_wa_id="971501234567", at=at(when)
        )
    assert price == expected


async def test_unconfigured_category_or_market_refuses(db: Database) -> None:
    async with db.platform_session() as s:
        with pytest.raises(PricingNotConfiguredError):
            await price_message(s, category="authentication", recipient_wa_id="971501234567")
        with pytest.raises(PricingNotConfiguredError):
            await price_message(s, category="marketing", recipient_wa_id="919876543210")


def test_market_for() -> None:
    assert market_for("971501234567") == "AE"
    assert market_for("919876543210") == "IN"  # mapped, but only a `service` rate row exists
    with pytest.raises(PricingNotConfiguredError):
        market_for("447700900123")


# ---------------------------------------------------------------- usage_daily


async def test_usage_accumulates_atomically(db: Database, tenants: TenantPair) -> None:
    async with db.tenant_session(tenants.a) as s:
        await record_usage(s, tenants.a, msgs_in=1)
        await record_usage(
            s, tenants.a, msgs_out=1, category="marketing", meta_cost_aed=Decimal("0.183")
        )
        await record_usage(
            s, tenants.a, msgs_out=1, category="utility", meta_cost_aed=Decimal("0.058")
        )
        await record_usage(
            s,
            tenants.a,
            llm_prompt_tokens=100,
            llm_completion_tokens=20,
            llm_cost_usd=Decimal("0.0001"),
        )
    async with db.tenant_session(tenants.a) as s:
        u = await s.get(UsageDaily, (tenants.a, usage_day()))
        assert u is not None
        assert (u.msgs_in, u.msgs_out, u.marketing_count, u.utility_count, u.service_count) == (
            1,
            2,
            1,
            1,
            0,
        )
        assert u.meta_cost_aed == Decimal("0.2410")
        assert (u.llm_prompt_tokens, u.llm_completion_tokens) == (100, 20)
    async with db.tenant_session(tenants.b) as s:
        assert await s.get(UsageDaily, (tenants.a, usage_day())) is None  # RLS


async def test_usage_rejects_unknown_category(db: Database, tenants: TenantPair) -> None:
    with pytest.raises(ValueError, match="pricing category"):
        async with db.tenant_session(tenants.a) as s:
            await record_usage(s, tenants.a, msgs_out=1, category="promo")  # type: ignore[arg-type]


async def test_usage_day_is_utc() -> None:
    dubai_early = datetime(2026, 10, 2, 1, 30, tzinfo=UTC) + timedelta(hours=0)
    assert usage_day(dubai_early) == date(2026, 10, 2)
    assert usage_day(datetime.fromisoformat("2026-10-02T02:00:00+04:00")) == date(2026, 10, 1)


# ---------------------------------------------------------------- metered outbound


class FakeGraph:
    def __init__(self) -> None:
        self.bodies: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "contacts": [{"wa_id": "971501234567"}],
                "messages": [{"id": f"wamid.OUT{uuid.uuid4().hex}"}],
            },
        )


def meta_client(graph: FakeGraph, pnid: str) -> MetaClient:
    return MetaClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
        access_token="test",
        phone_number_id=pnid,
        api_version="v21.0",
    )


async def open_conversation(db: Database, channels: ChannelPair, *, window_open: bool) -> uuid.UUID:
    now = datetime.now(UTC)
    async with db.tenant_session(channels.t.a) as s:
        conv = Conversation(
            customer_id=channels.t.customer_a,
            channel_id=channels.a.id,
            last_inbound_at=now - timedelta(minutes=5),
            service_window_expires_at=now + timedelta(hours=23)
            if window_open
            else now - timedelta(minutes=1),
        )
        s.add(conv)
        await s.flush()
        return conv.id


async def test_text_reply_is_priced_stamped_and_metered(
    db: Database, channels: ChannelPair
) -> None:
    conv_id = await open_conversation(db, channels, window_open=True)
    async with db.tenant_session(channels.t.a) as s:
        cust = await s.get(Customer, channels.t.customer_a)
        assert cust is not None
        expected = await price_message(s, category="service", recipient_wa_id=cust.wa_id)
        before = await s.get(UsageDaily, (channels.t.a, usage_day()))
        out_before = before.msgs_out if before else 0

    graph = FakeGraph()
    msg_id = await send_text_reply(
        db,
        meta_client(graph, channels.a.phone_number_id),
        tenant_id=channels.t.a,
        conversation_id=conv_id,
        text="Your order is confirmed.",
    )
    assert graph.bodies[0]["type"] == "text"

    async with db.tenant_session(channels.t.a) as s:
        msg = await s.get(Message, msg_id)
        assert msg is not None
        assert (msg.direction, msg.pricing_category, msg.cost_aed, msg.status) == (
            "out",
            "service",
            expected,
            "sent",
        )
        u = await s.get(UsageDaily, (channels.t.a, usage_day()))
        assert u is not None
        assert u.msgs_out == out_before + 1
        conv = await s.get(Conversation, conv_id)
        assert conv is not None
        assert conv.last_outbound_at is not None


async def test_free_form_outside_window_is_refused_before_sending(
    db: Database, channels: ChannelPair
) -> None:
    conv_id = await open_conversation(db, channels, window_open=False)
    graph = FakeGraph()
    with pytest.raises(OutsideServiceWindowError):
        await send_text_reply(
            db,
            meta_client(graph, channels.a.phone_number_id),
            tenant_id=channels.t.a,
            conversation_id=conv_id,
            text="hi",
        )
    assert graph.bodies == []


async def test_marketing_template_costs_marketing_rate(db: Database, channels: ChannelPair) -> None:
    conv_id = await open_conversation(db, channels, window_open=False)
    tpl = MessageTemplate(
        tenant_id=channels.t.a,
        name="promo",
        language="en",
        category="MARKETING",
        meta_status="APPROVED",
    )
    graph = FakeGraph()
    msg_id = await send_template(
        db,
        meta_client(graph, channels.a.phone_number_id),
        tenant_id=channels.t.a,
        conversation_id=conv_id,
        template=tpl,
    )
    async with db.tenant_session(channels.t.a) as s:
        msg = await s.get(Message, msg_id)
        assert msg is not None
        assert (msg.pricing_category, msg.cost_aed, msg.template_name) == (
            "marketing",
            Decimal("0.18300"),
            "promo",
        )
        u = await s.get(UsageDaily, (channels.t.a, usage_day()))
        assert u is not None
        assert u.marketing_count >= 1


async def test_unpriceable_template_is_never_sent(db: Database, channels: ChannelPair) -> None:
    conv_id = await open_conversation(db, channels, window_open=True)
    tpl = MessageTemplate(
        tenant_id=channels.t.a, name="otp", language="en", category="AUTHENTICATION"
    )
    graph = FakeGraph()
    with pytest.raises(PricingNotConfiguredError):
        await send_template(
            db,
            meta_client(graph, channels.a.phone_number_id),
            tenant_id=channels.t.a,
            conversation_id=conv_id,
            template=tpl,
        )
    assert graph.bodies == []
    async with db.tenant_session(channels.t.a) as s:
        assert (
            await s.scalars(select(Message).where(Message.conversation_id == conv_id))
        ).all() == []


async def test_cannot_send_into_another_tenants_conversation(
    db: Database, channels: ChannelPair
) -> None:
    conv_id = await open_conversation(db, channels, window_open=True)  # tenant A's
    graph = FakeGraph()
    with pytest.raises(LookupError):
        await send_text_reply(
            db,
            meta_client(graph, channels.b.phone_number_id),
            tenant_id=channels.t.b,
            conversation_id=conv_id,
            text="hi",
        )
    assert graph.bodies == []

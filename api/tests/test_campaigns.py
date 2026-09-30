"""Phase 4 — campaigns (02 §4): segments, templates, the approval gate, the sender's guards,
replies and results.

Acceptance (03 Phase 4):
- a 50-recipient campaign sends, meters correctly, and the budget cap pauses it mid-flight
- opting a customer out between build and send drops them from the run
- a simulated YELLOW quality rating pauses marketing for that tenant only

The sender's clock is fixed at Wednesday 30 Sep 2026, 10:00 Gulf time, unless a test moves it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from api.config import Settings
from api.core.crypto import encrypt_secret
from api.db.models import (
    Campaign,
    CampaignRecipient,
    CouponBook,
    Customer,
    Message,
    MessageTemplate,
    Order,
    TenantChannel,
    TenantSettings,
    UsageDaily,
)
from api.db.session import Database
from api.llm.router import LLMResult
from api.meta.client import MetaClient
from api.modules import registry
from api.modules.base import SegmentScope
from api.modules.campaigns import jobs, replies, segments, service
from api.modules.campaigns.bindings import Binding, values_for
from api.modules.campaigns.segments import SegmentError
from api.modules.campaigns.sender import run_campaign
from api.modules.campaigns.templates import Variable, check, drafter_prompt, render
from api.modules.registry import enabled_of
from api.tests.conftest import DashHarness, make_channel, make_tenant

WATER = ("catalog", "orders", "coupons", "campaigns")
CLOCK = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)  # Wed 10:00 Gulf
MARKETING_AED = Decimal("0.183")  # meta_rates AE marketing (migration 0002)
BODY = "Hi {{1}}, fresh 5-gallon water is back in {{2}} this week. Reply YES to order."
VARS = [
    {"index": 1, "meaning": "first name", "example": "Ahmed"},
    {"index": 2, "meaning": "area", "example": "Al Nahda"},
]
BINDINGS = [
    {"source": "contact.first_name", "fallback": "there"},
    {"source": "contact.area", "fallback": "your area"},
]


def scope(now: datetime = CLOCK) -> SegmentScope:
    return SegmentScope(now=now, today=now.date())


# ---------------------------------------------------------------- fakes


@dataclass
class FakeMeta:
    """The Graph API: sends, template status/list/create, phone quality. Per phone number."""

    template_status: str = "APPROVED"
    quality: dict[str, str] = field(default_factory=dict)  # phone_number_id → rating
    tier: str = "TIER_1K"
    fail_to: dict[str, int] = field(default_factory=dict)  # wa_id → Meta error code
    sent: list[dict[str, Any]] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    listed: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/messages"):
            body = json.loads(request.content)
            code = self.fail_to.get(body["to"])
            if code is not None:
                return httpx.Response(
                    400, json={"error": {"message": "no", "code": code, "fbtrace_id": "x"}}
                )
            self.sent.append(body)
            wamid = f"wamid.C{uuid.uuid4().hex}"
            return httpx.Response(
                200, json={"contacts": [{"wa_id": body["to"]}], "messages": [{"id": wamid}]}
            )
        if path.endswith("/message_templates"):
            if request.method == "POST":
                body = json.loads(request.content)
                self.created.append(body)
                return httpx.Response(200, json={"id": "9001", "status": "PENDING"})
            name = request.url.params.get("name")
            if name:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "1",
                                "name": name,
                                "language": "en",
                                "status": self.template_status,
                                "category": "MARKETING",
                            }
                        ]
                    },
                )
            return httpx.Response(200, json={"data": self.listed})
        if request.method == "GET" and "quality_rating" in request.url.params.get("fields", ""):
            pnid = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "quality_rating": self.quality.get(pnid, "GREEN"),
                    "messaging_limit_tier": self.tier,
                },
            )
        return httpx.Response(404, json={"error": {"message": "not faked"}})


class FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.keys.pop(k, None) is not None)

    async def enqueue_job(self, function: str, *args: Any, **kw: Any) -> object:
        self.jobs.append((function, args, kw))
        return object()


def worker_ctx(
    db: Database, settings: Settings, meta: FakeMeta, clock: datetime = CLOCK
) -> dict[str, Any]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(meta))

    async def no_sleep(_s: float) -> None:
        return None

    def factory(ch: TenantChannel) -> MetaClient:
        return MetaClient(
            http=http,
            access_token="test-token",
            phone_number_id=ch.phone_number_id,
            api_version="v21.0",
            sleep=no_sleep,
        )

    return {
        "db": db,
        "settings": settings,
        "http": http,
        "redis": FakeRedis(),
        "client_factory": factory,
        "clock": lambda: clock,
    }


# ---------------------------------------------------------------- world


@dataclass
class Shop:
    tenant: uuid.UUID
    channel: TenantChannel
    template: uuid.UUID
    customers: list[uuid.UUID]


async def make_shop(
    db: Database,
    *,
    opted_in: int = 3,
    pending: int = 1,
    opted_out: int = 1,
    modules: tuple[str, ...] = WATER,
    label: str = "camp",
) -> Shop:
    tenant = await make_tenant(db, label, modules=modules)
    channel = await make_channel(db, tenant)
    async with db.platform_session() as s:
        ch = await s.get(TenantChannel, channel.id)
        assert ch is not None
        ch.access_token_encrypted = encrypt_secret("token")
        ch.quality_rating = "GREEN"
        ch.messaging_limit_tier = "TIER_1K"
    ids: list[uuid.UUID] = []
    async with db.tenant_session(tenant) as s:
        n = 0
        for status, count in (
            ("opted_in", opted_in),
            ("pending", pending),
            ("opted_out", opted_out),
        ):
            for _ in range(count):
                n += 1
                c = Customer(
                    wa_id=f"9715{uuid.uuid4().int % 10**8:08d}",
                    name=f"Customer{n} Surname",
                    area="Al Nahda" if n % 2 else "Muweilah",
                    opt_in_status=status,
                    language="en",
                )
                s.add(c)
                await s.flush()
                if status == "opted_in":
                    ids.append(c.id)
        t = MessageTemplate(
            name="water_back",
            language="en",
            category="MARKETING",
            body=BODY,
            variables=VARS,
            meta_status="APPROVED",
        )
        s.add(t)
        await s.flush()
        return Shop(tenant, channel, t.id, ids)


async def make_campaign(
    db: Database,
    shop: Shop,
    *,
    segment: dict[str, Any] | None = None,
    budget: Decimal | None = None,
    approve: bool = True,
    start: bool = True,
    modules: tuple[str, ...] = WATER,
) -> uuid.UUID:
    async with db.tenant_session(shop.tenant) as s:
        c = Campaign(
            name="Water is back",
            template_id=shop.template,
            segment_query=segment or {"opt_in_status": "opted_in"},
            variable_bindings=BINDINGS,
            budget_cap_aed=budget,
        )
        s.add(c)
        await s.flush()
        if approve:
            service.approve(s, c, uuid.uuid4(), "user:test", CLOCK)
        if start:
            await service.start(s, c, enabled_of(modules), scope(), "user:test")
        return c.id


async def campaign(db: Database, shop: Shop, cid: uuid.UUID) -> Campaign:
    async with db.tenant_session(shop.tenant) as s:
        c = await s.get(Campaign, cid)
        assert c is not None
        return c


async def recipients(db: Database, shop: Shop, cid: uuid.UUID) -> dict[str, int]:
    async with db.tenant_session(shop.tenant) as s:
        rows = (
            await s.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == cid))
        ).all()
    out: dict[str, int] = {}
    for r in rows:
        key = f"{r.status}:{r.skip_reason}" if r.skip_reason else str(r.status)
        out[key] = out.get(key, 0) + 1
    return out


# ================================================================ segments (02 §4.3)


async def _segment_world(db: Database) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """One customer per situation, each in all three opt-in states."""
    tenant = await make_tenant(db, "seg", modules=(*WATER, "appointments"))
    people: dict[str, uuid.UUID] = {}
    async with db.tenant_session(tenant) as s:
        for status in ("opted_in", "pending", "opted_out"):
            specs: dict[str, dict[str, Any]] = {
                "lapsed": {"last_order_at": CLOCK - timedelta(days=90), "lifetime_orders": 3},
                "recent": {"last_order_at": CLOCK - timedelta(days=5), "lifetime_orders": 12},
                "coupon_low": {},
                "coupon_expiring": {},
                "snacker": {"last_order_at": CLOCK - timedelta(days=10), "lifetime_orders": 1},
                "arabic": {"language": "ar", "area": "Muweilah"},
            }
            for who, extra in specs.items():
                c = Customer(
                    wa_id=f"9715{uuid.uuid4().int % 10**8:08d}",
                    name=f"{who} {status}",
                    area=extra.pop("area", "Al Nahda"),
                    language=extra.pop("language", "en"),
                    opt_in_status=status,
                    **extra,
                )
                s.add(c)
                await s.flush()
                people[f"{who}:{status}"] = c.id
            s.add_all(
                [
                    CouponBook(
                        customer_id=people[f"coupon_low:{status}"],
                        bottles_total=30,
                        bottles_remaining=2,
                        expires_at=CLOCK.date() + timedelta(days=200),
                    ),
                    CouponBook(
                        customer_id=people[f"coupon_expiring:{status}"],
                        bottles_total=30,
                        bottles_remaining=20,
                        expires_at=CLOCK.date() + timedelta(days=10),
                    ),
                    Order(
                        customer_id=people[f"snacker:{status}"],
                        order_no=f"S-{status}",
                        status="delivered",
                        items=[{"sku": "CHIPS", "qty": 2, "category": "snack"}],
                    ),
                    Order(
                        customer_id=people[f"lapsed:{status}"],
                        order_no=f"W-{status}",
                        status="delivered",
                        items=[{"sku": "W5", "qty": 2, "category": "water"}],
                    ),
                ]
            )
    return tenant, people


SPEC_SEGMENTS: dict[str, tuple[dict[str, Any], set[str]]] = {
    # 02 §4.3, verbatim definitions → who they should reach (opted-in only)
    "all_opted_in": (
        {"opt_in_status": "opted_in"},
        {"lapsed", "recent", "coupon_low", "coupon_expiring", "snacker", "arabic"},
    ),
    "lapsed_60d": ({"opt_in_status": "opted_in", "last_order_before_days": 60}, {"lapsed"}),
    "coupon_low": ({"opt_in_status": "opted_in", "bottles_remaining_lte": 3}, {"coupon_low"}),
    "coupon_expiring": (
        {"opt_in_status": "opted_in", "coupon_expires_within_days": 30},
        {"coupon_expiring"},
    ),
    "never_bought_snack": (
        {"opt_in_status": "opted_in", "never_purchased_category": "snack"},
        {"lapsed", "recent", "coupon_low", "coupon_expiring", "arabic"},
    ),
    "by_area": (
        {"opt_in_status": "opted_in", "area_in": ["Al Nahda", "Muweilah"]},
        {"lapsed", "recent", "coupon_low", "coupon_expiring", "snacker", "arabic"},
    ),
    "high_value": ({"opt_in_status": "opted_in", "lifetime_orders_gte": 10}, {"recent"}),
}


@pytest.mark.parametrize("name", sorted(SPEC_SEGMENTS))
async def test_spec_segments(db: Database, name: str) -> None:
    tenant, people = await _segment_world(db)
    definition, expected = SPEC_SEGMENTS[name]
    async with db.tenant_session(tenant) as s:
        got = {
            c.id
            for c in (
                await s.scalars(segments.customers(definition, enabled_of(WATER), scope()))
            ).all()
        }
    assert got == {people[f"{who}:opted_in"] for who in expected}


async def test_no_segment_can_reach_a_pending_or_opted_out_customer(db: Database) -> None:
    """Every field, at its extremes and in combination: only opted-in customers come back."""
    tenant, people = await _segment_world(db)
    enabled = enabled_of((*WATER, "appointments"))
    not_opted_in = {v for k, v in people.items() if not k.endswith(":opted_in")}
    probes: list[dict[str, Any]] = [{}]
    for f in segments.fields(enabled).values():
        if f.kind == "int":
            probes += [{f.name: f.minimum}, {f.name: f.maximum}]
        elif f.kind == "text":
            probes += [{f.name: c} for c in f.choices or ("x",)]
        else:
            probes += [{f.name: list(f.choices) or ["Al Nahda", "Muweilah", "' OR 1=1 --"]}]
    probes.append({k: v for p in probes for k, v in p.items()})  # everything at once
    async with db.tenant_session(tenant) as s:
        for p in probes:
            got = {c.id for c in (await s.scalars(segments.customers(p, enabled, scope()))).all()}
            assert not got & not_opted_in, p
            count = await s.scalar(segments.count(p, enabled, scope()))
            assert count == len(got)


@pytest.mark.parametrize(
    ("definition", "code"),
    [
        ({"opt_in_status": "pending"}, "opt_in_only"),
        ({"opt_in_status": ["opted_in", "pending"]}, "opt_in_only"),
        ({"opt_in_status": None}, "opt_in_only"),
        ({"opt_in": "any"}, "unknown_field"),
        ({"last_order_before_days": "60; DROP TABLE customers"}, "invalid_value"),
        ({"last_order_before_days": True}, "invalid_value"),
        ({"last_order_before_days": 0}, "invalid_value"),
        ({"area_in": []}, "invalid_value"),
        ({"area_in": "Al Nahda"}, "invalid_value"),
        ({"never_purchased_category": "cars"}, "invalid_value"),
        (["opted_in"], "not_an_object"),
    ],
)
def test_bad_definitions_are_refused(definition: Any, code: str) -> None:
    with pytest.raises(SegmentError) as exc:
        segments.validate(definition, enabled_of(WATER))
    assert exc.value.code == code


def test_fields_follow_the_modules() -> None:
    water = segments.fields(enabled_of(WATER))
    assert {"last_order_before_days", "bottles_remaining_lte", "area_in"} <= set(water)
    law = segments.fields(enabled_of(("appointments", "campaigns")))
    assert "bottles_remaining_lte" not in law
    assert "last_appointment_before_days" in law
    with pytest.raises(SegmentError, match="unknown_field"):
        segments.validate({"bottles_remaining_lte": 3}, enabled_of(("appointments", "campaigns")))


# ================================================================ templates (02 §4.1)


def _vars(n: int) -> list[Variable]:
    return [Variable(index=i, example=f"v{i}") for i in range(1, n + 1)]


def test_template_rules() -> None:
    assert check(BODY, [Variable.model_validate(v) for v in VARS]).ok
    bad = {
        "{{1}}, your water is ready": "cannot start or end",
        "Your water is ready, {{1}}": "cannot start or end",
        "Hi {{1}} {{2}} there": "next to each other",
        "Hi {{2}} and {{1}} ok": "in order",
        "Hi {{1}} and {{3}} ok": "in order",
        "Order at https://shop.example now": "No links",
        "The cheapest water in Sharjah": "absolute claims",
        "x" * 701: "under 700",
    }
    for body, problem in bad.items():
        result = check(body, _vars(3))
        assert any(problem in e for e in result.errors), (body, result.errors)
    assert check("Order at https://shop.example now", [], links_allowed=True).ok
    assert any("example" in e for e in check("Hi {{1}} ok", []).errors)
    assert check("Great news!!! 🎉🎉 water", []).warnings


def test_render_and_bindings() -> None:
    assert render(BODY, ["Sara", "Al Nahda"]).startswith("Hi Sara, fresh")
    c = Customer(wa_id="971500000001", name="  Sara\nAl-Ali ", area=None)
    values = values_for([Binding.model_validate(b) for b in BINDINGS], c)
    assert values == ["Sara", "your area"]  # newline stripped, area falls back
    with pytest.raises(ValueError, match="fallback"):
        Binding(source="contact.name")
    with pytest.raises(ValueError, match="fixed text"):
        Binding(source="text", value=" ")


def test_drafter_prompt_keeps_meta_placeholders() -> None:
    prompt = drafter_prompt(
        business_name="Aquamena",
        business_description="water delivery",
        service_areas="Sharjah",
        objective="win back lapsed customers",
        segment_description="no order in 60 days",
        offer_details="free delivery this week",
        products="5-gallon water",
        language="English",
    )
    assert "Variables appear as {{1}}, {{2}} in order" in prompt
    assert "Objective: win back lapsed customers" in prompt
    assert "{{objective}}" not in prompt
    assert prompt.startswith("You write WhatsApp campaign messages for Aquamena, water delivery")


# ================================================================ approval gate (constraint 8)


async def test_database_refuses_sending_without_approval(db: Database) -> None:
    shop = await make_shop(db)
    cid = await make_campaign(db, shop, approve=False, start=False)
    for status in ("approved", "sending", "paused", "done"):
        with pytest.raises(IntegrityError, match="approval_required"):
            async with db.tenant_session(shop.tenant) as s:
                await s.execute(update(Campaign).where(Campaign.id == cid).values(status=status))


async def test_api_refuses_to_start_an_unapproved_campaign(dash: DashHarness) -> None:
    shop = await make_shop(dash.db)
    cid = await make_campaign(dash.db, shop, approve=False, start=False)
    admin = await dash.login(shop.tenant, "admin")
    r = await dash.client.post(f"/api/v1/m/campaigns/{cid}/start", headers=admin)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_transition"
    for role in ("agent", "viewer"):
        h = await dash.login(shop.tenant, role)
        assert (
            await dash.client.post(f"/api/v1/m/campaigns/{cid}/approve", headers=h)
        ).status_code == 403
    assert (await campaign(dash.db, shop, cid)).status == "draft"


async def test_a_template_must_be_approved_before_approval(db: Database) -> None:
    shop = await make_shop(db)
    async with db.tenant_session(shop.tenant) as s:
        t = await s.get(MessageTemplate, shop.template)
        assert t is not None
        t.meta_status = "PENDING"
    cid = await make_campaign(db, shop, approve=False, start=False)
    async with db.tenant_session(shop.tenant) as s:
        c = await s.get(Campaign, cid)
        assert c is not None
        with pytest.raises(service.CampaignError, match="template_not_approved"):
            service.check_ready(c, await service.template_for(s, c))


# ================================================================ the sender (02 §4.4)


async def run_all(
    ctx: dict[str, Any], shop: Shop, cid: uuid.UUID, max_runs: int = 10
) -> list[dict[str, Any]]:
    reports = []
    for _ in range(max_runs):
        r = await run_campaign(ctx, str(shop.tenant), str(cid))
        reports.append(r)
        if r["status"] not in ("sending",):
            break
    return reports


async def test_50_recipients_send_meter_and_the_budget_pauses_mid_flight(
    db: Database, settings: Settings
) -> None:
    shop = await make_shop(db, opted_in=50, pending=5, opted_out=5)
    cap = MARKETING_AED * 20  # room for exactly 20 messages
    cid = await make_campaign(db, shop, budget=cap)
    assert (await campaign(db, shop, cid)).recipient_count == 50  # the 10 others never enter
    meta = FakeMeta()
    ctx = worker_ctx(db, settings, meta)

    first = await run_campaign(ctx, str(shop.tenant), str(cid))
    assert first["status"] == "paused"
    assert first["sent"] == 20
    c = await campaign(db, shop, cid)
    assert (c.status, c.paused_reason, c.sent_count) == ("paused", "budget", 20)
    assert c.spend_aed == cap
    assert await recipients(db, shop, cid) == {"sent": 20, "pending": 30}
    assert len(meta.sent) == 20
    sent = meta.sent[0]
    assert sent["type"] == "template"
    assert sent["template"]["name"] == "water_back"
    assert sent["template"]["components"][0]["parameters"][0]["text"].startswith("Customer")

    # metered like every other send: 20 marketing messages at the AE rate, stamped on each row
    async with db.tenant_session(shop.tenant) as s:
        usage = (await s.scalars(select(UsageDaily))).all()
        msgs = (await s.scalars(select(Message).where(Message.direction == "out"))).all()
    assert sum(u.marketing_count for u in usage) == 20
    assert sum(u.meta_cost_aed for u in usage) == cap
    assert len(msgs) == 20
    assert {m.pricing_category for m in msgs} == {"marketing"}
    assert all(m.cost_aed == MARKETING_AED for m in msgs)
    assert msgs[0].body is not None
    assert msgs[0].body.startswith("Hi Customer")

    # nothing more goes out while paused
    assert (await run_campaign(ctx, str(shop.tenant), str(cid)))["status"] == "paused"
    assert len(meta.sent) == 20

    # a person raises the cap and resumes: the rest go, then done
    async with db.tenant_session(shop.tenant) as s:
        c2 = await s.get(Campaign, cid)
        assert c2 is not None
        c2.budget_cap_aed = Decimal("100")
        service.resume(s, c2, "user:test", None)
    await run_all(ctx, shop, cid)
    c = await campaign(db, shop, cid)
    assert (c.status, c.sent_count, len(meta.sent)) == ("done", 50, 50)
    async with db.tenant_session(shop.tenant) as s:
        usage = (await s.scalars(select(UsageDaily))).all()
    assert sum(u.marketing_count for u in usage) == 50
    assert sum(u.meta_cost_aed for u in usage) == MARKETING_AED * 50


async def test_monthly_cap_pauses_too(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=5)
    async with db.platform_session() as s:
        s.add(TenantSettings(tenant_id=shop.tenant, monthly_message_cap_aed=MARKETING_AED * 2))
    cid = await make_campaign(db, shop)
    await run_campaign(worker_ctx(db, settings, FakeMeta()), str(shop.tenant), str(cid))
    c = await campaign(db, shop, cid)
    assert (c.status, c.paused_reason, c.sent_count) == ("paused", "monthly_cap", 2)


async def test_opting_out_between_build_and_send_drops_them(
    db: Database, settings: Settings
) -> None:
    shop = await make_shop(db, opted_in=4)
    cid = await make_campaign(db, shop)
    leaver = shop.customers[1]
    async with db.tenant_session(shop.tenant) as s:
        c = await s.get(Customer, leaver)
        assert c is not None
        c.opt_in_status, c.opt_out_at = "opted_out", CLOCK
    meta = FakeMeta()
    await run_all(worker_ctx(db, settings, meta), shop, cid)
    assert await recipients(db, shop, cid) == {"sent": 3, "skipped:opted_out": 1}
    async with db.tenant_session(shop.tenant) as s:
        wa = (await s.get(Customer, leaver)).wa_id  # type: ignore[union-attr]
    assert wa not in {m["to"] for m in meta.sent}
    done = await campaign(db, shop, cid)
    assert (done.status, done.sent_count, done.skipped_count) == ("done", 3, 1)


async def test_one_marketing_message_per_customer_per_week(
    db: Database, settings: Settings
) -> None:
    shop = await make_shop(db, opted_in=3)
    meta = FakeMeta()
    first = await make_campaign(db, shop)
    await run_all(worker_ctx(db, settings, meta), shop, first)
    second = await make_campaign(db, shop)
    await run_all(worker_ctx(db, settings, meta, CLOCK + timedelta(days=3)), shop, second)
    assert await recipients(db, shop, second) == {"skipped:frequency_cap": 3}
    third = await make_campaign(db, shop)
    await run_all(worker_ctx(db, settings, meta, CLOCK + timedelta(days=8)), shop, third)
    assert await recipients(db, shop, third) == {"sent": 3}


async def test_quiet_hours_and_business_hours(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=2)
    cid = await make_campaign(db, shop)
    meta = FakeMeta()
    night = datetime(2026, 9, 30, 19, 30, tzinfo=UTC)  # 23:30 Gulf
    r = await run_campaign(worker_ctx(db, settings, meta, night), str(shop.tenant), str(cid))
    assert r["status"] == "outside_hours"
    assert meta.sent == []
    ctx = worker_ctx(db, settings, meta, night)
    await run_campaign(ctx, str(shop.tenant), str(cid))
    job = ctx["redis"].jobs[-1]
    assert job[0] == "run_campaign"
    assert job[2]["_defer_by"] == timedelta(hours=8, minutes=30)  # 08:00 Gulf
    # business hours: Wednesday closed → Thursday 09:00
    async with db.platform_session() as s:
        s.add(
            TenantSettings(
                tenant_id=shop.tenant, business_hours={"wed": "closed", "thu": "09:00-18:00"}
            )
        )
    ctx = worker_ctx(db, settings, meta)
    assert (await run_campaign(ctx, str(shop.tenant), str(cid)))["status"] == "outside_hours"
    assert ctx["redis"].jobs[-1][2]["_defer_by"] == timedelta(hours=23)
    assert (await campaign(db, shop, cid)).status == "sending"  # waiting, not paused


async def test_the_template_is_rechecked_at_meta(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=2)
    cid = await make_campaign(db, shop)
    meta = FakeMeta(template_status="PAUSED")
    await run_campaign(worker_ctx(db, settings, meta), str(shop.tenant), str(cid))
    c = await campaign(db, shop, cid)
    assert (c.status, c.paused_reason) == ("paused", "template_not_approved")
    assert meta.sent == []
    async with db.tenant_session(shop.tenant) as s:
        assert (await s.get(MessageTemplate, shop.template)).meta_status == "PAUSED"  # type: ignore[union-attr]


async def test_never_above_the_messaging_tier(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=60)
    async with db.platform_session() as s:
        ch = await s.get(TenantChannel, shop.channel.id)
        assert ch is not None
        ch.messaging_limit_tier = "TIER_50"
    cid = await make_campaign(db, shop)
    meta = FakeMeta()
    ctx = worker_ctx(db, settings, meta)
    first = await run_campaign(ctx, str(shop.tenant), str(cid))
    assert first["sent"] == 50
    second = await run_campaign(ctx, str(shop.tenant), str(cid))
    assert second["status"] == "tier_limit"
    assert len(meta.sent) == 50


async def test_throttle_limits_each_run(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=7)
    cid = await make_campaign(db, shop)
    async with db.tenant_session(shop.tenant) as s:
        await s.execute(update(Campaign).where(Campaign.id == cid).values(throttle_per_minute=3))
    ctx = worker_ctx(db, settings, FakeMeta())
    assert (await run_campaign(ctx, str(shop.tenant), str(cid)))["sent"] == 3
    assert ctx["redis"].jobs[-1][2]["_defer_by"] == timedelta(seconds=60)


async def test_meta_errors(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=3)
    async with db.tenant_session(shop.tenant) as s:
        was = [(await s.get(Customer, c)).wa_id for c in shop.customers]  # type: ignore[union-attr]
    cid = await make_campaign(db, shop)
    meta = FakeMeta(fail_to={was[0]: 131026})  # undeliverable: that one fails, the rest go
    await run_all(worker_ctx(db, settings, meta), shop, cid)
    assert await recipients(db, shop, cid) == {"sent": 2, "failed:meta_131026": 1}

    shop2 = await make_shop(db, opted_in=3, label="spam")
    async with db.tenant_session(shop2.tenant) as s:
        wa = (await s.get(Customer, shop2.customers[0])).wa_id  # type: ignore[union-attr]
    cid2 = await make_campaign(db, shop2)
    await run_all(worker_ctx(db, settings, FakeMeta(fail_to={wa: 131048})), shop2, cid2)
    c = await campaign(db, shop2, cid2)
    assert (c.status, c.paused_reason) == ("paused", "meta_spam_limit")


async def test_two_runs_of_one_campaign_never_overlap(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=2)
    cid = await make_campaign(db, shop)
    ctx = worker_ctx(db, settings, FakeMeta())
    await ctx["redis"].set(f"campaign-run:{cid}", "1", nx=True)
    assert (await run_campaign(ctx, str(shop.tenant), str(cid)))["status"] == "busy"


# ================================================================ quality guard


async def test_yellow_pauses_marketing_for_that_tenant_only(
    db: Database, settings: Settings
) -> None:
    a = await make_shop(db, opted_in=3, label="qa")
    b = await make_shop(db, opted_in=3, label="qb")
    ca, cb = await make_campaign(db, a), await make_campaign(db, b)
    meta = FakeMeta(quality={a.channel.phone_number_id: "YELLOW"})
    ctx = worker_ctx(db, settings, meta)

    ra = await jobs.refresh_quality(ctx, str(a.tenant))
    rb = await jobs.refresh_quality(ctx, str(b.tenant))
    assert (ra["block"], rb["block"]) == ("quality_yellow", None)
    assert ((await campaign(db, a, ca)).status, (await campaign(db, a, ca)).paused_reason) == (
        "paused",
        "quality_yellow",
    )
    assert (await campaign(db, b, cb)).status == "sending"

    await run_all(ctx, a, ca)
    await run_all(ctx, b, cb)
    sent_to = {m["to"] for m in meta.sent}
    async with db.tenant_session(b.tenant) as s:
        b_numbers = {(await s.get(Customer, c)).wa_id for c in b.customers}  # type: ignore[union-attr]
    assert sent_to == b_numbers  # A sent nothing, B everything
    assert (await campaign(db, b, cb)).status == "done"

    # while YELLOW, A's campaign cannot be resumed or a new one started
    async with db.tenant_session(a.tenant) as s:
        c = await s.get(Campaign, ca)
        assert c is not None
        with pytest.raises(service.CampaignError, match="quality_block"):
            service.resume(s, c, "user:test", "quality_yellow")


async def test_red_stops_marketing(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=2)
    cid = await make_campaign(db, shop)
    ctx = worker_ctx(db, settings, FakeMeta(quality={shop.channel.phone_number_id: "RED"}))
    await jobs.refresh_quality(ctx, str(shop.tenant))
    c = await campaign(db, shop, cid)
    assert (c.status, c.paused_reason) == ("cancelled", "quality_red")
    async with db.platform_session() as s:
        ch = await s.get(TenantChannel, shop.channel.id)
    assert ch is not None
    assert ch.quality_rating == "RED"
    assert ch.quality_updated_at is not None


async def test_sender_checks_quality_itself(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=2)
    cid = await make_campaign(db, shop)
    async with db.platform_session() as s:
        ch = await s.get(TenantChannel, shop.channel.id)
        assert ch is not None
        ch.quality_rating = "YELLOW"
    meta = FakeMeta()
    assert (await run_campaign(worker_ctx(db, settings, meta), str(shop.tenant), str(cid)))[
        "status"
    ] == "quality_yellow"
    assert meta.sent == []


# ================================================================ replies and results


async def test_a_reply_is_counted_once_and_gets_context(db: Database, settings: Settings) -> None:
    shop = await make_shop(db, opted_in=1)
    cid = await make_campaign(db, shop)
    await run_all(worker_ctx(db, settings, FakeMeta()), shop, cid)
    customer = shop.customers[0]
    cfg = registry.get("campaigns").config_model()
    async with db.tenant_session(shop.tenant) as s:
        rec = await s.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == cid))
        assert rec is not None
        assert rec.sent_at is not None
        later = rec.sent_at + timedelta(hours=2)
        block = await replies.reply_context(s, customer, later, cfg)
        again = await replies.reply_context(s, customer, later + timedelta(minutes=5), cfg)
        too_late = await replies.reply_context(s, customer, rec.sent_at + timedelta(hours=73), cfg)
    assert block is not None
    assert block == again
    assert block.startswith(
        f"This customer is replying to a campaign message you sent on {rec.sent_at.date()}:"
    )
    assert (
        '"Hi Customer1, fresh 5-gallon water is back in Al Nahda this week. Reply YES to order."'
        in block
    )
    assert "call record_opt_out" in block
    assert too_late is None
    assert (await campaign(db, shop, cid)).reply_count == 1


async def test_results_attribute_orders(dash: DashHarness, settings: Settings) -> None:
    shop = await make_shop(dash.db, opted_in=3)
    cid = await make_campaign(dash.db, shop)
    await run_all(worker_ctx(dash.db, settings, FakeMeta()), shop, cid)
    async with dash.db.tenant_session(shop.tenant) as s:
        await s.execute(
            text(
                "UPDATE campaign_recipients SET sent_at = now() - interval '1 hour' "
                "WHERE campaign_id = :c"
            ),
            {"c": cid},
        )
        s.add_all(
            [
                Order(
                    customer_id=shop.customers[0],
                    order_no="R1",
                    status="confirmed",
                    total_aed=Decimal("20.00"),
                ),
                Order(
                    customer_id=shop.customers[1],
                    order_no="R2",
                    status="cancelled",
                    total_aed=Decimal("99.00"),
                ),
            ]
        )
    viewer = await dash.login(shop.tenant, "viewer")
    body = (await dash.client.get(f"/api/v1/m/campaigns/{cid}", headers=viewer)).json()
    assert body["results"]["modules"]["orders"] == {
        "orders": 1,
        "orders_value_aed": "20.00",
        "window_days": 7,
    }
    assert body["results"]["by_status"] == {"sent": 3}
    page = (await dash.client.get(f"/api/v1/m/campaigns/{cid}/recipients", headers=viewer)).json()
    assert page["total"] == 3


# ================================================================ dashboard flow


async def test_build_preview_approve_start(dash: DashHarness) -> None:
    shop = await make_shop(dash.db, opted_in=4, pending=2, opted_out=2)
    admin = await dash.login(shop.tenant, "admin")
    fields = (await dash.client.get("/api/v1/m/campaigns/segment-fields", headers=admin)).json()
    assert "last_order_before_days" in {f["name"] for f in fields["fields"]}
    n = await dash.client.post(
        "/api/v1/m/campaigns/segments/count",
        headers=admin,
        json={"definition": {"area_in": ["al nahda"]}},
    )
    assert n.json()["recipients"] == 2
    bad = await dash.client.post(
        "/api/v1/m/campaigns/segments/count",
        headers=admin,
        json={"definition": {"opt_in_status": "pending"}},
    )
    assert bad.status_code == 422

    r = await dash.client.post(
        "/api/v1/m/campaigns",
        headers=admin,
        json={
            "name": "Back in stock",
            "template_id": str(shop.template),
            "segment": {},
            "variable_bindings": BINDINGS,
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["segment"] == {"opt_in_status": "opted_in"}
    p = (await dash.client.get(f"/api/v1/m/campaigns/{cid}/preview", headers=admin)).json()
    assert p["recipients"] == 4
    assert p["estimated_cost_aed"] == "0.73"  # 4 x 0.183
    assert p["sample"].startswith("Hi Customer")
    assert p["problems"] == []

    assert (await dash.client.post(f"/api/v1/m/campaigns/{cid}/approve", headers=admin)).json()[
        "status"
    ] == "approved"
    # an approved campaign is frozen
    assert (
        await dash.client.patch(f"/api/v1/m/campaigns/{cid}", headers=admin, json={"name": "x"})
    ).status_code == 409
    started = await dash.client.post(f"/api/v1/m/campaigns/{cid}/start", headers=admin)
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "sending"
    assert started.json()["recipient_count"] == 4
    paused = await dash.client.post(f"/api/v1/m/campaigns/{cid}/pause", headers=admin)
    assert (paused.json()["status"], paused.json()["paused_reason"]) == ("paused", "paused_by_team")
    assert (await dash.client.post(f"/api/v1/m/campaigns/{cid}/resume", headers=admin)).json()[
        "status"
    ] == "sending"
    assert (await dash.client.post(f"/api/v1/m/campaigns/{cid}/cancel", headers=admin)).json()[
        "status"
    ] == "cancelled"


async def test_bindings_must_match_the_template(dash: DashHarness) -> None:
    shop = await make_shop(dash.db)
    admin = await dash.login(shop.tenant, "admin")
    r = await dash.client.post(
        "/api/v1/m/campaigns",
        headers=admin,
        json={
            "name": "x",
            "template_id": str(shop.template),
            "segment": {},
            "variable_bindings": BINDINGS[:1],
        },
    )
    cid = r.json()["id"]
    p = (await dash.client.get(f"/api/v1/m/campaigns/{cid}/preview", headers=admin)).json()
    assert p["problems"] == ["bindings_mismatch"]
    a = await dash.client.post(f"/api/v1/m/campaigns/{cid}/approve", headers=admin)
    assert a.status_code == 422
    assert a.json()["detail"]["code"] == "bindings_mismatch"


async def test_template_create_submit_poll_and_sync(dash: DashHarness, settings: Settings) -> None:
    shop = await make_shop(dash.db)
    admin = await dash.login(shop.tenant, "admin")
    meta = FakeMeta()
    dash.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(meta))

    rules = await dash.client.post(
        "/api/v1/m/campaigns/templates",
        headers=admin,
        json={
            "name": "promo",
            "language": "en",
            "body": "{{1}}, the cheapest water!",
            "variables": [VARS[0]],
        },
    )
    assert rules.status_code == 422
    assert rules.json()["detail"]["code"] == "template_rules"

    body = "Hi {{1}}, snacks are 10% off with your next water order this week."
    r = await dash.client.post(
        "/api/v1/m/campaigns/templates",
        headers=admin,
        json={"name": "snack_offer", "language": "en", "body": body, "variables": [VARS[0]]},
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["meta_status"] is None
    s = await dash.client.post(f"/api/v1/m/campaigns/templates/{tid}/submit", headers=admin)
    assert s.status_code == 200, s.text
    assert s.json()["meta_status"] == "PENDING"
    assert meta.created == [
        {
            "name": "snack_offer",
            "language": "en",
            "category": "MARKETING",
            "components": [{"type": "BODY", "text": body, "example": {"body_text": [["Ahmed"]]}}],
        }
    ]
    # submitted: no more edits, no second submit
    assert (
        await dash.client.patch(
            f"/api/v1/m/campaigns/templates/{tid}", headers=admin, json={"body": body}
        )
    ).status_code == 409

    # the poller picks up Meta's decision
    out = await jobs.poll_templates(worker_ctx(dash.db, settings, meta), str(shop.tenant))
    assert out == {"checked": 1, "changed": 1}
    listed = (await dash.client.get("/api/v1/m/campaigns/templates", headers=admin)).json()
    assert {t["name"]: t["meta_status"] for t in listed}["snack_offer"] == "APPROVED"

    # sync imports a template made in Meta's own tools
    meta.listed = [
        {
            "id": "77",
            "name": "made_in_meta",
            "language": "ar",
            "status": "APPROVED",
            "category": "MARKETING",
            "components": [{"type": "BODY", "text": "مرحبا {{1}}، الماء متوفر الآن."}],
        }
    ]
    sync = (await dash.client.post("/api/v1/m/campaigns/templates/sync", headers=admin)).json()
    assert sync == {"updated": 0, "imported": 1}
    async with dash.db.tenant_session(shop.tenant) as s2:
        t = await s2.scalar(select(MessageTemplate).where(MessageTemplate.name == "made_in_meta"))
    assert t is not None
    assert t.source == "meta"
    assert t.meta_status == "APPROVED"
    assert len(t.variables or []) == 1


async def test_drafter_suggests_and_meters(dash: DashHarness) -> None:
    shop = await make_shop(dash.db)
    admin = await dash.login(shop.tenant, "admin")
    seen: list[list[dict[str, Any]]] = []

    class FakeLLM:
        async def chat(self, **kw: Any) -> LLMResult:
            seen.append(kw["messages"])
            content = json.dumps(
                {
                    "name": "lapsed_winback",
                    "category": "MARKETING",
                    "language": "en",
                    "body": "Hi {{1}}, we miss you. Your next 5-gallon comes with free delivery.",
                    "variables": [{"index": 1, "meaning": "first name", "example": "Ahmed"}],
                    "rationale": "lapsed customers respond to a small, concrete reason to return",
                }
            )
            return LLMResult(
                content, [], "gemini", "gemini-3-flash", 700, 90, 900, Decimal("0.0004")
            )

    dash.app.state.llm = FakeLLM()
    r = await dash.client.post(
        "/api/v1/m/campaigns/templates/draft",
        headers=admin,
        json={
            "objective": "win back lapsed customers",
            "audience": "no order in 60 days",
            "offer": "free delivery",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "lapsed_winback"
    assert r.json()["check"]["errors"] == []
    assert "Objective: win back lapsed customers" in seen[0][0]["content"]
    async with dash.db.tenant_session(shop.tenant) as s:
        usage = (await s.scalars(select(UsageDaily))).all()
        assert (
            await s.scalar(select(MessageTemplate).where(MessageTemplate.name == "lapsed_winback"))
            is None
        )
    assert sum(u.llm_prompt_tokens for u in usage) == 700  # metered, not saved


async def test_campaigns_screens_and_today(dash: DashHarness) -> None:
    shop = await make_shop(dash.db)
    await make_campaign(dash.db, shop)
    viewer = await dash.login(shop.tenant, "viewer")
    today = (await dash.client.get("/api/v1/today", headers=viewer)).json()
    assert today["modules"]["campaigns"]["sending"] == 1
    g = (await dash.client.get("/api/v1/m/campaigns/guardrails", headers=viewer)).json()
    assert g["quality_rating"] == "GREEN"
    assert g["frequency_days"] == 7
    contact = (
        await dash.client.get(f"/api/v1/contacts/{shop.customers[0]}", headers=viewer)
    ).json()
    assert contact["modules"]["campaigns"]["received"][0]["status"] == "pending"


def test_hours_guard() -> None:
    from zoneinfo import ZoneInfo

    from api.modules.campaigns.guards import may_send_at

    gulf = ZoneInfo("Asia/Dubai")
    wed_10 = CLOCK
    assert may_send_at(wed_10, None, gulf)
    assert may_send_at(wed_10, {"Sat-Thu": "08:00-22:00"}, gulf)  # unreadable → quiet hours only
    assert not may_send_at(wed_10, {"mon": "09:00-17:00"}, gulf)  # Wednesday not listed: closed
    assert not may_send_at(wed_10, {"wed": "11:00-17:00"}, gulf)
    assert may_send_at(wed_10, {"wed": "08:00-24:00"}, gulf)
    late = datetime(2026, 9, 30, 18, 30, tzinfo=UTC)  # 22:30 Gulf
    assert not may_send_at(late, {"wed": "00:00-24:00"}, gulf)  # quiet hours win

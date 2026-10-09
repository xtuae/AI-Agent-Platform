"""Phase 5: metering by category, Meta statement reconciliation, the reimbursement ledger, and the
tenant Costs screen."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from api.billing import jobs
from api.billing.ledger import LedgerError, mark_paid, tenant_ledger
from api.billing.periods import add_months, parse_month, service_months
from api.billing.statement import StatementError, category_of, pull, reconcile
from api.core.crypto import encrypt_secret
from api.db.models import MetaStatementLine, MetaStatementMonth, Tenant, TenantChannel, UsageDaily
from api.db.session import Database
from api.meta.client import MetaClient
from api.metering import record_usage
from api.tests.conftest import DashHarness, make_tenant, meta_id, pnid

FIXTURE = Path(__file__).parent / "fixtures" / "meta_pricing_analytics_2026_10.json"
PEG = Decimal("3.6725")
OCT = date(2026, 10, 1)
RATES = {"marketing": Decimal("0.1400"), "utility": Decimal("0.0580"), "service": Decimal("0")}


# ---------------------------------------------------------------- periods


def test_add_months_clamps_to_month_end() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 11, 15), 2) == date(2027, 1, 15)
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)


def test_service_months_follow_the_contract_start() -> None:
    months = service_months(date(2026, 9, 15), date(2026, 12, 14))
    assert [(m.number, m.start, m.end) for m in months] == [
        (1, date(2026, 9, 15), date(2026, 10, 14)),
        (2, date(2026, 10, 15), date(2026, 11, 14)),
        (3, date(2026, 11, 15), date(2026, 12, 14)),
    ]
    cut = service_months(date(2026, 1, 31), date(2026, 3, 10))
    assert [(m.start, m.end) for m in cut] == [
        (date(2026, 1, 31), date(2026, 2, 27)),
        (date(2026, 2, 28), date(2026, 3, 10)),
    ]


def test_parse_month() -> None:
    assert parse_month("2026-10") == OCT
    assert parse_month(None, today=date(2026, 10, 9)) == OCT
    for bad in ("2026-13", "26-10", "2026-10-01", "1999-01"):
        with pytest.raises(ValueError, match="month"):
            parse_month(bad)


def test_meta_category_names_map_to_ours() -> None:
    assert category_of("MARKETING_LITE") == "marketing"
    assert category_of("AUTHENTICATION_INTERNATIONAL") == "authentication"
    assert category_of("REFERRAL_CONVERSION") == "referral_conversion"


# ---------------------------------------------------------------- metering


async def test_usage_is_split_by_category_and_sums_to_the_total(db: Database) -> None:
    tid = await make_tenant(db, "meter")
    at = datetime(2026, 10, 3, 9, tzinfo=UTC)
    async with db.tenant_session(tid) as s:
        await record_usage(
            s, tid, at=at, msgs_out=1, category="marketing", meta_cost_aed=Decimal("0.14")
        )
        await record_usage(
            s, tid, at=at, msgs_out=1, category="marketing", meta_cost_aed=Decimal("0.14")
        )
        await record_usage(
            s, tid, at=at, msgs_out=1, category="utility", meta_cost_aed=Decimal("0.058")
        )
        await record_usage(s, tid, at=at, msgs_out=1, category="service")
        await record_usage(s, tid, at=at, msgs_in=1)
        with pytest.raises(ValueError, match="pricing category"):
            await record_usage(s, tid, at=at, meta_cost_aed=Decimal("1"))
    async with db.tenant_session(tid) as s:
        u = await s.scalar(select(UsageDaily))
    assert u is not None
    assert (u.marketing_cost_aed, u.utility_cost_aed, u.service_cost_aed) == (
        Decimal("0.2800"),
        Decimal("0.0580"),
        Decimal("0"),
    )
    assert u.meta_cost_aed == u.marketing_cost_aed + u.utility_cost_aed + u.service_cost_aed
    assert (u.marketing_count, u.utility_count, u.service_count, u.msgs_in, u.msgs_out) == (
        2,
        1,
        1,
        1,
        4,
    )


# ---------------------------------------------------------------- Meta statement


def _fixture(scale: Decimal = Decimal(1), currency: str = "AED") -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    data["currency"] = currency
    for p in data["pricing_analytics"]["data"][0]["data_points"]:
        p["cost"] = float(Decimal(str(p["cost"])) * scale)
    return data


def _meta_transport(body: dict[str, Any], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _client_for(transport: httpx.MockTransport) -> Callable[[TenantChannel], MetaClient]:
    http = httpx.AsyncClient(transport=transport)

    def factory(ch: TenantChannel) -> MetaClient:
        return MetaClient(
            http=http, access_token="t", phone_number_id=pnid(ch), api_version="v21.0"
        )

    return factory


async def _with_channel(db: Database, label: str) -> uuid.UUID:
    tid = await make_tenant(db, label)
    async with db.platform_session() as s:
        s.add(
            TenantChannel(
                tenant_id=tid,
                phone_number_id=meta_id(),
                waba_id="123456789012345",
                access_token_encrypted=encrypt_secret("tok"),
            )
        )
    return tid


async def _meter_like_the_fixture(
    db: Database, tid: uuid.UUID, *, extra_marketing: int = 0
) -> None:
    """Our meter for October, message by message, from the same traffic as the fixture. We also
    metered `extra_marketing` sends on 6 Oct that Meta never charged (failed deliveries)."""
    points = json.loads(FIXTURE.read_text())["pricing_analytics"]["data"][0]["data_points"]
    async with db.tenant_session(tid) as s:
        for p in points:
            at = datetime.fromtimestamp(p["start"], UTC) + timedelta(hours=8)
            cat = p["pricing_category"].lower()
            n = p["volume"] + (extra_marketing if cat == "marketing" and at.day == 6 else 0)
            for _ in range(n):
                await record_usage(
                    s, tid, at=at, msgs_out=1, category=cat, meta_cost_aed=RATES[cat]
                )


async def test_a_test_month_reconciles_to_metas_statement_within_one_percent(db: Database) -> None:
    tid = await _with_channel(db, "recon")
    await _meter_like_the_fixture(db, tid, extra_marketing=2)
    seen: list[httpx.Request] = []
    result = await pull(
        db,
        tid,
        OCT,
        _client_for(_meta_transport(_fixture(), seen)),
        now=datetime(2026, 11, 4, tzinfo=UTC),
    )
    assert (result.currency, result.complete, result.lines) == ("AED", True, 91)
    assert "pricing_analytics.start(1790812800).end(1793491200)" in seen[0].url.params["fields"]

    async with db.tenant_session(tid) as s:
        rec = await reconcile(s, OCT, usd_to_aed=PEG)
    assert rec.total is not None
    assert rec.total.meta_aed == Decimal("44.01200")
    assert rec.total.ours_aed == Decimal("44.2920")  # + two sends Meta never charged
    diff = rec.total.diff_aed
    assert diff is not None
    assert abs(diff) / rec.total.meta_aed < Decimal("0.01")
    assert rec.total.status == "ok"
    assert {c.key: c.status for c in rec.categories} == {
        "marketing": "ok",
        "utility": "ok",
        "service": "ok",
        "authentication": "ok",
    }
    # the day that differs is pointed out
    assert [d.key for d in rec.days if d.status == "check"] == ["2026-10-06"]

    # pulling again replaces, never doubles
    await pull(
        db,
        tid,
        OCT,
        _client_for(_meta_transport(_fixture(), [])),
        now=datetime(2026, 11, 5, tzinfo=UTC),
    )
    async with db.tenant_session(tid) as s:
        again = await reconcile(s, OCT, usd_to_aed=PEG)
    assert again.total is not None
    assert again.total.meta_aed == Decimal("44.01200")


async def test_a_price_meta_changed_shows_up_as_a_mismatch(db: Database) -> None:
    tid = await _with_channel(db, "recon-bad")
    await _meter_like_the_fixture(db, tid)
    await pull(
        db, tid, OCT, _client_for(_meta_transport(_fixture(Decimal("1.05")), [])),
        now=datetime(2026, 10, 20, tzinfo=UTC),
    )  # fmt: skip
    async with db.tenant_session(tid) as s:
        rec = await reconcile(s, OCT, usd_to_aed=PEG)
        month = await s.get(MetaStatementMonth, (tid, OCT))
    assert rec.total is not None
    assert rec.total.status == "check"
    assert month is not None
    assert month.complete is False  # pulled before the month closed


async def test_usd_billed_waba_is_compared_through_the_peg(db: Database) -> None:
    tid = await _with_channel(db, "recon-usd")
    await _meter_like_the_fixture(db, tid)
    await pull(
        db, tid, OCT, _client_for(_meta_transport(_fixture(1 / PEG, "USD"), [])),
        now=datetime(2026, 11, 4, tzinfo=UTC),
    )  # fmt: skip
    async with db.tenant_session(tid) as s:
        rec = await reconcile(s, OCT, usd_to_aed=PEG)
    assert rec.total is not None
    assert (rec.currency, rec.total.status) == ("USD", "ok")


async def test_pull_needs_a_waba_and_stays_inside_its_tenant(db: Database) -> None:
    lonely = await make_tenant(db, "no-waba")
    with pytest.raises(StatementError, match="no WhatsApp account"):
        await pull(db, lonely, OCT, _client_for(_meta_transport(_fixture(), [])))
    a = await _with_channel(db, "iso-a")
    b = await _with_channel(db, "iso-b")
    await pull(
        db,
        a,
        OCT,
        _client_for(_meta_transport(_fixture(), [])),
        now=datetime(2026, 11, 4, tzinfo=UTC),
    )
    async with db.tenant_session(b) as s:
        assert (await s.scalars(select(MetaStatementLine))).all() == []


async def test_daily_cron_pulls_this_month_and_settles_last(db: Database) -> None:
    assert jobs.months_to_pull(date(2026, 11, 3)) == [OCT, date(2026, 11, 1)]
    assert jobs.months_to_pull(date(2026, 11, 20)) == [date(2026, 11, 1)]
    tid = await _with_channel(db, "cron")
    ctx = {
        "db": db,
        "client_factory": _client_for(_meta_transport(_fixture(), [])),
        "clock": lambda: datetime(2026, 11, 3, 4, 30, tzinfo=UTC),
    }
    out = await jobs.pull_meta_statements(ctx)
    assert out["failed"] == 0
    async with db.tenant_session(tid) as s:
        months = {m.month: m.complete for m in (await s.scalars(select(MetaStatementMonth))).all()}
    assert months == {OCT: True, date(2026, 11, 1): False}


# ---------------------------------------------------------------- reimbursement ledger


async def _borne_tenant(db: Database) -> Tenant:
    """A tenant set up the way a free-period client is: service from 15 Sep 2026, Meta charges
    borne by HMH Labz until 14 Dec 2026 (the dates the first contract uses)."""
    tid = await _with_channel(db, "borne")
    async with db.platform_session() as s:
        await s.execute(
            update(Tenant)
            .where(Tenant.id == tid)
            .values(
                service_start=date(2026, 9, 15),
                free_months_until=date(2026, 12, 14),
                meta_charges_borne_by_us_until=date(2026, 12, 14),
                monthly_fee_aed=Decimal("2500.00"),
            )
        )
        tenant = await s.get(Tenant, tid)
    assert tenant is not None
    return tenant


DAYS_AND_COSTS = {
    date(2026, 9, 14): Decimal("9.9900"),  # before service: never reimbursed
    date(2026, 9, 15): Decimal("1.2500"),
    date(2026, 10, 1): Decimal("12.4200"),
    date(2026, 10, 14): Decimal("3.3300"),
    date(2026, 10, 15): Decimal("7.0000"),
    date(2026, 11, 14): Decimal("0.5800"),
    date(2026, 11, 15): Decimal("4.2000"),
    date(2026, 12, 14): Decimal("2.1150"),
    date(2026, 12, 15): Decimal("8.8800"),  # after the borne period: the client's own cost
}


async def _usage(db: Database, tid: uuid.UUID) -> None:
    async with db.tenant_session(tid) as s:
        for day, cost in DAYS_AND_COSTS.items():
            await record_usage(
                s, tid, at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC), msgs_out=1,
                category="marketing", meta_cost_aed=cost,
            )  # fmt: skip


async def test_ledger_shows_the_right_aed_for_service_months_one_to_three(db: Database) -> None:
    tenant = await _borne_tenant(db)
    await _usage(db, tenant.id)
    ledger = await tenant_ledger(db, tenant, today=date(2026, 11, 20), usd_to_aed=PEG)
    assert [(m.number, m.start, m.end, m.payable_aed, m.basis, m.state) for m in ledger] == [
        (1, date(2026, 9, 15), date(2026, 10, 14), Decimal("17.00"), "metered", "due"),
        (2, date(2026, 10, 15), date(2026, 11, 14), Decimal("7.58"), "metered", "due"),
        (3, date(2026, 11, 15), date(2026, 12, 14), Decimal("6.32"), "metered", "running"),
    ]  # 1.25+12.42+3.33 · 7.00+0.58 · 4.20+2.115 (half up)


async def test_ledger_prefers_metas_figure_once_the_months_are_final(db: Database) -> None:
    tenant = await _borne_tenant(db)
    await _usage(db, tenant.id)

    def statement(month: date) -> dict[str, Any]:
        points = [
            {"start": int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()),
             "end": 0, "country": "AE", "pricing_category": "MARKETING",
             "pricing_type": "REGULAR", "volume": 1, "cost": float(c) - 0.01}
            for d, c in DAYS_AND_COSTS.items() if d.month == month.month
        ]  # fmt: skip
        return {"currency": "AED", "pricing_analytics": {"data": [{"data_points": points}]}}

    for month in (date(2026, 9, 1), OCT):
        await pull(
            db, tenant.id, month, _client_for(_meta_transport(statement(month), [])),
            now=datetime(2026, 11, 20, tzinfo=UTC),
        )  # fmt: skip
    ledger = await tenant_ledger(db, tenant, today=date(2026, 11, 20), usd_to_aed=PEG)
    first, second = ledger[0], ledger[1]
    assert (first.basis, first.statement_aed, first.payable_aed) == (
        "meta_statement",
        Decimal("16.97"),
        Decimal("16.97"),
    )
    assert first.metered_aed == Decimal("17.00")
    assert second.basis == "metered"  # November not pulled yet: still our estimate

    paid = await mark_paid(
        db,
        tenant,
        1,
        reference="TRX-0091",
        actor="staff:test",
        today=date(2026, 11, 20),
        usd_to_aed=PEG,
    )
    assert paid.state == "paid"
    assert paid.paid is not None
    assert (paid.paid.amount_aed, paid.paid.basis, paid.paid.reference) == (
        Decimal("16.97"),
        "meta_statement",
        "TRX-0091",
    )
    with pytest.raises(LedgerError, match="already paid"):
        await mark_paid(
            db, tenant, 1, reference=None, actor="x", today=date(2026, 11, 20), usd_to_aed=PEG
        )
    with pytest.raises(LedgerError, match="not ended"):
        await mark_paid(
            db, tenant, 3, reference=None, actor="x", today=date(2026, 11, 20), usd_to_aed=PEG
        )
    with pytest.raises(LedgerError, match="no such"):
        await mark_paid(
            db, tenant, 4, reference=None, actor="x", today=date(2026, 11, 20), usd_to_aed=PEG
        )


async def test_no_borne_period_no_ledger(db: Database) -> None:
    tid = await make_tenant(db, "paying")
    async with db.platform_session() as s:
        tenant = await s.get(Tenant, tid)
    assert tenant is not None
    assert await tenant_ledger(db, tenant, today=date(2026, 11, 20), usd_to_aed=PEG) == []


# ---------------------------------------------------------------- Costs screen


async def test_costs_screen_by_day_and_category_without_llm_cost(dash: DashHarness) -> None:
    tenant = await _borne_tenant(dash.db)
    await _usage(dash.db, tenant.id)
    async with dash.db.tenant_session(tenant.id) as s:
        await record_usage(s, tenant.id, at=datetime(2026, 12, 15, 13, tzinfo=UTC), msgs_out=1,
                           category="utility", meta_cost_aed=Decimal("0.058"),
                           llm_prompt_tokens=900, llm_cost_usd=Decimal("0.002"))  # fmt: skip
    headers = await dash.login(tenant.id, "viewer")
    r = await dash.client.get("/api/v1/costs?month=2026-12", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "llm" not in r.text  # HMH Labz's own cost never reaches the client
    assert body["total_aed"] == "11.05"
    assert body["borne_aed"] == "2.12"  # 14 Dec is borne, 15 Dec is not (2.115 → 2.12)
    assert body["due_aed"] == "8.94"
    assert {c["category"]: c["cost_aed"] for c in body["categories"]} == {
        "marketing": "11.00",
        "utility": "0.06",
        "service": "0.00",
        "authentication": "0.00",
    }
    assert [(d["day"], d["borne_by_hmh"]) for d in body["days"]] == [
        ("2026-12-14", True),
        ("2026-12-15", False),
    ]
    assert body["statement"]["total"]["status"] == "not_pulled"
    assert "2026-09" in body["months"]

    r = await dash.client.get("/api/v1/costs?month=2026-1", headers=headers)
    assert r.status_code == 422


async def test_costs_are_per_tenant(dash: DashHarness) -> None:
    a = await _borne_tenant(dash.db)
    await _usage(dash.db, a.id)
    b = await make_tenant(dash.db, "other")
    r = await dash.client.get("/api/v1/costs?month=2026-10", headers=await dash.login(b))
    assert r.json()["total_aed"] == "0.00"
    assert r.json()["days"] == []

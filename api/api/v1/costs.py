"""/api/v1/costs — the Costs screen: WhatsApp message spend by day and category against the
monthly cap, and the month reconciled against Meta's own statement.

LLM cost is HMH Labz's, not the client's, and is never returned here (the platform console shows
it). Days are UTC days, as metered and as Meta reports them. For days inside the tenant's
borne-by-HMH period, `borne_by_hmh` is true: the dashboard shows "Borne by HMH Labz" rather than
an amount due, but the figures are still shown so the cost is visible.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from api.api.v1.common import amount, load_tenant, money, unprocessable
from api.auth.deps import Viewer
from api.billing.ledger import borne_cost, month_usage
from api.billing.periods import next_month, parse_month
from api.billing.statement import Line, reconcile
from api.config import get_settings
from api.db.models import TenantSettings, UsageDaily
from api.metering import PRICING_CATEGORIES

router = APIRouter(tags=["costs"])


class DayOut(BaseModel):
    day: date
    msgs_in: int
    msgs_out: int
    counts: dict[str, int]
    costs: dict[str, str]
    total_aed: str
    borne_by_hmh: bool


class CategoryOut(BaseModel):
    category: str
    count: int
    cost_aed: str


class LineOut(BaseModel):
    key: str
    ours_count: int
    meta_count: int | None
    ours_aed: str
    meta_aed: str | None
    diff_aed: str | None
    status: str


class StatementOut(BaseModel):
    pulled_at: datetime | None
    complete: bool
    currency: str | None
    meta_total_native: str | None
    total: LineOut | None
    categories: list[LineOut]
    days: list[LineOut]


class CostsOut(BaseModel):
    month: str
    months: list[str]  # months with any usage, newest first, for the picker
    cap_aed: str | None
    total_aed: str
    pct_of_cap: float | None
    borne_aed: str
    due_aed: str
    borne_until: date | None
    categories: list[CategoryOut]
    days: list[DayOut]
    statement: StatementOut


def line_out(line: Line) -> LineOut:
    diff = line.diff_aed
    return LineOut(
        key=line.key,
        ours_count=line.ours_count,
        meta_count=line.meta_count,
        ours_aed=amount(line.ours_aed),
        meta_aed=money(line.meta_aed) if line.meta_aed is not None else None,
        diff_aed=money(diff) if diff is not None else None,
        status=line.status,
    )


@router.get("/costs", response_model=CostsOut)
async def costs(ctx: Viewer, month: str | None = None) -> CostsOut:
    try:
        first = parse_month(month)
    except ValueError as exc:
        raise unprocessable("invalid_month", message=str(exc)) from exc
    tenant = await load_tenant(ctx)
    async with ctx.platform() as s:
        tset = await s.get(TenantSettings, ctx.tenant_id)
    borne_until = tenant.meta_charges_borne_by_us_until
    async with ctx.tx() as s:
        rows, totals = await month_usage(s, first)
        rec = await reconcile(s, first, usd_to_aed=get_settings().usd_to_aed)
        firsts = (await s.scalars(select(UsageDaily.day).order_by(UsageDaily.day))).all()
    months = sorted({d.strftime("%Y-%m") for d in firsts} | {first.strftime("%Y-%m")}, reverse=True)
    today = datetime.now(UTC).date()
    if first <= today < next_month(first):
        months = sorted(set(months) | {today.strftime("%Y-%m")}, reverse=True)

    cap = tset.monthly_message_cap_aed if tset else None
    borne = borne_cost(rows, borne_until)
    return CostsOut(
        month=first.strftime("%Y-%m"),
        months=months,
        cap_aed=money(cap) if cap is not None else None,
        total_aed=amount(totals.meta_cost_aed),
        pct_of_cap=float(totals.meta_cost_aed / cap * 100) if cap else None,
        borne_aed=amount(borne),
        due_aed=amount(totals.meta_cost_aed - borne),
        borne_until=borne_until,
        categories=[
            CategoryOut(category=c, count=totals.counts[c], cost_aed=amount(totals.costs[c]))
            for c in PRICING_CATEGORIES
        ],
        days=[
            DayOut(
                day=r.day,
                msgs_in=r.msgs_in,
                msgs_out=r.msgs_out,
                counts={c: getattr(r, f"{c}_count") for c in PRICING_CATEGORIES},
                costs={c: amount(getattr(r, f"{c}_cost_aed")) for c in PRICING_CATEGORIES},
                total_aed=amount(r.meta_cost_aed),
                borne_by_hmh=borne_until is not None and r.day <= borne_until,
            )
            for r in rows
        ],
        statement=StatementOut(
            pulled_at=rec.pulled_at,
            complete=rec.complete,
            currency=rec.currency,
            meta_total_native=money(rec.meta_total_native)
            if rec.meta_total_native is not None
            else None,
            total=line_out(rec.total) if rec.total else None,
            categories=[line_out(x) for x in rec.categories],
            days=[line_out(x) for x in rec.days],
        ),
    )

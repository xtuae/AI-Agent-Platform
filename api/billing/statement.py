"""Meta's own statement next to our meter: pull WABA pricing analytics into meta_statement_lines
and reconcile a month against usage_daily.

Our figure (usage_daily) is stamped at send time from meta_rates; Meta's figure is what Meta
actually charged. They should agree within 1%. When they do not, the usual causes are a Meta
price change missing from meta_rates, messages Meta did not charge (failed / undelivered), or
free-entry-point conversations — the per-day table shows where to look.

Meta bills in the WABA's currency. AED is compared as is and USD through the dirham's fixed peg
(settings.usd_to_aed); any other currency is shown but not reconciled.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.periods import next_month, unix
from api.db.models import MetaStatementLine, MetaStatementMonth, TenantChannel, UsageDaily
from api.db.session import Database
from api.meta.client import MetaClient
from api.metering import PRICING_CATEGORIES

TOLERANCE: Final = Decimal("0.01")  # 1%
PENNY: Final = Decimal("0.01")
SETTLE: Final = timedelta(days=2)  # Meta's figures for a day can still move for a day or two


class StatementError(RuntimeError):
    pass


def category_of(meta_category: str) -> str:
    """Meta's category names → ours (MARKETING_LITE is marketing, AUTHENTICATION_INTERNATIONAL
    is authentication); anything else is kept as reported and shows as its own row."""
    c = meta_category.lower()
    for ours in PRICING_CATEGORIES:
        if c.startswith(ours):
            return ours
    return c


def to_aed(amount: Decimal, currency: str | None, usd_to_aed: Decimal) -> Decimal | None:
    if currency == "AED":
        return amount
    if currency == "USD":
        return amount * usd_to_aed
    return None


@dataclass(frozen=True)
class PullResult:
    month: date
    currency: str | None
    lines: int
    complete: bool


async def pull(
    db: Database,
    tenant_id: uuid.UUID,
    month: date,
    client_for: Callable[[TenantChannel], MetaClient],
    *,
    now: datetime | None = None,
) -> PullResult:
    """Replace `month`'s statement lines with Meta's current figures."""
    now = now or datetime.now(UTC)
    end_day = next_month(month)
    async with db.platform_session() as s:
        channels = (
            await s.scalars(
                select(TenantChannel).where(
                    TenantChannel.tenant_id == tenant_id,
                    TenantChannel.waba_id.is_not(None),
                    TenantChannel.access_token_encrypted.is_not(None),
                )
            )
        ).all()
    by_waba: dict[str, TenantChannel] = {}
    for ch in channels:
        assert ch.waba_id is not None
        by_waba.setdefault(ch.waba_id, ch)  # one pull per WABA, whichever number
    if not by_waba:
        raise StatementError("no WhatsApp account with a token to read Meta's figures from")

    start_ts = unix(month)
    end_ts = min(unix(end_day), int(now.timestamp()))
    totals: dict[tuple[date, str, str, str], list[Decimal]] = defaultdict(
        lambda: [Decimal(0), Decimal(0)]
    )
    currencies: set[str] = set()
    for waba, ch in by_waba.items():
        currency, points = await client_for(ch).pricing_analytics(waba, start_ts, end_ts)
        if currency:
            currencies.add(currency.upper())
        for p in points:
            day = datetime.fromtimestamp(p.start, UTC).date()
            if not month <= day < end_day:
                continue
            key = (
                day,
                (p.pricing_category or "unknown").lower(),
                (p.country or "unknown").upper(),
                (p.pricing_type or "regular").lower(),
            )
            totals[key][0] += p.volume
            totals[key][1] += Decimal(str(p.cost))
    if len(currencies) > 1:
        raise StatementError(f"WABAs bill in different currencies: {', '.join(sorted(currencies))}")
    currency = currencies.pop() if currencies else None
    complete = now >= datetime(end_day.year, end_day.month, end_day.day, tzinfo=UTC) + SETTLE

    async with db.tenant_session(tenant_id) as s:
        await s.execute(
            delete(MetaStatementLine).where(
                MetaStatementLine.day >= month, MetaStatementLine.day < end_day
            )
        )
        s.add_all(
            MetaStatementLine(
                day=day,
                category=cat,
                market=market,
                pricing_type=ptype,
                volume=int(vol),
                cost=cost,
                currency=currency or "unknown",
            )
            for (day, cat, market, ptype), (vol, cost) in totals.items()
        )
        stmt = insert(MetaStatementMonth).values(
            tenant_id=tenant_id, month=month, currency=currency, complete=complete, pulled_at=now
        )
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "month"],
                set_={"currency": currency, "complete": complete, "pulled_at": now},
            )
        )
    return PullResult(month, currency, len(totals), complete)


# ---------------------------------------------------------------- reconciliation


@dataclass(frozen=True)
class Line:
    key: str  # category, or ISO day
    ours_count: int
    meta_count: int | None
    ours_aed: Decimal
    meta_aed: Decimal | None
    status: str  # ok | check | not_pulled | not_comparable

    @property
    def diff_aed(self) -> Decimal | None:
        return None if self.meta_aed is None else self.ours_aed - self.meta_aed


@dataclass
class Reconciliation:
    month: date
    pulled_at: datetime | None
    complete: bool
    currency: str | None
    meta_total_native: Decimal | None  # in the WABA's currency
    categories: list[Line] = field(default_factory=list)
    days: list[Line] = field(default_factory=list)
    total: Line | None = None


def _within(a: Decimal, b: Decimal) -> bool:
    diff = abs(a - b)
    return diff <= PENNY or (b != 0 and diff / abs(b) <= TOLERANCE)


def _status(ours_n: int, meta_n: int, ours: Decimal, meta: Decimal | None) -> str:
    if meta is None:
        return "not_comparable"
    count_ok = ours_n == meta_n or (meta_n and abs(ours_n - meta_n) / meta_n <= TOLERANCE)
    return "ok" if count_ok and _within(ours, meta) else "check"


async def reconcile(s: AsyncSession, month: date, *, usd_to_aed: Decimal) -> Reconciliation:
    """Tenant session. Categories and days of `month`: our meter vs Meta's statement."""
    end_day = next_month(month)
    rec_row = await s.get(MetaStatementMonth, (s.info["tenant_id"], month))
    usage = (
        await s.scalars(select(UsageDaily).where(UsageDaily.day >= month, UsageDaily.day < end_day))
    ).all()
    lines = (
        await s.execute(
            select(
                MetaStatementLine.day,
                MetaStatementLine.category,
                func.sum(MetaStatementLine.volume),
                func.sum(MetaStatementLine.cost),
            )
            .where(MetaStatementLine.day >= month, MetaStatementLine.day < end_day)
            .group_by(MetaStatementLine.day, MetaStatementLine.category)
        )
    ).all()

    ours_cat: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    ours_day: dict[date, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for u in usage:
        for c in PRICING_CATEGORIES:
            n, cost = getattr(u, f"{c}_count"), getattr(u, f"{c}_cost_aed")
            ours_cat[c][0] += n
            ours_cat[c][1] += cost
            ours_day[u.day][0] += n
            ours_day[u.day][1] += cost

    currency = rec_row.currency if rec_row else None
    meta_cat: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    meta_day: dict[date, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    native_total = Decimal(0)
    for day, cat, vol, cost in lines:
        c = category_of(cat)
        meta_cat[c][0] += vol
        meta_cat[c][1] += cost
        meta_day[day][0] += vol
        meta_day[day][1] += cost
        native_total += cost

    rec = Reconciliation(
        month=month,
        pulled_at=rec_row.pulled_at if rec_row else None,
        complete=bool(rec_row and rec_row.complete),
        currency=currency,
        meta_total_native=native_total if rec_row else None,
    )

    def line(key: str, ours: list[Decimal], meta: list[Decimal] | None) -> Line:
        if rec_row is None:
            return Line(key, int(ours[0]), None, ours[1], None, "not_pulled")
        m = meta or [Decimal(0), Decimal(0)]
        meta_aed = to_aed(m[1], currency, usd_to_aed)
        return Line(
            key,
            int(ours[0]),
            int(m[0]),
            ours[1],
            meta_aed,
            _status(int(ours[0]), int(m[0]), ours[1], meta_aed),
        )

    for c in sorted(set(ours_cat) | set(meta_cat), key=_category_order):
        rec.categories.append(line(c, ours_cat[c], meta_cat.get(c)))
    for d in sorted(set(ours_day) | set(meta_day)):
        rec.days.append(line(d.isoformat(), ours_day[d], meta_day.get(d)))
    total_ours = [sum((v[0] for v in ours_cat.values()), Decimal(0)),
                  sum((v[1] for v in ours_cat.values()), Decimal(0))]  # fmt: skip
    total_meta = [sum((v[0] for v in meta_cat.values()), Decimal(0)),
                  sum((v[1] for v in meta_cat.values()), Decimal(0))]  # fmt: skip
    rec.total = line("total", total_ours, total_meta)
    return rec


def _category_order(c: str) -> tuple[int, str]:
    order = {k: i for i, k in enumerate(PRICING_CATEGORIES)}
    return (order.get(c, len(order)), c)

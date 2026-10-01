"""Usage summaries and the reimbursement ledger.

A tenant inside a borne-by-HMH period (tenants.service_start → meta_charges_borne_by_us_until)
pays Meta itself — each client keeps its own WABA and billing (01 §4.2) — and HMH Labz pays those
message charges back per service month. For each service month the ledger shows:

* metered   — usage_daily.meta_cost_aed over the month's days (our meter, known at once);
* statement — Meta's own figure over the same days, once every calendar month the service month
              touches has been pulled after it closed (complete) in a currency we can convert;
* payable   — the statement figure when there is one, else the metered figure (an estimate);
* state     — upcoming / running / due / paid. Paying snapshots the amount and its basis.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.periods import month_start, next_month, service_months
from api.billing.statement import to_aed
from api.db.models import (
    MetaStatementLine,
    MetaStatementMonth,
    Reimbursement,
    Tenant,
    UsageDaily,
)
from api.db.session import Database
from api.metering import PRICING_CATEGORIES

CENT: Final = Decimal("0.01")
State = Literal["upcoming", "running", "due", "paid"]
Basis = Literal["meta_statement", "metered"]


def aed(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- usage


@dataclass
class Totals:
    msgs_in: int = 0
    msgs_out: int = 0
    meta_cost_aed: Decimal = Decimal(0)
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_cost_usd: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        self.counts: dict[str, int] = dict.fromkeys(PRICING_CATEGORIES, 0)
        self.costs: dict[str, Decimal] = dict.fromkeys(PRICING_CATEGORIES, Decimal(0))

    def add(self, u: UsageDaily) -> None:
        self.msgs_in += u.msgs_in
        self.msgs_out += u.msgs_out
        self.meta_cost_aed += u.meta_cost_aed
        self.llm_prompt_tokens += u.llm_prompt_tokens
        self.llm_completion_tokens += u.llm_completion_tokens
        self.llm_cost_usd += u.llm_cost_usd
        for c in PRICING_CATEGORIES:
            self.counts[c] += getattr(u, f"{c}_count")
            self.costs[c] += getattr(u, f"{c}_cost_aed")


async def month_usage(s: AsyncSession, month: date) -> tuple[list[UsageDaily], Totals]:
    """Tenant session: the month's usage_daily rows (UTC days) and their totals."""
    rows = list(
        (
            await s.scalars(
                select(UsageDaily)
                .where(UsageDaily.day >= month, UsageDaily.day < next_month(month))
                .order_by(UsageDaily.day)
            )
        ).all()
    )
    totals = Totals()
    for r in rows:
        totals.add(r)
    return rows, totals


def borne_cost(rows: list[UsageDaily], borne_until: date | None) -> Decimal:
    """The part of these days' Meta cost that HMH Labz bears."""
    if borne_until is None:
        return Decimal(0)
    return sum((r.meta_cost_aed for r in rows if r.day <= borne_until), Decimal(0))


# ---------------------------------------------------------------- ledger


@dataclass(frozen=True)
class Paid:
    amount_aed: Decimal
    basis: str
    reference: str | None
    paid_at: datetime
    recorded_by: str


@dataclass(frozen=True)
class LedgerMonth:
    tenant_id: uuid.UUID
    tenant_name: str
    number: int
    start: date
    end: date
    metered_aed: Decimal
    statement_aed: Decimal | None
    payable_aed: Decimal
    basis: Basis
    state: State
    paid: Paid | None


async def tenant_ledger(
    db: Database, tenant: Tenant, *, today: date, usd_to_aed: Decimal
) -> list[LedgerMonth]:
    if tenant.service_start is None or tenant.meta_charges_borne_by_us_until is None:
        return []
    periods = service_months(tenant.service_start, tenant.meta_charges_borne_by_us_until)
    if not periods:
        return []
    first, last = periods[0].start, periods[-1].end

    async with db.tenant_session(tenant.id) as s:
        metered: dict[date, Decimal] = dict(
            (
                await s.execute(
                    select(UsageDaily.day, UsageDaily.meta_cost_aed).where(
                        UsageDaily.day >= first, UsageDaily.day <= last
                    )
                )
            )
            .tuples()
            .all()
        )
        stated: dict[date, Decimal] = defaultdict(Decimal)
        for d, c in (
            await s.execute(
                select(MetaStatementLine.day, func.sum(MetaStatementLine.cost))
                .where(MetaStatementLine.day >= first, MetaStatementLine.day <= last)
                .group_by(MetaStatementLine.day)
            )
        ).all():
            stated[d] = c
        pulled = {
            m.month: m
            for m in (
                await s.scalars(
                    select(MetaStatementMonth).where(
                        MetaStatementMonth.month >= month_start(first),
                        MetaStatementMonth.month <= last,
                    )
                )
            ).all()
        }
    async with db.platform_session() as s:
        paid = {
            r.service_month: r
            for r in (
                await s.scalars(select(Reimbursement).where(Reimbursement.tenant_id == tenant.id))
            ).all()
        }

    out: list[LedgerMonth] = []
    for p in periods:
        m_aed = sum((c for d, c in metered.items() if p.start <= d <= p.end), Decimal(0))
        months = {month_start(p.start), month_start(p.end)}
        statement: Decimal | None = None
        if all(m in pulled and pulled[m].complete for m in months):
            currencies = {pulled[m].currency for m in months}
            native = sum((c for d, c in stated.items() if p.start <= d <= p.end), Decimal(0))
            if len(currencies) == 1:
                statement = to_aed(native, currencies.pop(), usd_to_aed)
        row = paid.get(p.number)
        state: State
        if row is not None:
            state = "paid"
        elif today < p.start:
            state = "upcoming"
        elif today <= p.end:
            state = "running"
        else:
            state = "due"
        out.append(
            LedgerMonth(
                tenant_id=tenant.id,
                tenant_name=tenant.name,
                number=p.number,
                start=p.start,
                end=p.end,
                metered_aed=aed(m_aed),
                statement_aed=aed(statement) if statement is not None else None,
                payable_aed=aed(statement if statement is not None else m_aed),
                basis="meta_statement" if statement is not None else "metered",
                state=state,
                paid=Paid(row.amount_aed, row.basis, row.reference, row.paid_at, row.recorded_by)
                if row
                else None,
            )
        )
    return out


class LedgerError(ValueError):
    pass


async def mark_paid(
    db: Database,
    tenant: Tenant,
    number: int,
    *,
    reference: str | None,
    actor: str,
    today: date,
    usd_to_aed: Decimal,
) -> LedgerMonth:
    months = {
        m.number: m for m in await tenant_ledger(db, tenant, today=today, usd_to_aed=usd_to_aed)
    }
    m = months.get(number)
    if m is None:
        raise LedgerError("no such service month in the borne-by-HMH period")
    if m.state == "paid":
        raise LedgerError("already paid")
    if m.state != "due":
        raise LedgerError("this service month has not ended yet")
    async with db.platform_session() as s:
        s.add(
            Reimbursement(
                tenant_id=tenant.id,
                service_month=number,
                period_start=m.start,
                period_end=m.end,
                amount_aed=m.payable_aed,
                basis=m.basis,
                reference=reference,
                recorded_by=actor,
            )
        )
    done = {
        x.number: x for x in await tenant_ledger(db, tenant, today=today, usd_to_aed=usd_to_aed)
    }
    return done[number]

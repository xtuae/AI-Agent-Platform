"""Calendar arithmetic for billing: calendar months (usage, statements) and service months
(contract periods counted from a tenant's service_start). All days are UTC days, like
usage_daily and Meta's pricing analytics."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


def add_months(d: date, n: int) -> date:
    """Same day n months later, clamped to the month's end (31 Jan + 1 → 28/29 Feb)."""
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def month_start(d: date) -> date:
    return d.replace(day=1)


def next_month(first: date) -> date:
    return add_months(first.replace(day=1), 1)


def parse_month(value: str | None, *, today: date | None = None) -> date:
    """'2026-10' → date(2026, 10, 1); None → the current UTC month."""
    if value is None:
        return month_start(today or datetime.now(UTC).date())
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("month must look like 2026-10")
    year, month = map(int, value.split("-"))
    if not 1 <= month <= 12 or not 2020 <= year <= 2100:
        raise ValueError("month out of range")
    return date(year, month, 1)


def unix(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


@dataclass(frozen=True)
class ServiceMonth:
    number: int  # 1-based
    start: date
    end: date  # inclusive


def service_months(service_start: date, until: date) -> list[ServiceMonth]:
    """Service months from `service_start` whose first day is on or before `until`; the last
    one is cut at `until` (inclusive). 15 Sep → 15 Sep to 14 Oct, 15 Oct to 14 Nov, and so on."""
    out: list[ServiceMonth] = []
    n = 1
    while True:
        start = add_months(service_start, n - 1)
        if start > until:
            return out
        end = min(add_months(service_start, n) - timedelta(days=1), until)
        out.append(ServiceMonth(n, start, end))
        n += 1

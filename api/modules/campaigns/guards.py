"""The send-time guards of 02 §4.4 that do not need Meta: hours, messaging tier, budget, quality.

Each is a pure function or a single query, so the sender and the dashboard ask the same question.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Campaign, CampaignRecipient, TenantChannel, UsageDaily

GULF: Final = ZoneInfo("Asia/Dubai")
QUIET_START: Final = time(22, 0)  # never 22:00-08:00 Gulf time, whatever the business hours
QUIET_END: Final = time(8, 0)
DAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# unique customers a WABA may start conversations with per 24 h, by Meta messaging tier
TIER_LIMIT: Final[dict[str, int | None]] = {
    "TIER_50": 50,
    "TIER_250": 250,
    "TIER_1K": 1_000,
    "TIER_2K": 2_000,
    "TIER_10K": 10_000,
    "TIER_100K": 100_000,
    "TIER_UNLIMITED": None,
    "UNLIMITED": None,
}
UNKNOWN_TIER_LIMIT: Final = 250  # not read from Meta yet: assume the smallest real tier


def _span(raw: Any) -> tuple[time, time] | None:
    if not isinstance(raw, str) or raw.strip().lower() == "closed" or "-" not in raw:
        return None
    a, b = raw.strip().split("-", 1)
    try:
        start = time.fromisoformat(a)
        end = time(23, 59, 59) if b == "24:00" else time.fromisoformat(b)
    except ValueError:
        return None
    return (start, end) if start < end else None


def may_send_at(at: datetime, business_hours: dict[str, Any] | None, tz: ZoneInfo) -> bool:
    """Inside business hours (when the tenant has set them; a day missing from them is closed)
    and outside 22:00-08:00 Gulf time."""
    gulf = at.astimezone(GULF).time()
    if gulf >= QUIET_START or gulf < QUIET_END:
        return False
    if not business_hours or not any(k in DAYS for k in business_hours):
        return True  # not set (or an old free-text format): only the quiet hours apply
    local = at.astimezone(tz)
    span = _span(business_hours.get(DAYS[local.weekday()]))
    return span is not None and span[0] <= local.time() < span[1]


def next_send_time(
    at: datetime, business_hours: dict[str, Any] | None, tz: ZoneInfo
) -> datetime | None:
    """The next quarter hour at which a marketing send is allowed, within 8 days."""
    t = at.replace(second=0, microsecond=0)
    t += timedelta(minutes=15 - t.minute % 15)
    for _ in range(8 * 24 * 4):
        if may_send_at(t, business_hours, tz):
            return t
        t += timedelta(minutes=15)
    return None


def tier_limit(channel: TenantChannel) -> int | None:
    tier = (channel.messaging_limit_tier or "").upper()
    return TIER_LIMIT.get(tier, UNKNOWN_TIER_LIMIT) if tier else UNKNOWN_TIER_LIMIT


async def sent_last_24h(s: AsyncSession, now: datetime) -> int:
    """Distinct customers this tenant sent a campaign message to in the last 24 h."""
    return int(
        await s.scalar(
            select(func.count(func.distinct(CampaignRecipient.customer_id))).where(
                CampaignRecipient.sent_at > now - timedelta(hours=24)
            )
        )
        or 0
    )


def quality_block(channel: TenantChannel) -> str | None:
    """YELLOW pauses marketing; RED stops it (02 §4.4)."""
    rating = (channel.quality_rating or "").upper()
    if rating == "RED":
        return "quality_red"
    if rating == "YELLOW":
        return "quality_yellow"
    return None


async def month_spend(s: AsyncSession, now: datetime) -> Decimal:
    first = date(now.year, now.month, 1)
    return Decimal(
        await s.scalar(
            select(func.coalesce(func.sum(UsageDaily.meta_cost_aed), 0)).where(
                UsageDaily.day >= first
            )
        )
        or 0
    )


async def budget_block(
    s: AsyncSession,
    campaign: Campaign,
    next_cost: Decimal,
    monthly_cap: Decimal | None,
    now: datetime,
) -> str | None:
    """The live budget check before every send: the campaign's own cap, then the tenant's
    monthly message cap (which covers the agent's replies too)."""
    if (
        campaign.budget_cap_aed is not None
        and campaign.spend_aed + next_cost > campaign.budget_cap_aed
    ):
        return "budget"
    if monthly_cap is not None and await month_spend(s, now) + next_cost > monthly_cap:
        return "monthly_cap"
    return None

"""Meta message pricing: category → AED, table-driven and dated (meta_rates).

`cost_aed` is stamped at send time from the rate effective on that UTC day for the RECIPIENT's
market. A Meta price change is a new meta_rates row with an effective_from date — no deploy, and
historical messages keep the price they were actually charged.

This module never guesses: no rate row → PricingNotConfiguredError. The caller must decide
(the outbound path refuses to send an unpriceable message rather than send it unmetered).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.metering import PRICING_CATEGORIES, PricingCategory

# Country calling code → market. Longest prefix wins. Extend when a tenant serves new markets
# (and add meta_rates rows for them at the same time).
_CALLING_CODES: Final[dict[str, str]] = {
    "971": "AE",
    # India: added for testing from +91 phones. Only a `service` rate row exists for IN (free
    # within the customer-service window, per Meta's pricing page); marketing/utility/auth stay
    # unpriced until rows from Meta's INR rate card are added, so those sends are refused.
    "91": "IN",
}

_RATE_SQL = text(
    """
    SELECT rate_aed FROM meta_rates
    WHERE market = :market AND category = :category
      AND effective_from <= :day AND (effective_to IS NULL OR :day < effective_to)
    ORDER BY effective_from DESC
    LIMIT 1
    """
)


class PricingNotConfiguredError(LookupError):
    """No rate row covers this market/category/day."""


def market_for(wa_id: str) -> str:
    for prefix in sorted(_CALLING_CODES, key=len, reverse=True):
        if wa_id.startswith(prefix):
            return _CALLING_CODES[prefix]
    raise PricingNotConfiguredError(f"no market configured for calling code of {wa_id[:4]}…")


async def price_message(
    session: AsyncSession,
    *,
    category: PricingCategory,
    recipient_wa_id: str,
    at: datetime | None = None,
) -> Decimal:
    if category not in PRICING_CATEGORIES:
        raise PricingNotConfiguredError(f"unknown pricing category {category!r}")
    market = market_for(recipient_wa_id)
    day = (at or datetime.now(UTC)).astimezone(UTC).date()
    rate = await session.scalar(_RATE_SQL, {"market": market, "category": category, "day": day})
    if rate is None:
        raise PricingNotConfiguredError(f"no {market}/{category} rate effective on {day}")
    return Decimal(rate)

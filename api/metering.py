"""usage_daily upserts — the billing spine (constraint 7).

Called inside the SAME tenant transaction that writes the message row, so a message and its
meter move together: either both commit or neither does.

Day boundary: UTC. Meta's pricing analytics and invoices are reported in UTC, and Phase 5 must
reconcile this table to Meta's statement within 1%.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import UsageDaily

PricingCategory = Literal["marketing", "utility", "service", "authentication"]
PRICING_CATEGORIES: Final = ("marketing", "utility", "service", "authentication")

_CATEGORY_COLUMN: Final[dict[str, str]] = {
    "marketing": "marketing_count",
    "utility": "utility_count",
    "service": "service_count",
    "authentication": "authentication_count",
}

_ADDITIVE = (
    "msgs_in",
    "msgs_out",
    "marketing_count",
    "utility_count",
    "service_count",
    "authentication_count",
    "meta_cost_aed",
    "llm_prompt_tokens",
    "llm_completion_tokens",
    "llm_cost_usd",
)


def usage_day(at: datetime | None = None) -> date:
    return (at or datetime.now(UTC)).astimezone(UTC).date()


async def record_usage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    at: datetime | None = None,
    msgs_in: int = 0,
    msgs_out: int = 0,
    category: PricingCategory | None = None,
    meta_cost_aed: Decimal = Decimal(0),
    llm_prompt_tokens: int = 0,
    llm_completion_tokens: int = 0,
    llm_cost_usd: Decimal = Decimal(0),
) -> None:
    """Atomically add to the day's counters (ON CONFLICT DO UPDATE SET x = x + excluded.x)."""
    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "day": usage_day(at),
        "msgs_in": msgs_in,
        "msgs_out": msgs_out,
        "marketing_count": 0,
        "utility_count": 0,
        "service_count": 0,
        "authentication_count": 0,
        "meta_cost_aed": meta_cost_aed,
        "llm_prompt_tokens": llm_prompt_tokens,
        "llm_completion_tokens": llm_completion_tokens,
        "llm_cost_usd": llm_cost_usd,
    }
    if category is not None:
        if category not in _CATEGORY_COLUMN:
            raise ValueError(f"unknown pricing category {category!r}")
        values[_CATEGORY_COLUMN[category]] = 1

    stmt = insert(UsageDaily).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageDaily.tenant_id, UsageDaily.day],
        set_={c: getattr(UsageDaily, c) + getattr(stmt.excluded, c) for c in _ADDITIVE},
    )
    await session.execute(stmt)

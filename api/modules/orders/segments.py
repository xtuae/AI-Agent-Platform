"""Segment fields the orders module adds to campaigns (02 §4.3), and what a campaign led to."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import CampaignRecipient, Customer, Order
from api.modules.base import SegmentField, SegmentScope
from api.modules.orders.service import money


def _lapsed(days: int, scope: SegmentScope) -> ColumnElement[bool]:
    return Customer.last_order_at < scope.now - timedelta(days=days)


def _never_bought(category: str, _scope: SegmentScope) -> ColumnElement[bool]:
    return ~exists(
        select(Order.id).where(
            Order.customer_id == Customer.id,
            Order.status != "cancelled",
            Order.items.contains([{"category": category}]),
        )
    )


FIELDS = (
    SegmentField(
        name="last_order_before_days",
        label="Last order more than … days ago",
        kind="int",
        help="Customers who have ordered, but not in this many days",
        minimum=1,
        maximum=3650,
        clause=_lapsed,
    ),
    SegmentField(
        name="lifetime_orders_gte",
        label="At least … orders to date",
        kind="int",
        help="Regular customers",
        minimum=1,
        maximum=100_000,
        clause=lambda n, _s: Customer.lifetime_orders >= n,
    ),
    SegmentField(
        name="never_purchased_category",
        label="Never bought from category",
        kind="text",
        help="e.g. snack — for a first cross-sell offer",
        choices=("water", "snack", "other"),
        clause=_never_bought,
    ),
)


async def attribution(s: AsyncSession, campaign_id: uuid.UUID, days: int) -> dict[str, Any]:
    """Orders placed by recipients within `days` of receiving the campaign message."""
    window = and_(
        Order.customer_id == CampaignRecipient.customer_id,
        Order.created_at >= CampaignRecipient.sent_at,
        Order.created_at < CampaignRecipient.sent_at + timedelta(days=days),
        Order.status != "cancelled",
    )
    n, value = (
        await s.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.total_aed), 0))
            .select_from(CampaignRecipient)
            .join(Order, window)
            .where(
                CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.sent_at.is_not(None)
            )
        )
    ).one()
    return {"orders": int(n), "orders_value_aed": money(Decimal(value)), "window_days": days}

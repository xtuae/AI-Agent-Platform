"""Segment fields the coupons module adds to campaigns (02 §4.3)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import ColumnElement, and_, exists, func, or_, select

from api.db.models import CouponBook, Customer
from api.modules.base import SegmentField, SegmentScope


def _low(n: int, scope: SegmentScope) -> ColumnElement[bool]:
    """Has (had) a coupon book, and the unexpired balance is n bottles or fewer — including those
    who have just run out."""
    unexpired = or_(CouponBook.expires_at.is_(None), CouponBook.expires_at >= scope.today)
    balance = (
        select(func.coalesce(func.sum(CouponBook.bottles_remaining), 0))
        .where(CouponBook.customer_id == Customer.id, unexpired)
        .scalar_subquery()
    )
    return and_(
        exists(select(CouponBook.id).where(CouponBook.customer_id == Customer.id)), balance <= n
    )


def _expiring(days: int, scope: SegmentScope) -> ColumnElement[bool]:
    return exists(
        select(CouponBook.id).where(
            CouponBook.customer_id == Customer.id,
            CouponBook.bottles_remaining > 0,
            CouponBook.expires_at >= scope.today,
            CouponBook.expires_at <= scope.today + timedelta(days=days),
        )
    )


FIELDS = (
    SegmentField(
        name="bottles_remaining_lte",
        label="Coupon bottles left at most",
        kind="int",
        help="Coupon-book customers running low (0 = ran out)",
        minimum=0,
        maximum=1000,
        clause=_low,
    ),
    SegmentField(
        name="coupon_expires_within_days",
        label="A coupon book expires within … days",
        kind="int",
        help="Unused bottles about to expire",
        minimum=1,
        maximum=365,
        clause=_expiring,
    ),
)

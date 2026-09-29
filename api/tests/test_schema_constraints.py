"""Database-level guards that back the non-negotiable constraints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from api.db.models import Campaign, CouponBook, Customer
from api.db.session import Database
from api.tests.conftest import TenantPair


async def test_campaign_cannot_leave_draft_without_approval(
    db: Database, tenants: TenantPair
) -> None:
    """Constraint 8, enforced by the database even if the API is wrong."""
    for status in ("approved", "sending", "paused", "done"):
        with pytest.raises(IntegrityError, match="approval_required"):
            async with db.tenant_session(tenants.a) as s:
                s.add(Campaign(name="x", status=status))


async def test_campaign_needs_both_approved_by_and_approved_at(
    db: Database, tenants: TenantPair
) -> None:
    with pytest.raises(IntegrityError, match="approval_required"):
        async with db.tenant_session(tenants.a) as s:
            s.add(Campaign(name="x", status="approved", approved_by=uuid.uuid4()))


async def test_approved_campaign_is_accepted(db: Database, tenants: TenantPair) -> None:
    async with db.tenant_session(tenants.a) as s:
        s.add(
            Campaign(
                name="ok",
                status="approved",
                approved_by=uuid.uuid4(),
                approved_at=datetime.now(UTC),
            )
        )
        s.add(Campaign(name="draft-then-cancelled", status="cancelled"))


async def test_customer_defaults_to_pending_opt_in(db: Database, tenants: TenantPair) -> None:
    async with db.tenant_session(tenants.a) as s:
        c = Customer(wa_id="971504444444")
        s.add(c)
        await s.flush()
        await s.refresh(c)
        assert c.opt_in_status == "pending"


async def test_coupon_balance_cannot_go_negative(db: Database, tenants: TenantPair) -> None:
    with pytest.raises(IntegrityError, match="bottles_remaining"):
        async with db.tenant_session(tenants.a) as s:
            s.add(CouponBook(customer_id=tenants.customer_a, bottles_remaining=-1))

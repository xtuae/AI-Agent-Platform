"""What the campaigns module shows on Today and in the contact panel."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Campaign, CampaignRecipient, TenantChannel
from api.modules.base import TodayScope
from api.modules.campaigns import guards


async def today(s: AsyncSession, scope: TodayScope) -> dict[str, Any]:
    counts = {
        st: int(n)
        for st, n in (
            await s.execute(
                select(Campaign.status, func.count())
                .where(Campaign.status.in_(("sending", "paused", "approved")))
                .group_by(Campaign.status)
            )
        ).all()
    }
    paused = (
        await s.execute(
            select(Campaign.name, Campaign.paused_reason)
            .where(Campaign.status == "paused")
            .limit(5)
        )
    ).all()
    channel = await s.scalar(
        select(TenantChannel)
        .where(TenantChannel.tenant_id == scope.tenant_id, TenantChannel.is_active.is_(True))
        .limit(1)
    )
    return {
        "sending": counts.get("sending", 0),
        "approved": counts.get("approved", 0),
        "paused": [{"name": n, "reason": r} for n, r in paused],
        "quality_rating": channel.quality_rating if channel else None,
        "quality_block": guards.quality_block(channel) if channel else None,
    }


async def contact_panel(s: AsyncSession, customer_id: uuid.UUID, _today: date) -> dict[str, Any]:
    rows = (
        await s.execute(
            select(CampaignRecipient, Campaign.name)
            .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
            .where(CampaignRecipient.customer_id == customer_id)
            .order_by(CampaignRecipient.created_at.desc())
            .limit(20)
        )
    ).all()
    return {
        "received": [
            {
                "campaign": name,
                "status": r.status,
                "skip_reason": r.skip_reason,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "replied_at": r.replied_at.isoformat() if r.replied_at else None,
            }
            for r, name in rows
        ]
    }

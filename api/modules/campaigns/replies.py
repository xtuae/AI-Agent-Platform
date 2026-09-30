"""Replies to campaigns (02 §4.2): counted once per recipient, and given context.

A customer message within `reply_window_hours` of a campaign message is a reply to it. The first
one stamps campaign_recipients.replied_at and bumps campaigns.reply_count; every one gets the
spec's context block, which the turn loop adds to the support prompt."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.agents.text import clean_inline
from api.db.models import Campaign, CampaignRecipient, Message
from api.modules.base import ModuleConfig
from api.modules.campaigns.config import config_of

BLOCK_PATH: Final = (
    Path(__file__).resolve().parents[2] / "agents/prompts/templates" / ("campaign_reply_v1.j2")
)
DELIVERED: Final = ("sent", "delivered", "read")


def block(sent_date: str, body: str) -> str:
    text = BLOCK_PATH.read_text()
    return (
        text.replace("{{campaign_sent_date}}", sent_date)
        .replace("{{campaign_body_rendered}}", body)
        .strip()
    )


async def reply_context(
    s: AsyncSession, customer_id: uuid.UUID, now: datetime, config: ModuleConfig | None
) -> str | None:
    window = timedelta(hours=config_of(config).reply_window_hours)
    rec = await s.scalar(
        select(CampaignRecipient)
        .where(
            CampaignRecipient.customer_id == customer_id,
            CampaignRecipient.status.in_(DELIVERED),
            CampaignRecipient.sent_at > now - window,
            CampaignRecipient.sent_at <= now,
        )
        .order_by(CampaignRecipient.sent_at.desc())
        .limit(1)
        .with_for_update()
    )
    if rec is None or rec.sent_at is None:
        return None
    if rec.replied_at is None:
        rec.replied_at = now
        campaign = await s.get(Campaign, rec.campaign_id, with_for_update=True)
        if campaign is not None:
            campaign.reply_count += 1
    body = (
        await s.scalar(select(Message.body).where(Message.wamid == rec.wamid))
        if rec.wamid
        else None
    )
    if not body:
        return None
    return block(rec.sent_at.date().isoformat(), clean_inline(body, 700) or "")

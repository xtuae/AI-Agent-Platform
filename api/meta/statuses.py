"""Delivery status webhooks → messages.status and campaign counters.

Statuses arrive out of order and more than once. A status is applied only if it moves the message
FORWARD (sent → delivered → read); 'failed' applies unless the message was already delivered or
read. Campaign counters change only on a forward transition, under a row lock, so replays and
concurrent jobs cannot double-count.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Campaign, CampaignRecipient, Message
from api.webhooks.payloads import Status

_RANK = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3}


def should_apply(current: str | None, new: str) -> bool:
    if new == "failed":
        return current not in ("delivered", "read", "failed")
    if new not in _RANK or current == "failed":
        return False
    return _RANK[new] > _RANK.get(current or "", -1)


@dataclass
class StatusResult:
    messages_updated: int = 0
    recipients_updated: int = 0
    unknown: int = 0


async def apply_statuses(session: AsyncSession, statuses: list[Status]) -> StatusResult:
    result = StatusResult()
    for st in statuses:
        matched = False
        msg = await session.scalar(
            select(Message)
            .where(Message.wamid == st.id, Message.direction == "out")
            .with_for_update()
        )
        if msg is not None:
            matched = True
            if should_apply(msg.status, st.status):
                msg.status = st.status
                if st.status == "failed" and st.errors:
                    msg.error_code = (
                        str(st.errors[0].code) if st.errors[0].code is not None else None
                    )
                    msg.error_detail = (st.errors[0].title or "")[:300] or None
                if msg.pricing_category is None and st.pricing and st.pricing.category:
                    msg.pricing_category = st.pricing.category
                result.messages_updated += 1

        rec = await session.scalar(
            select(CampaignRecipient).where(CampaignRecipient.wamid == st.id).with_for_update()
        )
        if rec is not None:
            matched = True
            previous = rec.status
            if should_apply(previous, st.status):
                rec.status = st.status
                result.recipients_updated += 1
                delivered = int(
                    st.status in ("delivered", "read")
                    and _RANK.get(previous or "", -1) < _RANK["delivered"]
                )
                read = int(st.status == "read")
                if delivered or read:
                    await session.execute(
                        update(Campaign)
                        .where(Campaign.id == rec.campaign_id)
                        .values(
                            delivered_count=Campaign.delivered_count + delivered,
                            read_count=Campaign.read_count + read,
                        )
                    )
        if not matched:
            result.unknown += 1
    return result

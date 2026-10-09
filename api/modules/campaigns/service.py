"""Campaign lifecycle shared by the dashboard and the sender.

draft ──approve (a person)──▶ approved ──start──▶ sending ──▶ done
                                   ▲                  │
                                   └──── paused ◀─────┘ (budget, quality, template, a person)
cancelled from anywhere but done.

* approve records approved_by + approved_at; the database refuses any status past draft without
  them (constraint 8), so there is no path to 'sending' that skips a person.
* start snapshots the segment into campaign_recipients ('pending'). Who actually receives the
  message is decided again at send time (opt-in, frequency cap) — see sender.py.
* A campaign has a channel. WhatsApp: an approved Meta template, to customers with a number.
  Telegram (06 §6): free text with the same {{n}} variables, no template, no cost, only to
  customers who started the bot and have not blocked it — and, as everywhere, only opted-in ones.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.logging import get_logger
from api.db.models import (
    AuditLog,
    Campaign,
    CampaignRecipient,
    Customer,
    CustomerIdentity,
    MessageTemplate,
)
from api.meta.pricing import PricingNotConfiguredError, market_for, price_message
from api.metering import PricingCategory
from api.modules.base import SegmentScope
from api.modules.campaigns import segments
from api.modules.campaigns.bindings import parse, values_for
from api.modules.campaigns.templates import placeholder_count, render
from api.modules.registry import Enabled

log = get_logger(__name__)

SNAPSHOT_CHUNK: Final = 1000
TEXT_MAX_CHARS: Final = 4000


class CampaignError(Exception):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Preview:
    recipients: int
    estimated_cost_aed: Decimal | None  # None when a market has no rate configured
    sample: str | None
    sample_to: str | None


async def template_for(s: AsyncSession, campaign: Campaign) -> MessageTemplate:
    if campaign.template_id is None:
        raise CampaignError("no_template")
    template = await s.get(MessageTemplate, campaign.template_id)
    if template is None:
        raise CampaignError("no_template")
    return template


def check_ready(campaign: Campaign, template: MessageTemplate) -> None:
    """Everything a campaign needs before a person may approve or start it."""
    if (template.meta_status or "").upper() != "APPROVED":
        raise CampaignError("template_not_approved", status=template.meta_status)
    if (template.category or "").upper() not in ("MARKETING", "UTILITY"):
        raise CampaignError("template_category")
    needed = placeholder_count(template.body or "")
    try:
        bindings = parse(campaign.variable_bindings)
    except ValueError as exc:
        raise CampaignError("invalid_bindings", detail=str(exc)[:200]) from exc
    if len(bindings) != needed:
        raise CampaignError("bindings_mismatch", needed=needed, given=len(bindings))
    if not campaign.segment_query:
        raise CampaignError("no_segment")


def check_text_ready(campaign: Campaign) -> None:
    """A Telegram campaign: a body, its variables bound, a segment. No template exists there."""
    body = (campaign.body or "").strip()
    if not body:
        raise CampaignError("no_body")
    if len(body) > TEXT_MAX_CHARS:
        raise CampaignError("body_too_long", maximum=TEXT_MAX_CHARS)
    try:
        bindings = parse(campaign.variable_bindings)
    except ValueError as exc:
        raise CampaignError("invalid_bindings", detail=str(exc)[:200]) from exc
    needed = placeholder_count(body)
    if len(bindings) != needed:
        raise CampaignError("bindings_mismatch", needed=needed, given=len(bindings))
    if not campaign.segment_query:
        raise CampaignError("no_segment")


async def ready(s: AsyncSession, campaign: Campaign) -> None:
    """check_ready for the campaign's channel."""
    if campaign.channel_kind == "telegram":
        check_text_ready(campaign)
    else:
        check_ready(campaign, await template_for(s, campaign))


def reachable(channel_kind: str) -> ColumnElement[bool]:
    """Customers the campaign's channel can reach at all (opt-in is added by segments)."""
    if channel_kind == "whatsapp":
        return Customer.wa_id.is_not(None)
    return exists(
        select(CustomerIdentity.id).where(
            CustomerIdentity.customer_id == Customer.id,
            CustomerIdentity.kind == channel_kind,
            CustomerIdentity.blocked_at.is_(None),
        )
    )


async def preview(
    s: AsyncSession, campaign: Campaign, enabled: Enabled, scope: SegmentScope
) -> Preview:
    """Recipient count, estimated AED cost and one rendered sample — before anything sends."""
    definition = campaign.segment_query or {}
    audience = reachable(campaign.channel_kind)
    total = int(await s.scalar(segments.count(definition, enabled, scope).where(audience)) or 0)
    if campaign.channel_kind != "whatsapp":  # no per-message cost; free text, no template
        sample = sample_to = None
        first = await s.scalar(
            segments.customers(definition, enabled, scope).where(audience).limit(1)
        )
        if first is not None and campaign.body:
            try:
                sample = render(campaign.body, values_for(parse(campaign.variable_bindings), first))
                sample_to = first.name
            except ValueError:
                sample = None
        return Preview(total, Decimal(0), sample, sample_to)
    markets = [
        wa
        for wa in (
            await s.execute(
                segments.customers(definition, enabled, scope)
                .where(audience)
                .with_only_columns(Customer.wa_id)
                .limit(20_000)
            )
        )
        .scalars()
        .all()
        if wa
    ]
    by_market: dict[str, int] = {}
    for wa in markets:
        m = market_for(wa)
        by_market[m] = by_market.get(m, 0) + 1
    template = await s.get(MessageTemplate, campaign.template_id) if campaign.template_id else None
    category: PricingCategory = (
        "utility" if (template and (template.category or "").upper() == "UTILITY") else "marketing"
    )
    cost: Decimal | None = Decimal(0)
    for market, n in by_market.items():
        sample_wa = next(w for w in markets if market_for(w) == market)
        try:
            unit = await price_message(
                s, category=category, recipient_wa_id=sample_wa, at=scope.now
            )
        except PricingNotConfiguredError:
            cost = None
            break
        cost = (cost or Decimal(0)) + unit * n
    if cost is not None and len(markets) < total:  # more than we priced: scale up
        cost = (cost / max(len(markets), 1) * total).quantize(Decimal("0.01"))
    sample = sample_to = None
    if template is not None and template.body:
        first = await s.scalar(
            segments.customers(definition, enabled, scope).where(audience).limit(1)
        )
        if first is not None:
            try:
                sample = render(template.body, values_for(parse(campaign.variable_bindings), first))
                sample_to = first.name or first.wa_id
            except ValueError:
                sample = None
    return Preview(total, cost, sample, sample_to)


def audit(s: AsyncSession, actor: str, action: str, campaign: Campaign, **after: Any) -> None:
    s.add(
        AuditLog(
            actor=actor,
            action=action,
            entity="campaign",
            entity_id=campaign.id,
            after=after or None,
        )
    )


def approve(
    s: AsyncSession, campaign: Campaign, user_id: uuid.UUID, actor: str, now: datetime
) -> None:
    if campaign.status != "draft":
        raise CampaignError("invalid_transition", status=campaign.status)
    campaign.status = "approved"
    campaign.approved_by = user_id
    campaign.approved_at = now
    audit(s, actor, "approve_campaign", campaign)


async def start(
    s: AsyncSession, campaign: Campaign, enabled: Enabled, scope: SegmentScope, actor: str
) -> int:
    """approved → sending: snapshot the segment. Returns the number of recipients."""
    if campaign.status != "approved":
        raise CampaignError("invalid_transition", status=campaign.status)
    if campaign.approved_by is None or campaign.approved_at is None:  # the DB refuses it too
        raise CampaignError("not_approved")
    await ready(s, campaign)
    ids = (
        await s.scalars(
            select(Customer.id).where(
                segments.condition(campaign.segment_query, enabled, scope),
                reachable(campaign.channel_kind),
            )
        )
    ).all()
    for i in range(0, len(ids), SNAPSHOT_CHUNK):
        await s.execute(
            insert(CampaignRecipient)
            .values(
                [
                    {
                        "tenant_id": campaign.tenant_id,
                        "campaign_id": campaign.id,
                        "customer_id": cid,
                        "status": "pending",
                    }
                    for cid in ids[i : i + SNAPSHOT_CHUNK]
                ]
            )
            .on_conflict_do_nothing(index_elements=["campaign_id", "customer_id"])
        )
    n = int(
        await s.scalar(select(func.count()).where(CampaignRecipient.campaign_id == campaign.id))
        or 0
    )
    campaign.recipient_count = n
    campaign.status = "sending"
    campaign.started_at = campaign.started_at or scope.now
    campaign.paused_reason = None
    audit(s, actor, "start_campaign", campaign, recipients=n)
    return n


def pause(s: AsyncSession, campaign: Campaign, reason: str, actor: str) -> None:
    if campaign.status not in ("sending", "approved"):
        raise CampaignError("invalid_transition", status=campaign.status)
    campaign.status = "paused"
    campaign.paused_reason = reason
    audit(s, actor, "pause_campaign", campaign, reason=reason)
    if actor == "system":
        # the alert: loud in the logs (and so in monitoring); the dashboard shows the reason
        log.critical(
            "campaign_paused",
            tenant_id=str(campaign.tenant_id),
            campaign_id=str(campaign.id),
            reason=reason,
        )


def resume(s: AsyncSession, campaign: Campaign, actor: str, quality: str | None) -> None:
    if campaign.status != "paused":
        raise CampaignError("invalid_transition", status=campaign.status)
    if quality is not None:
        raise CampaignError("quality_block", reason=quality)
    campaign.status = "sending"
    campaign.paused_reason = None
    audit(s, actor, "resume_campaign", campaign)


def cancel(s: AsyncSession, campaign: Campaign, actor: str, reason: str | None = None) -> None:
    if campaign.status in ("done", "cancelled"):
        raise CampaignError("invalid_transition", status=campaign.status)
    campaign.status = "cancelled"
    campaign.finished_at = datetime.now(UTC)
    if reason:
        campaign.paused_reason = reason
    audit(s, actor, "cancel_campaign", campaign, reason=reason)


async def pause_all_marketing(s: AsyncSession, reason: str) -> int:
    """Tenant-wide stop (WhatsApp quality guard): pause every sending WhatsApp campaign; RED
    cancels them. Another channel's campaigns are not affected by a WhatsApp number's quality."""
    live = (
        await s.scalars(
            select(Campaign)
            .where(
                Campaign.status.in_(("sending", "approved")), Campaign.channel_kind == "whatsapp"
            )
            .with_for_update()
        )
    ).all()
    for c in live:
        if reason == "quality_red":
            cancel(s, c, "system", reason)
            log.critical(
                "campaign_stopped", tenant_id=str(c.tenant_id), campaign_id=str(c.id), reason=reason
            )
        elif c.status == "sending":
            pause(s, c, reason, "system")
    return len(live)


async def bump(s: AsyncSession, campaign_id: uuid.UUID, **deltas: Any) -> None:
    await s.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id)
        .values({k: getattr(Campaign, k) + v for k, v in deltas.items()})
    )

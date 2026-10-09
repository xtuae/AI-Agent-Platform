"""The campaign sender (02 §4.4). An ARQ job that sends ONE batch and re-queues itself.

Each run, in order — any guard that fails stops the run before a message goes out:
  1. the campaign is 'sending' (so it was approved by a person — the DB guarantees that)
  2. quality: the channel's rating is not YELLOW/RED; otherwise all of the tenant's marketing
     pauses (YELLOW) or stops (RED)
  3. the template is still APPROVED, re-checked against Meta
  4. hours: inside business hours and never 22:00-08:00 Gulf time; otherwise re-queued for the
     next allowed time (the campaign stays 'sending')
  5. messaging tier: fewer distinct customers in 24 h than the WABA's tier allows
Then up to `throttle_per_minute` pending recipients, each checked AT SEND TIME:
  - still opted in (someone who opted out after the campaign was built is skipped)
  - no marketing message from any campaign in the last `frequency_days`
  - the live budget: the campaign's cap and the tenant's monthly cap, with this message's price
and sent through meta.outbound.send_template, which meters it (messages + usage_daily).

A recipient is marked 'sending' before the Meta call and never retried from that state, so a crash
or an ambiguous timeout can cost one delivery report, never a double message. A Redis lock keeps
two runs of one campaign from overlapping.

A Telegram campaign (06 §6) runs the same loop without the Meta-only guards — no quality rating,
no template, no tier, no price — and with the bot instead: free text to customers who started the
bot. Its own guard is the block rate: if more than BLOCK_RATE_MAX of the sends so far found the
bot blocked (after BLOCK_RATE_MIN_SAMPLE sends), the campaign pauses before it annoys more people.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.channels.base import ChannelSender
from api.channels.errors import ChannelAPIError, RecipientBlockedError, RecipientUnreachableError
from api.channels.outbound import _prepare, _record, mark_blocked
from api.channels.registry import sender_for
from api.config import Settings
from api.core.logging import get_logger
from api.db.models import (
    Campaign,
    CampaignRecipient,
    Conversation,
    Customer,
    Message,
    MessageTemplate,
    Tenant,
    TenantChannel,
    TenantSettings,
)
from api.db.session import Database
from api.meta.client import MetaAPIError, MetaClient
from api.meta.outbound import client_for_channel, send_template
from api.meta.pricing import PricingNotConfiguredError, price_message
from api.metering import PricingCategory
from api.modules import registry
from api.modules.campaigns import guards
from api.modules.campaigns.bindings import components, parse, values_for
from api.modules.campaigns.config import config_of
from api.modules.campaigns.service import pause, pause_all_marketing
from api.modules.campaigns.templates import render

log = get_logger(__name__)

JOB: Final = "run_campaign"
LOCK_TTL_S: Final = 300
NEXT_RUN_S: Final = 60
TIER_WAIT: Final = timedelta(hours=1)
MAX_CONSECUTIVE_FAILURES: Final = 5
# Meta error codes (Cloud API)
RECIPIENT_ERRORS: Final = frozenset({131026, 131047, 131049, 131050, 131051, 131052, 131056})
TEMPLATE_ERRORS: Final = range(132000, 132100)
SPAM_LIMIT: Final = 131048  # Meta is rate-limiting us for spam: stop before quality drops
RATE_LIMITS: Final = frozenset({4, 80007, 130429})
BLOCK_RATE_MAX: Final = 0.03
BLOCK_RATE_MIN_SAMPLE: Final = 30


@dataclass
class RunReport:
    status: str
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    next_run_in_s: int | None = None
    reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Dubai")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Dubai")


async def _requeue(
    ctx: dict[str, Any], tenant_id: uuid.UUID, campaign_id: uuid.UUID, delay_s: int
) -> None:
    at = datetime.now(UTC) + timedelta(seconds=delay_s)
    await ctx["redis"].enqueue_job(
        JOB,
        str(tenant_id),
        str(campaign_id),
        _job_id=f"campaign:{campaign_id}:{int(at.timestamp()) // 60}",
        _defer_by=timedelta(seconds=delay_s),
    )


async def _channel(
    db: Database, tenant_id: uuid.UUID, kind: str = "whatsapp"
) -> TenantChannel | None:
    async with db.platform_session() as s:
        channel: TenantChannel | None = await s.scalar(
            select(TenantChannel)
            .where(
                TenantChannel.tenant_id == tenant_id,
                TenantChannel.kind == kind,
                TenantChannel.is_active.is_(True),
            )
            .order_by(TenantChannel.id)
            .limit(1)
        )
        return channel


async def _conversation(
    s: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID, channel_id: uuid.UUID
) -> uuid.UUID:
    """The customer's open conversation on this channel, opened if there is none. A template
    does not open the free-form 24 h window, so none is set."""
    stmt = (
        insert(Conversation)
        .values(tenant_id=tenant_id, customer_id=customer_id, channel_id=channel_id, state="open")
        .on_conflict_do_update(
            index_elements=[
                Conversation.tenant_id,
                Conversation.customer_id,
                Conversation.channel_id,
            ],
            index_where=text("state <> 'closed'"),
            set_={"state": Conversation.state},
        )
        .returning(Conversation.id)
    )
    conversation_id: uuid.UUID = (await s.execute(stmt)).scalar_one()
    return conversation_id


async def run_campaign(ctx: dict[str, Any], tenant_id: str, campaign_id: str) -> dict[str, Any]:
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(campaign_id)
    lock = f"campaign-run:{cid}"
    if not await ctx["redis"].set(lock, "1", nx=True, ex=LOCK_TTL_S):
        return {"status": "busy"}
    try:
        report = await _run(ctx, tid, cid)
    finally:
        await ctx["redis"].delete(lock)
    if report.next_run_in_s is not None:
        await _requeue(ctx, tid, cid, report.next_run_in_s)
    log.info(
        "campaign_run",
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        status=report.status,
        sent=report.sent,
        skipped=report.skipped,
        failed=report.failed,
    )
    return {
        "status": report.status,
        "sent": report.sent,
        "skipped": report.skipped,
        "failed": report.failed,
        "reasons": report.reasons,
    }


async def _run(ctx: dict[str, Any], tid: uuid.UUID, cid: uuid.UUID) -> RunReport:
    db: Database = ctx["db"]
    settings: Settings = ctx["settings"]
    http: httpx.AsyncClient = ctx["http"]
    now: datetime = ctx.get("clock", _utcnow)()

    async with db.platform_session() as s:
        tenant = await s.get(Tenant, tid)
        ts = await s.get(TenantSettings, tid)
        enabled = await registry.enabled_for(s, tid)
    if tenant is None or not enabled.has("campaigns"):
        return RunReport("module_off")
    cfg = config_of(enabled.config("campaigns"))
    tz = _zone(tenant.timezone)

    # 1. still sending?
    async with db.tenant_session(tid) as s:
        campaign = await s.get(Campaign, cid)
        if campaign is None or campaign.status != "sending":
            return RunReport(campaign.status if campaign else "missing")
        if campaign.channel_kind != "whatsapp":
            return await _run_text(ctx, tid, cid, campaign, ts, cfg.frequency_days, tz, now)
        template = (
            await s.get(MessageTemplate, campaign.template_id) if campaign.template_id else None
        )
        if template is None:
            pause(s, campaign, "no_template", "system")
            return RunReport("paused")

    # 2. quality
    channel = await _channel(db, tid)
    if channel is None or channel.waba_id is None:
        async with db.tenant_session(tid) as s:
            c = await s.get(Campaign, cid, with_for_update=True)
            if c is not None and c.status == "sending":
                pause(s, c, "no_channel", "system")
        return RunReport("paused")
    blocked = guards.quality_block(channel)
    if blocked:
        async with db.tenant_session(tid) as s:
            await pause_all_marketing(s, blocked)
        return RunReport(blocked)

    client: MetaClient = ctx.get(
        "client_factory", lambda ch: client_for_channel(ch, http, settings)
    )(channel)

    # 3. template still APPROVED at Meta
    try:
        infos = await client.get_template_status(channel.waba_id, template.name)
    except MetaAPIError as exc:
        log.warning("campaign_template_check_failed", campaign_id=str(cid), status=exc.status_code)
        return RunReport("retry", next_run_in_s=NEXT_RUN_S)
    meta_status = next(
        (i.status for i in infos if (i.language or template.language) == template.language), None
    )
    if (meta_status or "").upper() != "APPROVED":
        async with db.tenant_session(tid) as s:
            t = await s.get(MessageTemplate, template.id)
            if t is not None and meta_status:
                t.meta_status = meta_status.upper()
            c = await s.get(Campaign, cid, with_for_update=True)
            if c is not None and c.status == "sending":
                pause(s, c, "template_not_approved", "system")
        return RunReport("paused")

    # 4. hours
    hours = ts.business_hours if ts else None
    if not guards.may_send_at(now, hours, tz):
        nxt = guards.next_send_time(now, hours, tz)
        wait = int((nxt - now).total_seconds()) if nxt else 24 * 3600
        return RunReport("outside_hours", next_run_in_s=max(wait, NEXT_RUN_S))

    # 5. messaging tier
    limit = guards.tier_limit(channel)
    async with db.tenant_session(tid) as s:
        used = await guards.sent_last_24h(s, now)
    room = (
        campaign.throttle_per_minute
        if limit is None
        else min(campaign.throttle_per_minute, limit - used)
    )
    if room <= 0:
        return RunReport("tier_limit", next_run_in_s=int(TIER_WAIT.total_seconds()))

    # the batch
    async with db.tenant_session(tid) as s:
        batch = (
            await s.scalars(
                select(CampaignRecipient.id)
                .where(CampaignRecipient.campaign_id == cid, CampaignRecipient.status == "pending")
                .order_by(CampaignRecipient.created_at, CampaignRecipient.id)
                .limit(room)
            )
        ).all()
    if not batch:
        async with db.tenant_session(tid) as s:
            c = await s.get(Campaign, cid, with_for_update=True)
            if c is not None and c.status == "sending":
                c.status = "done"
                c.finished_at = now
        return RunReport("done")

    report = RunReport("sending", next_run_in_s=NEXT_RUN_S)
    bindings = parse(campaign.variable_bindings)
    monthly_cap = ts.monthly_message_cap_aed if ts else None
    category: PricingCategory = (
        "utility" if (template.category or "").upper() == "UTILITY" else "marketing"
    )
    run = _Batch(
        db=db,
        client=client,
        tenant_id=tid,
        campaign_id=cid,
        template=template,
        bindings=bindings,
        channel_id=channel.id,
        frequency_days=cfg.frequency_days,
        monthly_cap=monthly_cap,
        category=category,
        now=now,
    )
    consecutive = 0
    for rid in batch:
        outcome = await _one(run, rid)
        if outcome.startswith("skip:"):
            report.skip(outcome[5:])
            continue
        if outcome == "sent":
            report.sent += 1
            consecutive = 0
            continue
        if outcome.startswith("pause:"):
            async with db.tenant_session(tid) as s:
                c = await s.get(Campaign, cid, with_for_update=True)
                if c is not None and c.status == "sending":
                    pause(s, c, outcome[6:], "system")
            report.status, report.next_run_in_s = "paused", None
            return report
        if outcome == "rate_limited":
            return report
        report.failed += 1
        consecutive += 1
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            async with db.tenant_session(tid) as s:
                c = await s.get(Campaign, cid, with_for_update=True)
                if c is not None and c.status == "sending":
                    pause(s, c, "meta_errors", "system")
            report.status, report.next_run_in_s = "paused", None
            return report
    return report


@dataclass(frozen=True)
class _Batch:
    db: Database
    client: MetaClient
    tenant_id: uuid.UUID
    campaign_id: uuid.UUID
    template: MessageTemplate
    bindings: list[Any]
    channel_id: uuid.UUID
    frequency_days: int
    monthly_cap: Decimal | None
    category: PricingCategory
    now: datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _one(run: _Batch, rid: uuid.UUID) -> str:
    """Check, claim and send to one recipient. Returns 'sent', 'failed', 'rate_limited',
    'skip:<reason>' or 'pause:<reason>'."""
    db, client, tid, cid = run.db, run.client, run.tenant_id, run.campaign_id
    template, bindings, now = run.template, run.bindings, run.now
    async with db.tenant_session(tid) as s:
        rec = await s.get(CampaignRecipient, rid, with_for_update=True)
        campaign = await s.get(Campaign, cid, with_for_update=True)
        if rec is None or campaign is None or rec.status != "pending":
            return "skip:already_handled"
        if campaign.status != "sending":
            return "pause:" + (campaign.paused_reason or campaign.status)
        customer = await s.get(Customer, rec.customer_id)
        reason: str | None = None
        if customer is None or customer.opt_in_status != "opted_in":
            reason = (
                "opted_out"
                if customer and customer.opt_in_status == "opted_out"
                else "not_opted_in"
            )
        elif await s.scalar(
            select(CampaignRecipient.id)
            .where(
                CampaignRecipient.customer_id == customer.id,
                CampaignRecipient.id != rec.id,
                CampaignRecipient.sent_at > now - timedelta(days=run.frequency_days),
            )
            .limit(1)
        ):
            reason = "frequency_cap"
        if reason is not None:
            rec.status, rec.skip_reason = "skipped", reason
            campaign.skipped_count += 1
            return f"skip:{reason}"
        assert customer is not None
        if not customer.wa_id:  # known only on another channel since the snapshot
            rec.status, rec.skip_reason = "skipped", "no_whatsapp"
            campaign.skipped_count += 1
            return "skip:no_whatsapp"
        try:
            price = await price_message(
                s, category=run.category, recipient_wa_id=customer.wa_id, at=now
            )
        except PricingNotConfiguredError:
            return "pause:unpriced"
        blocked = await guards.budget_block(s, campaign, price, run.monthly_cap, now)
        if blocked:
            return f"pause:{blocked}"
        values = values_for(bindings, customer)
        rec.status, rec.variables = "sending", values
        conversation_id = await _conversation(s, tid, customer.id, run.channel_id)

    try:
        message_id = await send_template(
            db,
            client,
            tenant_id=tid,
            conversation_id=conversation_id,
            template=template,
            components=components(values),
            rendered_body=render(template.body or "", values),
        )
    except MetaAPIError as exc:
        return await _failed(db, tid, cid, rid, exc)

    async with db.tenant_session(tid) as s:
        msg = await s.get(Message, message_id)
        rec = await s.get(CampaignRecipient, rid, with_for_update=True)
        campaign = await s.get(Campaign, cid, with_for_update=True)
        if rec is not None and msg is not None:
            rec.status, rec.wamid, rec.sent_at, rec.cost_aed = "sent", msg.wamid, now, msg.cost_aed
        if campaign is not None and msg is not None:
            campaign.sent_count += 1
            campaign.spend_aed += msg.cost_aed or 0
    return "sent"


# ---------------------------------------------------------------- free-text channels (Telegram)


async def _pause(db: Database, tid: uuid.UUID, cid: uuid.UUID, reason: str) -> None:
    async with db.tenant_session(tid) as s:
        c = await s.get(Campaign, cid, with_for_update=True)
        if c is not None and c.status == "sending":
            pause(s, c, reason, "system")


async def _run_text(
    ctx: dict[str, Any],
    tid: uuid.UUID,
    cid: uuid.UUID,
    campaign: Campaign,
    ts: TenantSettings | None,
    frequency_days: int,
    tz: ZoneInfo,
    now: datetime,
) -> RunReport:
    db: Database = ctx["db"]
    channel = await _channel(db, tid, campaign.channel_kind)
    if channel is None:
        await _pause(db, tid, cid, "no_channel")
        return RunReport("paused")
    factory = ctx.get("sender_factory") or (lambda ch: sender_for(ch, ctx["http"], ctx["settings"]))
    sender: ChannelSender = factory(channel)

    hours = ts.business_hours if ts else None
    if not guards.may_send_at(now, hours, tz):
        nxt = guards.next_send_time(now, hours, tz)
        wait = int((nxt - now).total_seconds()) if nxt else 24 * 3600
        return RunReport("outside_hours", next_run_in_s=max(wait, NEXT_RUN_S))

    async with db.tenant_session(tid) as s:
        batch = (
            await s.scalars(
                select(CampaignRecipient.id)
                .where(CampaignRecipient.campaign_id == cid, CampaignRecipient.status == "pending")
                .order_by(CampaignRecipient.created_at, CampaignRecipient.id)
                .limit(campaign.throttle_per_minute)
            )
        ).all()
    if not batch:
        async with db.tenant_session(tid) as s:
            c = await s.get(Campaign, cid, with_for_update=True)
            if c is not None and c.status == "sending":
                c.status = "done"
                c.finished_at = now
        return RunReport("done")

    report = RunReport("sending", next_run_in_s=NEXT_RUN_S)
    run = _TextBatch(
        db=db,
        sender=sender,
        tenant_id=tid,
        campaign_id=cid,
        body=campaign.body or "",
        bindings=parse(campaign.variable_bindings),
        channel_id=channel.id,
        frequency_days=frequency_days,
        monthly_cap=ts.monthly_message_cap_aed if ts else None,
        now=now,
    )
    consecutive = 0
    for rid in batch:
        outcome = await _one_text(run, rid)
        if outcome.startswith("skip:"):
            report.skip(outcome[5:])
            continue
        if outcome == "sent":
            report.sent += 1
            consecutive = 0
            continue
        if outcome.startswith("pause:"):
            await _pause(db, tid, cid, outcome[6:])
            report.status, report.next_run_in_s = "paused", None
            return report
        if outcome == "rate_limited":
            return report
        report.failed += 1
        if outcome == "blocked":
            if await _block_rate_exceeded(db, tid, cid):
                await _pause(db, tid, cid, "block_rate")
                report.status, report.next_run_in_s = "paused", None
                return report
            continue  # a person blocking the bot says nothing about the channel's health
        consecutive += 1
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            await _pause(db, tid, cid, "channel_errors")
            report.status, report.next_run_in_s = "paused", None
            return report
    return report


async def _block_rate_exceeded(db: Database, tid: uuid.UUID, cid: uuid.UUID) -> bool:
    async with db.tenant_session(tid) as s:
        attempts, blocked = (
            await s.execute(
                select(
                    func.count(),
                    func.count().filter(CampaignRecipient.skip_reason == "blocked"),
                ).where(
                    CampaignRecipient.campaign_id == cid,
                    CampaignRecipient.status.in_(("sent", "failed")),
                )
            )
        ).one()
    return int(attempts) >= BLOCK_RATE_MIN_SAMPLE and int(blocked) / int(attempts) > BLOCK_RATE_MAX


@dataclass(frozen=True)
class _TextBatch:
    db: Database
    sender: ChannelSender
    tenant_id: uuid.UUID
    campaign_id: uuid.UUID
    body: str
    bindings: list[Any]
    channel_id: uuid.UUID
    frequency_days: int
    monthly_cap: Decimal | None
    now: datetime


async def _one_text(run: _TextBatch, rid: uuid.UUID) -> str:
    """Like _one, for a free-text channel. Returns 'sent', 'failed', 'blocked', 'rate_limited',
    'skip:<reason>' or 'pause:<reason>'."""
    db, tid, cid, now = run.db, run.tenant_id, run.campaign_id, run.now
    async with db.tenant_session(tid) as s:
        rec = await s.get(CampaignRecipient, rid, with_for_update=True)
        campaign = await s.get(Campaign, cid, with_for_update=True)
        if rec is None or campaign is None or rec.status != "pending":
            return "skip:already_handled"
        if campaign.status != "sending":
            return "pause:" + (campaign.paused_reason or campaign.status)
        customer = await s.get(Customer, rec.customer_id)
        reason: str | None = None
        if customer is None or customer.opt_in_status != "opted_in":
            reason = (
                "opted_out"
                if customer and customer.opt_in_status == "opted_out"
                else "not_opted_in"
            )
        elif await s.scalar(
            select(CampaignRecipient.id)
            .where(
                CampaignRecipient.customer_id == customer.id,
                CampaignRecipient.id != rec.id,
                CampaignRecipient.sent_at > now - timedelta(days=run.frequency_days),
            )
            .limit(1)
        ):
            reason = "frequency_cap"
        if reason is not None:
            rec.status, rec.skip_reason = "skipped", reason
            campaign.skipped_count += 1
            return f"skip:{reason}"
        assert customer is not None
        blocked = await guards.budget_block(s, campaign, Decimal(0), run.monthly_cap, now)
        if blocked:
            return f"pause:{blocked}"
        values = values_for(run.bindings, customer)
        rec.status, rec.variables = "sending", values
        conversation_id = await _conversation(s, tid, customer.id, run.channel_id)
    text = render(run.body, values)

    try:
        prepared = await _prepare(db, tid, conversation_id, category="marketing")
    except RecipientUnreachableError:
        return await _text_failed(db, tid, cid, rid, "unreachable", count_failed=False)
    try:
        result = await run.sender.send_text(prepared.to, text)
    except RecipientBlockedError:
        await mark_blocked(db, tid, conversation_id)
        await _text_failed(db, tid, cid, rid, "blocked")
        return "blocked"
    except ChannelAPIError as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return await _text_failed(db, tid, cid, rid, "rate_limited", retry=True)
        return await _text_failed(db, tid, cid, rid, f"channel_{status_code or 'no_answer'}")
    message_id = await _record(
        db, tid, conversation_id, prepared, result, msg_type="text", body=text
    )

    async with db.tenant_session(tid) as s:
        msg = await s.get(Message, message_id)
        rec = await s.get(CampaignRecipient, rid, with_for_update=True)
        campaign = await s.get(Campaign, cid, with_for_update=True)
        if rec is not None and msg is not None:
            rec.status, rec.wamid, rec.sent_at, rec.cost_aed = "sent", msg.wamid, now, msg.cost_aed
        if campaign is not None and msg is not None:
            campaign.sent_count += 1
            campaign.spend_aed += msg.cost_aed or 0
    return "sent"


async def _text_failed(
    db: Database,
    tid: uuid.UUID,
    cid: uuid.UUID,
    rid: uuid.UUID,
    detail: str,
    *,
    retry: bool = False,
    count_failed: bool = True,
) -> str:
    async with db.tenant_session(tid) as s:
        rec = await s.get(CampaignRecipient, rid, with_for_update=True)
        campaign = await s.get(Campaign, cid, with_for_update=True)
        if rec is not None:
            if retry:
                rec.status = "pending"  # refused before sending: safe to try again later
            elif not count_failed:
                rec.status, rec.skip_reason = "skipped", detail
                if campaign is not None:
                    campaign.skipped_count += 1
            else:
                rec.status, rec.skip_reason = "failed", detail
                if campaign is not None:
                    campaign.failed_count += 1
    log.warning("campaign_send_failed", campaign_id=str(cid), reason=detail)
    if retry:
        return "rate_limited"
    return "failed" if count_failed else f"skip:{detail}"


async def _failed(
    db: Database, tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID, exc: MetaAPIError
) -> str:
    code = exc.code
    if exc.status_code is None:
        detail, outcome = "no_answer_from_meta", "failed"  # ambiguous: never re-sent
    elif code in RATE_LIMITS:
        detail, outcome = "rate_limited", "rate_limited"
    elif code == SPAM_LIMIT:
        detail, outcome = "meta_spam_limit", "pause:meta_spam_limit"
    elif code in TEMPLATE_ERRORS or code in (131008, 131009, 132000):
        detail, outcome = f"template_error_{code}", "pause:template_error"
    elif exc.status_code in (401, 403) or code in (190, 10, 200):
        detail, outcome = "meta_auth", "pause:meta_auth"
    elif code in RECIPIENT_ERRORS:
        detail, outcome = f"meta_{code}", "failed"
    else:
        detail, outcome = f"meta_{code or exc.status_code}", "failed"
    async with db.tenant_session(tid) as s:
        rec = await s.get(CampaignRecipient, rid, with_for_update=True)
        campaign = await s.get(Campaign, cid, with_for_update=True)
        if rec is not None:
            if outcome == "rate_limited":
                rec.status = "pending"  # Meta refused before sending: safe to try again later
            else:
                rec.status, rec.skip_reason = "failed", detail
                if campaign is not None:
                    campaign.failed_count += 1
    log.warning("campaign_send_failed", campaign_id=str(cid), reason=detail)
    return outcome

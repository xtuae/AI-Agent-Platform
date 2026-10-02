"""Background jobs of the campaigns module.

* refresh_quality — read the phone number's quality rating and messaging tier from Meta. Queued by
  the phone_number_quality_update webhook and every 30 minutes. YELLOW pauses all the tenant's
  marketing, RED stops it (02 §4.4). Only that tenant: every channel is read and acted on alone.
* poll_templates — templates still PENDING at Meta get their status re-read (the status webhook
  normally gets there first).
* campaign_housekeeping — the scheduler's cron: queues both for every tenant with the module on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import select

from api.alerts import raise_alert
from api.config import Settings
from api.core.logging import get_logger
from api.db.models import MessageTemplate, TenantChannel, TenantModule
from api.db.session import Database
from api.meta.client import MetaAPIError, MetaClient
from api.meta.outbound import ChannelNotConfiguredError, client_for_channel
from api.modules.campaigns import guards
from api.modules.campaigns.service import pause_all_marketing

log = get_logger(__name__)

QUALITY_JOB: Final = "refresh_quality"
TEMPLATES_JOB: Final = "poll_templates"


def _client(ctx: dict[str, Any], channel: TenantChannel) -> MetaClient:
    factory = ctx.get("client_factory")
    if factory is not None:
        client: MetaClient = factory(channel)
        return client
    http: httpx.AsyncClient = ctx["http"]
    settings: Settings = ctx["settings"]
    return client_for_channel(channel, http, settings)


async def refresh_quality(ctx: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    db: Database = ctx["db"]
    tid = uuid.UUID(tenant_id)
    async with db.platform_session() as s:
        channels = (
            await s.scalars(
                select(TenantChannel).where(
                    TenantChannel.tenant_id == tid, TenantChannel.is_active.is_(True)
                )
            )
        ).all()
    worst: str | None = None
    for ch in channels:
        try:
            status = await _client(ctx, ch).phone_status()
        except (MetaAPIError, ChannelNotConfiguredError) as exc:
            log.warning("quality_refresh_failed", tenant_id=tenant_id, error=type(exc).__name__)
            continue
        async with db.platform_session() as s:
            row = await s.get(TenantChannel, ch.id, with_for_update=True)
            if row is None:
                continue
            before = row.quality_rating
            row.quality_rating = (status.quality_rating or before or "").upper() or None
            row.messaging_limit_tier = status.messaging_limit_tier or row.messaging_limit_tier
            row.quality_updated_at = datetime.now(UTC)
            if before != row.quality_rating:
                log.warning(
                    "quality_rating_changed",
                    tenant_id=tenant_id,
                    before=before,
                    after=row.quality_rating,
                )
            block = guards.quality_block(row)
            changed = (before, row.quality_rating) if before != row.quality_rating else None
            phone = row.display_phone or "a number"
        if changed is not None and before is not None:  # first reading is not a "change"
            await raise_alert(
                ctx["redis"],
                f"quality:{ch.id}:{changed[1]}",
                f"Quality rating for {phone} ({tenant_id[:8]}) "
                f"changed {changed[0]} → {changed[1]}.",
                cooldown_s=6 * 3600,
            )
        if block and (worst is None or block == "quality_red"):
            worst = block
    stopped = 0
    if worst is not None:
        async with db.tenant_session(tid) as s:
            stopped = await pause_all_marketing(s, worst)
    return {"channels": len(channels), "block": worst, "campaigns_stopped": stopped}


async def poll_templates(ctx: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    db: Database = ctx["db"]
    tid = uuid.UUID(tenant_id)
    async with db.tenant_session(tid) as s:
        pending = (
            await s.scalars(
                select(MessageTemplate).where(MessageTemplate.meta_status == "PENDING").limit(50)
            )
        ).all()
        names = [(t.id, t.name, t.language) for t in pending]
    if not names:
        return {"checked": 0}
    async with db.platform_session() as s:
        channel = await s.scalar(
            select(TenantChannel).where(
                TenantChannel.tenant_id == tid,
                TenantChannel.is_active.is_(True),
                TenantChannel.waba_id.is_not(None),
            )
        )
    if channel is None or channel.waba_id is None:
        return {"checked": 0}
    client = _client(ctx, channel)
    changed = 0
    for template_id, name, language in names:
        try:
            infos = await client.get_template_status(channel.waba_id, name)
        except MetaAPIError:
            continue
        info = next((i for i in infos if (i.language or language) == language), None)
        if info is None:
            continue
        async with db.tenant_session(tid) as s:
            t = await s.get(MessageTemplate, template_id)
            if t is not None and (t.meta_status or "") != info.status.upper():
                t.meta_status = info.status.upper()
                t.rejected_reason = info.rejected_reason
                t.meta_template_id = t.meta_template_id or info.id
                changed += 1
    return {"checked": len(names), "changed": changed}


async def campaign_housekeeping(ctx: dict[str, Any]) -> None:
    """Cron (scheduler): queue quality + template checks for every tenant with campaigns on."""
    settings: Settings = ctx["settings"]
    db: Database = ctx["db"]
    async with db.platform_session() as s:
        tenants = (
            await s.scalars(
                select(TenantModule.tenant_id).where(
                    TenantModule.module_key == "campaigns", TenantModule.enabled.is_(True)
                )
            )
        ).all()
    slot = int((datetime.now(UTC) - datetime(2026, 1, 1, tzinfo=UTC)) / timedelta(minutes=15))
    for tid in tenants:
        for job in (QUALITY_JOB, TEMPLATES_JOB):
            await ctx["redis"].enqueue_job(
                job, str(tid), _job_id=f"{job}:{tid}:{slot}", _queue_name=settings.arq_queue_name
            )

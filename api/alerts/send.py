"""Deliver queued alerts (scheduler job, every minute)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select

from api.alerts import OUTBOX
from api.config import Settings
from api.core.logging import get_logger
from api.db.models import Tenant, TenantChannel
from api.db.session import Database
from api.meta.client import MetaAPIError, MetaClient
from api.meta.outbound import ChannelNotConfiguredError
from api.meta.pricing import PricingNotConfiguredError, price_message
from api.metering import record_usage

log = get_logger(__name__)

GIVE_UP_AFTER_S: Final = 3600
BATCH: Final = 20


async def _channel(db: Database, slug: str) -> tuple[Any, TenantChannel] | None:
    async with db.platform_session() as s:
        row = (
            await s.execute(
                select(Tenant.id, TenantChannel)
                .join(TenantChannel, TenantChannel.tenant_id == Tenant.id)
                .where(Tenant.slug == slug, TenantChannel.is_active.is_(True))
                .limit(1)
            )
        ).first()
    return (row[0], row[1]) if row else None


async def send_alerts(ctx: dict[str, Any]) -> dict[str, int]:
    redis = ctx["redis"]
    db: Database = ctx["db"]
    settings: Settings = ctx["settings"]
    recipients = [r.strip().lstrip("+") for r in (settings.alert_to or "").split(",") if r.strip()]
    target = await _channel(db, settings.alert_tenant_slug) if settings.alert_tenant_slug else None

    sent = failed = dropped = 0
    for _ in range(BATCH):
        raw = await redis.lpop(OUTBOX)
        if raw is None:
            break
        alert: dict[str, Any] = json.loads(raw)
        if not recipients or target is None:
            dropped += 1  # already logged CRITICAL when raised
            log.error("platform_alert_undeliverable", key=alert["key"], reason="not configured")
            continue
        tenant_id, channel = target
        client: MetaClient = ctx["client_factory"](channel)
        ok = True
        for to in recipients:
            try:
                await client.send_template(
                    to,
                    settings.alert_template,
                    settings.alert_template_language,
                    [{"type": "body", "parameters": [{"type": "text", "text": alert["text"]}]}],
                )
            except (MetaAPIError, ChannelNotConfiguredError) as exc:
                ok = False
                log.error("platform_alert_send_failed", key=alert["key"], error=type(exc).__name__)
                continue
            async with db.tenant_session(tenant_id) as s:
                try:
                    cost = await price_message(
                        s, category="utility", recipient_wa_id=to, at=datetime.now(UTC)
                    )
                except PricingNotConfiguredError:
                    cost = None
                await record_usage(
                    s,
                    tenant_id,
                    msgs_out=1,
                    category="utility",
                    meta_cost_aed=cost if cost is not None else Decimal(0),
                )
        if ok:
            sent += 1
        elif time.time() - float(alert["at"]) < GIVE_UP_AFTER_S:
            alert["tries"] = int(alert.get("tries", 0)) + 1
            await redis.rpush(OUTBOX, json.dumps(alert))
            failed += 1
            break  # Meta is refusing: try the rest next minute
        else:
            dropped += 1
    return {"sent": sent, "failed": failed, "dropped": dropped}

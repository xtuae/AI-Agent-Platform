"""Hourly: is every active bot's webhook still pointed at us and delivering? (06 §5.3)

getWebhookInfo per active Telegram channel. A wrong URL, a recent delivery error or a growing
backlog is written to tenant_channels.webhook_error (the console and the Today screen show it) and
raises a platform alert. A healthy answer clears the error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import select

from api.alerts import raise_alert
from api.channels.registry import telegram_client_for
from api.channels.telegram.client import TelegramAPIError
from api.config import Settings
from api.core.logging import get_logger
from api.db.models import Tenant, TenantChannel
from api.db.session import Database

log = get_logger(__name__)

BACKLOG_ALERT: Final = 100
RECENT_ERROR: Final = timedelta(hours=1)


def problem_of(
    url: str, expected: str, pending: int, last_error_date: int | None, now: datetime
) -> str | None:
    if url != expected:
        return "webhook not set to this server"
    if last_error_date and now - datetime.fromtimestamp(last_error_date, tz=UTC) < RECENT_ERROR:
        return "Telegram reports delivery errors"
    if pending > BACKLOG_ALERT:
        return f"{pending} updates waiting at Telegram"
    return None


async def check_telegram_webhooks(ctx: dict[str, Any]) -> dict[str, int]:
    db: Database = ctx["db"]
    settings: Settings = ctx["settings"]
    http: httpx.AsyncClient = ctx["http"]
    if not settings.public_api_base_url:
        return {"checked": 0}
    base = settings.public_api_base_url.rstrip("/")
    async with db.platform_session() as s:
        rows = (
            await s.execute(
                select(TenantChannel, Tenant.name)
                .join(Tenant, Tenant.id == TenantChannel.tenant_id)
                .where(TenantChannel.kind == "telegram", TenantChannel.is_active.is_(True))
            )
        ).all()
    checked = broken = 0
    now = datetime.now(UTC)
    for channel, tenant_name in rows:
        checked += 1
        try:
            info = await telegram_client_for(channel, http, settings).get_webhook_info()
            problem = problem_of(
                info.url,
                f"{base}/webhook/telegram/{channel.channel_key}",
                info.pending_update_count,
                info.last_error_date,
                now,
            )
        except TelegramAPIError as exc:
            problem = f"getWebhookInfo failed ({exc.status_code})"
        async with db.platform_session() as s:
            row = await s.get(TenantChannel, channel.id, with_for_update=True)
            if row is not None:
                row.webhook_error = problem
        if problem:
            broken += 1
            log.error("telegram_webhook_unhealthy", channel_id=str(channel.id), problem=problem)
            await raise_alert(
                ctx["redis"],
                f"telegram_webhook:{channel.id}",
                f"Telegram bot of {tenant_name}: {problem}. Console → Setup → Re-register.",
            )
    return {"checked": checked, "broken": broken}

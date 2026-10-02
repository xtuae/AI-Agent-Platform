"""The watchdog (scheduler job, every minute): checks the conditions Phase 6 alerts on.

    webhook 5xx rate        > 5% of at least 10 webhooks over the last 5 minutes
    webhooks buffering      Postgres unreachable: webhooks are being parked in Redis
    ARQ queue depth         > 500 jobs waiting
    LLM failover            the primary provider failed and the router switched (or all failed)
    tenant message cap      a tenant reached 80% of its monthly cap (every 15 minutes)
    disk                    > 80% used on any configured path
    backup                  no successful backup in 26 hours (when backups are configured)

Quality-rating changes alert from the job that reads them (refresh_quality) and a failed backup
alerts from the backup itself, so both arrive as they happen.
"""

from __future__ import annotations

import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from api.alerts import raise_alert
from api.billing.ledger import month_usage
from api.billing.periods import month_start
from api.config import Settings
from api.core.logging import get_logger
from api.db.models import Tenant, TenantSettings
from api.db.session import Database
from api.webhooks import buffer

log = get_logger(__name__)

WEBHOOK_METRIC: Final = "metrics:webhook:"  # + epoch minute + ":total" / ":err"
LLM_FAILOVER: Final = "metrics:llm_failover"
LLM_DOWN: Final = "metrics:llm_all_down"
BACKUP_OK: Final = "backup:last_success"
QUEUE_LIMIT: Final = 500
ERR_RATE: Final = 0.05
MIN_WEBHOOKS: Final = 10
DISK_LIMIT: Final = 0.80
CAP_SHARE: Final = Decimal("0.8")
BACKUP_STALE_S: Final = 26 * 3600


async def count_webhook(redis: Redis, status: int) -> None:
    """Called for every webhook response (middleware). Never raises."""
    minute = WEBHOOK_METRIC + str(int(time.time()) // 60)
    with contextlib.suppress(RedisError, OSError):
        async with redis.pipeline(transaction=False) as p:
            p.incr(minute + ":total")
            p.expire(minute + ":total", 900)
            if status >= 500:
                p.incr(minute + ":err")
                p.expire(minute + ":err", 900)
            await p.execute()


async def note_llm(redis: Redis, event: str) -> None:
    with contextlib.suppress(RedisError, OSError):
        await redis.incr(LLM_FAILOVER if event == "failover" else LLM_DOWN)


async def _webhooks(redis: Redis, now: float) -> None:
    minute = int(now) // 60
    total = err = 0
    for m in range(minute - 4, minute + 1):
        total += int(await redis.get(f"{WEBHOOK_METRIC}{m}:total") or 0)
        err += int(await redis.get(f"{WEBHOOK_METRIC}{m}:err") or 0)
    if total >= MIN_WEBHOOKS and err / total > ERR_RATE:
        await raise_alert(
            redis,
            "webhook_5xx",
            f"Webhook errors: {err} of {total} in the last 5 minutes returned 5xx.",
        )


async def _buffering(redis: Redis) -> None:
    n = await buffer.depth(redis)
    if n:
        await raise_alert(
            redis, "webhook_buffering",
            f"Postgres unreachable: {n} webhook(s) parked in Redis. "
            "They replay automatically when it is back.",
            cooldown_s=900,
        )  # fmt: skip


async def _queue(redis: Redis, settings: Settings) -> None:
    depth = int(await redis.zcard(settings.arq_queue_name))
    if depth > QUEUE_LIMIT:
        await raise_alert(
            redis,
            "queue_depth",
            f"Job queue depth {depth} (limit {QUEUE_LIMIT}). Workers are falling behind.",
        )


async def _llm(redis: Redis) -> None:
    down = int(await redis.getdel(LLM_DOWN) or 0)
    failover = int(await redis.getdel(LLM_FAILOVER) or 0)
    if down:
        await raise_alert(
            redis,
            "llm_down",
            f"Both LLM providers failed {down} time(s) in the last minute. "
            "Customers get the holding message.",
            cooldown_s=600,
        )
    elif failover:
        await raise_alert(
            redis,
            "llm_failover",
            f"LLM failover: the primary provider failed {failover} time(s) in the last minute; "
            "OpenRouter is answering.",
        )


def disk_usage(paths: list[str]) -> list[tuple[str, float]]:
    out = []
    for p in paths:
        try:
            u = shutil.disk_usage(p)
        except OSError:
            continue
        out.append((p, u.used / u.total if u.total else 0.0))
    return out


async def _disk(redis: Redis, settings: Settings) -> None:
    for path, share in disk_usage(settings.disk_check_paths):
        if share > DISK_LIMIT:
            await raise_alert(
                redis, f"disk:{path}", f"Disk {path} is {share:.0%} full.", cooldown_s=6 * 3600
            )


async def _backup(redis: Redis, settings: Settings, now: float) -> None:
    if not settings.backup_bucket:
        return
    last = await redis.get(BACKUP_OK)
    if last is None or now - float(last) > BACKUP_STALE_S:
        await raise_alert(
            redis, "backup_stale", "No successful backup in the last 26 hours.", cooldown_s=6 * 3600
        )


async def tenant_caps(redis: Redis, db: Database, now: datetime) -> int:
    """Tenants at 80%+ of their monthly message cap. One alert per tenant per month."""
    async with db.platform_session() as s:
        rows = (
            await s.execute(
                select(Tenant.id, Tenant.name, TenantSettings.monthly_message_cap_aed)
                .join(TenantSettings, TenantSettings.tenant_id == Tenant.id)
                .where(
                    Tenant.status.in_(("trial", "active")),
                    TenantSettings.monthly_message_cap_aed.is_not(None),
                )
            )
        ).all()
    first = month_start(now.date())
    hit = 0
    for tid, name, cap in rows:
        if not cap:
            continue
        async with db.tenant_session(tid) as s:
            _, totals = await month_usage(s, first)
        if totals.meta_cost_aed >= cap * CAP_SHARE:
            hit += 1
            pct = totals.meta_cost_aed / cap * 100
            await raise_alert(
                redis, f"cap80:{tid}:{first:%Y-%m}",
                f"{name} is at {pct:.0f}% of its AED {cap} monthly message cap.",
                cooldown_s=40 * 86400,
            )  # fmt: skip
    return hit


async def watchdog(ctx: dict[str, Any]) -> dict[str, Any]:
    redis: Redis = ctx["redis"]
    settings: Settings = ctx["settings"]
    clock = ctx.get("clock", lambda: datetime.now(UTC))
    now: datetime = clock()
    checks: tuple[Callable[[], Awaitable[None]], ...] = (
        lambda: _webhooks(redis, now.timestamp()),
        lambda: _buffering(redis),
        lambda: _queue(redis, settings),
        lambda: _llm(redis),
        lambda: _disk(redis, settings),
        lambda: _backup(redis, settings, now.timestamp()),
    )
    failed = 0
    for check in checks:
        try:
            await check()
        except (RedisError, OSError) as exc:
            failed += 1
            log.error("watchdog_check_failed", error=type(exc).__name__)
    caps = await tenant_caps(redis, ctx["db"], now) if now.minute % 15 == 0 else None
    return {"failed_checks": failed, "caps": caps}

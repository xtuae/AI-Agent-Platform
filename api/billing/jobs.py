"""Worker job: keep Meta's statement figures current for every tenant (daily cron).

Pulls the current month, and the previous one during the first days of a month so the closed
month's figures settle and it is marked complete. A tenant whose pull fails is logged and
skipped; the next run tries again.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Final

from sqlalchemy import select

from api.billing.periods import add_months, month_start
from api.billing.statement import StatementError, pull
from api.core.logging import get_logger
from api.db.models import Tenant, TenantChannel
from api.db.session import Database
from api.meta.client import MetaAPIError

log = get_logger(__name__)

SETTLE_DAYS: Final = 5  # keep re-pulling last month until this day of the new month


def months_to_pull(today: date) -> list[date]:
    this = month_start(today)
    return [add_months(this, -1), this] if today.day <= SETTLE_DAYS else [this]


async def pull_meta_statements(ctx: dict[str, Any]) -> dict[str, int]:
    db: Database = ctx["db"]
    now: datetime = ctx.get("clock", lambda: datetime.now(UTC))()
    async with db.platform_session() as s:
        tenant_ids = (
            await s.scalars(
                select(Tenant.id)
                .join(TenantChannel, TenantChannel.tenant_id == Tenant.id)
                .where(
                    Tenant.status.in_(("trial", "active")),
                    TenantChannel.waba_id.is_not(None),
                    TenantChannel.access_token_encrypted.is_not(None),
                )
                .distinct()
            )
        ).all()
    done = failed = 0
    for tid in tenant_ids:
        for month in months_to_pull(now.date()):
            try:
                await pull(db, tid, month, ctx["client_factory"], now=now)
                done += 1
            except (StatementError, MetaAPIError) as exc:
                failed += 1
                log.error(
                    "meta_statement_pull_failed",
                    tenant_id=str(tid),
                    month=month.isoformat(),
                    error=type(exc).__name__,
                )
    log.info("meta_statements_pulled", pulled=done, failed=failed)
    return {"pulled": done, "failed": failed}

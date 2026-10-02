"""Scheduler job: the nightly encrypted backup."""

from __future__ import annotations

import time
from typing import Any

from api.alerts import raise_alert
from api.alerts.watchdog import BACKUP_OK
from api.config import Settings
from api.core.logging import get_logger
from api.ops.backup import BackupError, S3Storage, backup

log = get_logger(__name__)


def storage_from(settings: Settings) -> S3Storage:
    if not (
        settings.backup_bucket
        and settings.backup_endpoint_url
        and settings.backup_access_key_id
        and settings.backup_secret_access_key
    ):
        raise BackupError(
            "backup storage is not configured (BACKUP_BUCKET, BACKUP_ENDPOINT_URL, keys)"
        )
    return S3Storage(
        bucket=settings.backup_bucket,
        endpoint_url=settings.backup_endpoint_url,
        access_key=settings.backup_access_key_id,
        secret_key=settings.backup_secret_access_key.get_secret_value(),
        region=settings.backup_region,
    )


async def nightly_backup(ctx: dict[str, Any]) -> dict[str, Any]:
    settings: Settings = ctx["settings"]
    redis = ctx["redis"]
    if not settings.backup_bucket:
        log.warning("backup_skipped", reason="BACKUP_BUCKET not set")
        return {"status": "unconfigured"}
    try:
        if settings.backup_database_url is None or not settings.backup_age_recipient:
            raise BackupError("BACKUP_DATABASE_URL and BACKUP_AGE_RECIPIENT are required")
        result = await backup(
            dsn=settings.backup_database_url.get_secret_value(),
            recipient=settings.backup_age_recipient,
            storage=storage_from(settings),
            prefix=settings.backup_prefix,
            retention_days=settings.backup_retention_days,
        )
    except (BackupError, OSError) as exc:
        await raise_alert(
            redis, "backup_failed", f"Nightly backup FAILED: {str(exc)[:300]}", cooldown_s=3600
        )
        return {"status": "failed"}
    except Exception as exc:
        await raise_alert(
            redis, "backup_failed", f"Nightly backup FAILED: {type(exc).__name__}", cooldown_s=3600
        )
        raise
    await redis.set(BACKUP_OK, str(time.time()))
    return {"status": "ok", "key": result.key, "size": result.size, "rows": result.rows}

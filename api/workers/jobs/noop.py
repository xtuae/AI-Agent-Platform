"""Phase 0 proof-of-queue job. Replaced in spirit by real jobs from Phase 1."""

from __future__ import annotations

from typing import Any

from api.core.logging import get_logger

log = get_logger(__name__)


async def noop(ctx: dict[str, Any], marker: str) -> dict[str, Any]:
    db_ok = await ctx["db"].ping()
    log.info("noop_job", job_id=ctx.get("job_id"), marker=marker, db_ok=db_ok)
    return {"marker": marker, "db_ok": db_ok}

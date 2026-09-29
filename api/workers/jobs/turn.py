"""Inbound message job → one agent turn (api.agents.turn.TurnRunner)."""

from __future__ import annotations

import uuid
from typing import Any

from arq import Retry

from api.agents.turn import TurnRunner
from api.core.logging import get_logger

log = get_logger(__name__)

LOCK_RETRY_DEFER_S = 2
MAX_TRIES = 5


async def handle_inbound_message(
    ctx: dict[str, Any], tenant_id: str, message_id: str
) -> dict[str, Any]:
    runner: TurnRunner = ctx["turn_runner"]
    job_try = int(ctx.get("job_try") or 1)
    outcome = await runner.handle(uuid.UUID(tenant_id), uuid.UUID(message_id), job_try=job_try)
    if outcome == "locked":
        # Another turn for this conversation is running; try again shortly.
        raise Retry(defer=LOCK_RETRY_DEFER_S)
    if outcome == "llm_down":
        if job_try < MAX_TRIES:
            raise Retry(defer=ctx["settings"].llm_down_retry_defer_s * job_try)
        await runner.escalate_after_retries(uuid.UUID(tenant_id), uuid.UUID(message_id))
    log.info("turn_done", tenant_id=tenant_id, outcome=outcome, job_try=job_try)
    return {"status": outcome}

"""Rolling conversation summary, every N inbound messages, on the cheap model (01 §6.1 step 15).

Keeps the prompt small: the Support Agent sees the summary plus the last 10 turns, never the
full thread.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from api.agents import prompts
from api.agents.context import load_persona
from api.config import Settings
from api.core.logging import get_logger
from api.db.models import Conversation, Message
from api.db.session import Database
from api.llm.router import LLM, LLMUnavailableError
from api.metering import record_usage

log = get_logger(__name__)

SUMMARY_MAX_CHARS = 800


async def summarise_conversation(
    ctx: dict[str, Any], tenant_id: str, conversation_id: str
) -> dict[str, Any]:
    db: Database = ctx["db"]
    llm: LLM = ctx["llm"]
    settings: Settings = ctx["settings"]
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(conversation_id)
    persona = await load_persona(db, tid)

    async with db.tenant_session(tid) as s:
        conv = await s.get(Conversation, cid)
        if conv is None:
            return {"status": "missing"}
        rows = list(
            (
                await s.scalars(
                    select(Message)
                    .where(Message.conversation_id == cid)
                    .order_by(Message.created_at.desc())
                    .limit(24)
                )
            ).all()
        )
        previous = conv.summary or "(none)"
    rows.reverse()
    transcript = "\n".join(
        f"{'Customer' if m.direction == 'in' else 'Assistant'}: "
        f"{(m.transcript or m.body or f'[{m.msg_type}]')[:400]}"
        for m in rows
    )
    prompt = prompts.render(
        prompts.SUMMARY,
        business_name=persona.business_name,
        previous_summary=previous,
        transcript=transcript,
    )
    try:
        res = await llm.chat(
            provider=persona.llm_provider,
            model=settings.summary_model or persona.classify_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=250,
        )
    except LLMUnavailableError:
        log.warning("summary_skipped_llm_down", tenant_id=tenant_id)
        return {"status": "llm_down"}

    async with db.tenant_session(tid) as s:
        conv = await s.get(Conversation, cid)
        if conv is not None and res.content:
            conv.summary = res.content.strip()[:SUMMARY_MAX_CHARS]
        await record_usage(
            s,
            tid,
            llm_prompt_tokens=res.prompt_tokens,
            llm_completion_tokens=res.completion_tokens,
            llm_cost_usd=res.cost_usd,
        )
    log.info(
        "conversation_summarised",
        tenant_id=tenant_id,
        tokens=res.prompt_tokens + res.completion_tokens,
    )
    return {"status": "ok"}

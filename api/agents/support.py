"""The Support Agent conversation loop (02_agent_prompts §2-3).

    system prompt (+ rolling summary) + last 10 turns
      → model (tools enabled, temperature 0.3, max 300 tokens)
      → tool calls executed against Postgres — one tenant transaction per tool round
      → … at most `max_rounds` rounds …
      → draft reply → validator
            ok               → send
            regenerate-type  → ONE corrective regeneration → validate again → else hand over
            escalate-type    → hand over

"Hand over" = holding message instead of the draft + conversation escalated to a human. A tool
that raises (DB timeout, bug) also hands over — the model is never allowed to improvise an answer
around a failed lookup (scenario 20).

Transactions are per tool round, not per turn: holding one open across several LLM calls
(seconds each) would pin a pooled connection per active conversation.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from api.agents import prompts
from api.agents.canned import canned
from api.agents.context import Persona
from api.agents.tools import execute, tool_schemas, toolset
from api.agents.tools.base import Escalation, EscalationReason, ToolContext, escalate
from api.agents.validator import ValidationInput, ValidationResult, validate
from api.core.logging import get_logger
from api.db.session import Database
from api.llm.router import LLM, LLMRequestError, LLMResult

log = get_logger(__name__)

SUPPORT_TEMPERATURE = 0.3
SUPPORT_MAX_TOKENS = 300


@dataclass(frozen=True)
class SupportTurn:
    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    inbound_wamid: str | None
    customer_text: str
    language: str
    is_new_conversation: bool
    persona: Persona
    system_prompt: str
    customer_block: str
    summary: str | None
    history: list[dict[str, str]]
    previous_outbound: str | None
    now: datetime
    today: date
    other_tenant_names: tuple[str, ...] = ()


@dataclass
class SupportOutcome:
    text: str
    kind: Literal["model", "holding"]
    escalations: list[Escalation] = field(default_factory=list)
    llm_calls: list[LLMResult] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)


class _HandoverError(Exception):
    def __init__(self, reason: EscalationReason, summary: str) -> None:
        super().__init__(summary)
        self.reason = reason
        self.summary = summary


class SupportAgent:
    def __init__(self, db: Database, llm: LLM, *, max_rounds: int = 5) -> None:
        self._db = db
        self._llm = llm
        self._max_rounds = max_rounds

    async def reply(self, turn: SupportTurn) -> SupportOutcome:
        outcome = SupportOutcome(text="", kind="model")
        messages: list[dict[str, Any]] = [{"role": "system", "content": turn.system_prompt}]
        if turn.summary:
            messages.append(
                {
                    "role": "system",
                    "content": "Summary of the earlier conversation (context only):\n"
                    + turn.summary,
                }
            )
        messages.extend(turn.history)
        tool_results: list[dict[str, Any]] = []

        try:
            for attempt in (1, 2):
                draft = await self._generate(turn, messages, tool_results, outcome)
                result = self._validate(turn, draft, tool_results)
                if result.ok:
                    outcome.text = draft.strip()
                    return outcome
                outcome.validation.append(result.reasons())
                log.warning(
                    "reply_rejected",
                    tenant_id=str(turn.tenant_id),
                    attempt=attempt,
                    checks=[f.check for f in result.failures],
                )
                if result.must_escalate or attempt == 2:
                    raise _HandoverError(
                        "agent_unsure",
                        "Automatic handover: the drafted reply failed checks ("
                        + ", ".join(sorted({f.check for f in result.failures}))
                        + ").",
                    )
                messages.append({"role": "assistant", "content": draft})
                messages.append(
                    {
                        "role": "system",
                        "content": prompts.render(prompts.CORRECTIVE, reasons=result.reasons()),
                    }
                )
        except _HandoverError as h:
            return await self._hand_over(turn, outcome, h.reason, h.summary)
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------ generation with tools

    async def _generate(
        self,
        turn: SupportTurn,
        messages: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        outcome: SupportOutcome,
    ) -> str:
        persona = turn.persona
        for _ in range(self._max_rounds):
            try:
                res = await self._llm.chat(
                    provider=persona.llm_provider,
                    model=persona.chat_model,
                    messages=messages,
                    tools=tool_schemas(persona.enabled),
                    temperature=SUPPORT_TEMPERATURE,
                    max_tokens=SUPPORT_MAX_TOKENS,
                )
            except LLMRequestError as exc:
                log.error("support_llm_rejected", tenant_id=str(turn.tenant_id), error=str(exc))
                raise _HandoverError(
                    "agent_unsure",
                    "Automatic handover: the assistant could not process this message.",
                ) from None
            outcome.llm_calls.append(res)
            if not res.tool_calls:
                return res.content or ""
            messages.append(res.assistant_message())
            round_results = await self._run_tools(turn, res, outcome)
            for call, result in zip(res.tool_calls, round_results, strict=True):
                tool_results.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
        raise _HandoverError(
            "agent_unsure", f"Automatic handover: no answer after {self._max_rounds} tool rounds."
        )

    async def _run_tools(
        self, turn: SupportTurn, res: LLMResult, outcome: SupportOutcome
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        try:
            async with self._db.tenant_session(turn.tenant_id) as s:
                ctx = ToolContext(
                    session=s,
                    tenant_id=turn.tenant_id,
                    customer_id=turn.customer_id,
                    conversation_id=turn.conversation_id,
                    inbound_wamid=turn.inbound_wamid,
                    now=turn.now,
                    today=turn.today,
                    timezone=turn.persona.timezone,
                    feature_flags=turn.persona.feature_flags,
                    enabled=turn.persona.enabled,
                )
                for call in res.tool_calls:
                    outcome.tools_called.append(call.name)
                    results.append(await execute(ctx, call.name, call.arguments))
            outcome.escalations.extend(ctx.escalations)  # committed
        except Exception as exc:  # noqa: BLE001 — ANY tool failure hands over; the model must not improvise
            log.error(
                "tool_round_failed",
                tenant_id=str(turn.tenant_id),
                tools=[c.name for c in res.tool_calls],
                error=type(exc).__name__,
            )
            raise _HandoverError(
                "agent_unsure",
                "Automatic handover: a lookup failed while answering ("
                + ", ".join(c.name for c in res.tool_calls)
                + ").",
            ) from None
        return results

    # ------------------------------------------------------------ validation / handover

    def _validate(
        self, turn: SupportTurn, draft: str, tool_results: list[dict[str, Any]]
    ) -> ValidationResult:
        return validate(
            ValidationInput(
                reply=draft,
                tool_results=tool_results,
                language=turn.language,
                customer_text=turn.customer_text,
                trusted_context=turn.customer_block,
                previous_outbound=turn.previous_outbound,
                is_new_conversation=turn.is_new_conversation,
                disclosure_markers=turn.persona.disclosure_markers,
                other_tenant_names=turn.other_tenant_names,
                tool_names=tuple(toolset(turn.persona.enabled)),
            )
        )

    async def _hand_over(
        self, turn: SupportTurn, outcome: SupportOutcome, reason: EscalationReason, summary: str
    ) -> SupportOutcome:
        if not any(e.conversation_id == turn.conversation_id for e in outcome.escalations):
            async with self._db.tenant_session(turn.tenant_id) as s:
                outcome.escalations.append(
                    await escalate(s, turn.conversation_id, reason=reason, summary=summary)
                )
        outcome.kind = "holding"
        outcome.text = canned(
            "holding",
            turn.language,
            agent_name=turn.persona.agent_name,
            business_name=turn.persona.business_name,
            with_disclosure=turn.is_new_conversation,
        )
        log.info("support_handover", tenant_id=str(turn.tenant_id), reason=reason)
        return outcome

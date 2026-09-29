"""Tool plumbing: the execution context, result helpers, audit, and escalation.

Guarantees for every tool:
* Runs inside a tenant-scoped transaction (RLS) opened by the turn loop — one per tool round.
* Acts only on the customer and conversation of THIS turn. No tool accepts a customer id.
* Arguments are validated by a Pydantic model before any code runs.
* Every write is recorded in audit_log with actor 'agent'.
* Numbers returned are strings of Decimals, straight from the database.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import AuditLog, Conversation
from api.modules.registry import Enabled

EscalationReason = Literal[
    "complaint",
    "refund_or_billing",
    "angry_customer",
    "damaged_goods",
    "driver_issue",
    "out_of_scope",
    "agent_unsure",
    "customer_asked_for_human",
]
ACTOR: Final = "agent"


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


@dataclass(frozen=True)
class Escalation:
    conversation_id: uuid.UUID
    reason: EscalationReason
    summary: str
    urgency: Literal["normal", "high"]


@dataclass
class ToolContext:
    session: AsyncSession
    tenant_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    inbound_wamid: str | None
    now: datetime
    today: date  # in the tenant's timezone
    feature_flags: dict[str, Any] = field(default_factory=dict)
    enabled: Enabled = field(default_factory=Enabled)  # the tenant's modules and their configs
    escalations: list[Escalation] = field(default_factory=list)


ToolResult = dict[str, Any]
ToolFn = Callable[[ToolContext, Any], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema exactly as in 02_agent_prompts §3
    args_model: type[ToolArgs]
    run: ToolFn

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def error(code: str, **details: Any) -> ToolResult:
    return {"error": code, **details}


def money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(Decimal("0.01")))


async def audit(
    ctx: ToolContext,
    action: str,
    entity: str,
    entity_id: uuid.UUID | None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    ctx.session.add(
        AuditLog(
            actor=ACTOR,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before=before,
            after=after,
        )
    )


async def escalate(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    reason: EscalationReason,
    summary: str,
    urgency: Literal["normal", "high"] = "normal",
    actor: str = ACTOR,
) -> Escalation:
    """Hand the conversation to a human: state → awaiting_human (agent replies are suppressed
    until a person hands it back), and an audit record the dashboard reads for the reason."""
    conv = await session.get(Conversation, conversation_id, with_for_update=True)
    if conv is None:
        raise LookupError("conversation not found for tenant")
    before = conv.state
    conv.state = "awaiting_human"
    record = Escalation(conversation_id, reason, summary[:500], urgency)
    session.add(
        AuditLog(
            actor=actor,
            action="escalate",
            entity="conversation",
            entity_id=conversation_id,
            before={"state": before},
            after={
                "state": "awaiting_human",
                "reason": reason,
                "summary": record.summary,
                "urgency": urgency,
            },
        )
    )
    return record


async def escalate_ctx(
    ctx: ToolContext,
    *,
    reason: EscalationReason,
    summary: str,
    urgency: Literal["normal", "high"] = "normal",
) -> Escalation:
    record = await escalate(
        ctx.session, ctx.conversation_id, reason=reason, summary=summary, urgency=urgency
    )
    ctx.escalations.append(record)
    return record

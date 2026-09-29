"""escalate_to_human — hand the conversation to the tenant's team."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from api.agents.tools.base import (
    EscalationReason,
    Tool,
    ToolArgs,
    ToolContext,
    ToolResult,
    escalate_ctx,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "enum": [
                "complaint",
                "refund_or_billing",
                "angry_customer",
                "damaged_goods",
                "driver_issue",
                "out_of_scope",
                "agent_unsure",
                "customer_asked_for_human",
            ],
        },
        "summary": {
            "type": "string",
            "description": "One or two sentences the human needs to pick this up cold",
        },
        "urgency": {"type": "string", "enum": ["normal", "high"]},
    },
    "required": ["reason", "summary"],
}


class Args(ToolArgs):
    reason: EscalationReason = "agent_unsure"
    summary: str = Field(default="", max_length=1000)
    urgency: Literal["normal", "high"] = "normal"


async def run(ctx: ToolContext, args: Args) -> ToolResult:
    await escalate_ctx(
        ctx, reason=args.reason, summary=args.summary or args.reason, urgency=args.urgency
    )
    return {"escalated": True, "note": "A person from the team will reply in this chat."}


TOOL = Tool(
    name="escalate_to_human",
    description=(
        "Hand this conversation to the team. Call this for complaints, refund or billing disputes, "
        "angry customers, anything about damaged goods or a driver, and anything you are not "
        "confident answering."
    ),
    parameters=SCHEMA,
    args_model=Args,
    run=run,
)

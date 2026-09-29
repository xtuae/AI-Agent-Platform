"""Tool registry and executor.

`execute()` is the only entry point: it looks the tool up by name, validates arguments with the
tool's Pydantic model, and runs it. Unknown tools and invalid arguments come back to the model as
tool errors (it can correct itself); infrastructure failures (DB errors, timeouts) propagate so
the turn loop can send a holding message and escalate instead of letting the model improvise.
"""

from __future__ import annotations

import json
from typing import Any, Final

from pydantic import ValidationError

from api.agents.tools import (
    create_order,
    escalate_to_human,
    get_coupon_packages,
    get_customer_context,
    get_order_status,
    get_products,
    record_opt_in,
    record_opt_out,
    reschedule_delivery,
    update_customer,
)
from api.agents.tools.base import Tool, ToolContext, ToolResult, error
from api.core.logging import get_logger

log = get_logger(__name__)

TOOLS: Final[dict[str, Tool]] = {
    t.name: t
    for t in (
        get_customer_context.TOOL,
        get_products.TOOL,
        get_coupon_packages.TOOL,
        create_order.TOOL,
        get_order_status.TOOL,
        reschedule_delivery.TOOL,
        update_customer.TOOL,
        record_opt_in.TOOL,
        escalate_to_human.TOOL,
        record_opt_out.TOOL,
    )
}


def tool_schemas() -> list[dict[str, Any]]:
    return [t.schema() for t in TOOLS.values()]


async def execute(ctx: ToolContext, name: str, raw_arguments: str) -> ToolResult:
    tool = TOOLS.get(name)
    if tool is None:
        log.warning("tool_unknown", tool=name)
        return error("unknown_tool", available=sorted(TOOLS))
    try:
        payload = json.loads(raw_arguments or "{}")
        if not isinstance(payload, dict):
            raise ValueError("arguments must be an object")
        args = tool.args_model.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        count = exc.error_count() if isinstance(exc, ValidationError) else 1
        log.info("tool_invalid_arguments", tool=name, errors=count)
        return error("invalid_arguments", detail=_brief(exc))
    result = await tool.run(ctx, args)
    log.info("tool_called", tool=name, ok="error" not in result)
    return result


def _brief(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
    return str(exc)[:200]

"""Tool registry and executor.

Core tools every tenant has (profile, opt-in/out, escalation) plus the tools of the tenant's
enabled modules. `toolset(enabled)` is what the model is offered; `execute()` refuses any tool
outside that set, so a model that names a tool from a module the tenant does not have gets an
error back rather than running it.

`execute()` looks the tool up by name, validates arguments with the tool's Pydantic model, and
runs it. Unknown tools and invalid arguments come back to the model as tool errors (it can correct
itself); infrastructure failures (DB errors, timeouts) propagate so the turn loop can send a
holding message and escalate instead of letting the model improvise.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from functools import lru_cache
from typing import Any, Final

from pydantic import ValidationError

from api.agents.tools import (
    escalate_to_human,
    get_customer_context,
    record_opt_in,
    record_opt_out,
    update_customer,
)
from api.agents.tools.base import Tool, ToolContext, ToolResult, error
from api.core.logging import get_logger
from api.modules import registry
from api.modules.registry import Enabled

log = get_logger(__name__)

CORE_TOOLS: Final[tuple[Tool, ...]] = (
    get_customer_context.TOOL,
    update_customer.TOOL,
    record_opt_in.TOOL,
    escalate_to_human.TOOL,
    record_opt_out.TOOL,
)


@lru_cache(maxsize=64)
def _toolset(keys: tuple[str, ...]) -> dict[str, Tool]:
    modules = registry.resolve(keys)
    tools: dict[str, Tool] = {"get_customer_context": get_customer_context.for_modules(modules)}
    for m in modules:
        for t in m.tools:
            if t.name in tools or any(t.name == c.name for c in CORE_TOOLS):
                raise registry.ModuleError(f"tool {t.name!r} is defined twice")
            tools[t.name] = t
    for t in CORE_TOOLS[1:]:
        tools[t.name] = t
    # parameters other modules add to a tool (e.g. coupons → create_order.use_coupon_book)
    for m in modules:
        for tool_name, props in m.tool_params.items():
            if tool_name in tools:
                params = copy.deepcopy(tools[tool_name].parameters)
                params.setdefault("properties", {}).update(props)
                tools[tool_name] = replace(tools[tool_name], parameters=params)
    return tools


def toolset(enabled: Enabled) -> dict[str, Tool]:
    return _toolset(enabled.keys)


def tool_schemas(enabled: Enabled) -> list[dict[str, Any]]:
    return [t.schema() for t in toolset(enabled).values()]


async def execute(ctx: ToolContext, name: str, raw_arguments: str) -> ToolResult:
    tools = toolset(ctx.enabled)
    tool = tools.get(name)
    if tool is None:
        log.warning("tool_unknown", tool=name)
        return error("unknown_tool", available=sorted(tools))
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

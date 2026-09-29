"""The module contract.

A module is a capability a tenant can have switched on (catalog, orders, coupons, appointments…).
It lives in its own package, `api/modules/<key>/`, and declares everything it adds in one
`Module` manifest in `module.py`. Core code never imports a module directly; it asks the
registry for the modules enabled for a tenant and uses what they contribute:

* agent — tools, lines and sections of the support prompt, classifier intents, lines of the
  customer block, sections of get_customer_context
* API — a router mounted at /api/v1/m/<key>/, answering only for tenants with the module on
* dashboard — Today data and a contact panel (the React side has a matching registry)

Tables a module owns live in its package (`models.py`) and arrive through the normal migration
chain; every deployment has every table, and turning a module on for a tenant is a settings row,
not a deploy.

Prompt contributions are template SOURCE (Jinja) spliced into the core template before rendering,
ordered by `weight`. That keeps the spec's text in templates, and lets a test prove the water
preset reproduces 02_agent_prompts.md §2 byte for byte.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from api.agents.tools.base import Tool, ToolContext
    from api.db.models import Customer


class ModuleConfig(BaseModel):
    """Base for a module's per-tenant settings (stored in tenant_modules.config)."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Line:
    """One weighted piece of template source for a prompt slot."""

    weight: int
    text: str


@dataclass(frozen=True)
class Intent:
    weight: int
    name: str
    description: str


@dataclass(frozen=True)
class CustomerLines:
    """A module's contribution to the '## WHAT YOU KNOW ABOUT THIS CUSTOMER' block."""

    known: bool  # does this module's data make the customer a known one?
    lines: tuple[Line, ...] = ()


CustomerBlockHook = Callable[[AsyncSession, "Customer", date], Awaitable[CustomerLines]]
ContextHook = Callable[["ToolContext"], Awaitable[dict[str, Any]]]
TodayHook = Callable[[AsyncSession, "TodayScope"], Awaitable[dict[str, Any]]]
PanelHook = Callable[[AsyncSession, uuid.UUID, date], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class TodayScope:
    tenant_id: uuid.UUID
    timezone: str
    today: date


@dataclass(frozen=True)
class Module:
    key: str  # stable id; also the URL segment /api/v1/m/<key>/
    name: str
    description: str
    requires: tuple[str, ...] = ()
    config_model: type[ModuleConfig] = ModuleConfig

    # --- agent
    tools: tuple[Tool, ...] = ()
    # support prompt slots (template source, weighted across modules)
    capabilities: tuple[Line, ...] = ()  # "## WHAT YOU CAN DO" bullets
    facts: tuple[Line, ...] = ()  # rule 1: "NEVER state <facts> that did not come from a tool"
    rules: tuple[Line, ...] = ()  # extra ABSOLUTE RULES ("#. " auto-numbered)
    sections: tuple[Line, ...] = ()  # whole "## SECTION" blocks (owned by this module)
    steps: dict[str, tuple[Line, ...]] = field(default_factory=dict)  # steps into other sections
    business_facts: tuple[Line, ...] = ()  # lines under "## BUSINESS FACTS"
    new_contact: Line | None = None  # replaces the core new-contact instruction (highest weight)
    # get_customer_context description parts
    context_fetches: tuple[Line, ...] = ()  # "Fetch this customer's profile, <...>"
    context_topics: tuple[Line, ...] = ()  # "…any question about <...>"
    # classifier
    intents: tuple[Intent, ...] = ()
    intent_rules: tuple[Line, ...] = ()
    # runtime hooks
    customer_block: CustomerBlockHook | None = None
    customer_context: ContextHook | None = None

    # extra JSON-schema properties this module adds to another module's tool, by tool name
    # (e.g. coupons adds use_coupon_book to create_order)
    tool_params: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- extension points other modules call (typed in the owning module; Any here so the
    # contract does not import every module)
    order_redeem: Callable[..., Awaitable[Any]] | None = None  # orders.service.RedeemHook
    order_cancelled: Callable[..., Awaitable[int]] | None = None  # orders.service.CancelHook

    # --- API + dashboard
    router: APIRouter | None = None
    today: TodayHook | None = None
    contact_panel: PanelHook | None = None

    def validate_config(self, raw: dict[str, Any] | None) -> ModuleConfig:
        return self.config_model.model_validate(raw or {})

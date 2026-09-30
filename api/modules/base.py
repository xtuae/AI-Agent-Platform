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
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement
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
class Subject:
    """What an appointment is about, owned by another module (e.g. a listing for a viewing)."""

    module: str
    id: uuid.UUID
    label: str
    location_note: str | None = None


class SubjectError(Exception):
    """A subject parameter was given but cannot be booked (unknown, unavailable, …)."""

    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


# (session, the booking's extra parameters) → the subject, None when this module's parameter is
# absent, or raise SubjectError
SubjectHook = Callable[[AsyncSession, dict[str, Any]], Awaitable[Subject | None]]


@dataclass(frozen=True)
class SegmentScope:
    now: datetime
    today: date  # in the tenant's timezone


@dataclass(frozen=True)
class SegmentField:
    """One key a campaign segment definition may use (02 §4.3), e.g. last_order_before_days.

    `clause` receives the value, already validated against `kind` / `minimum` / `maximum` /
    `choices`, and returns a boolean SQL condition over `customers` (correlated subqueries are
    fine). The compiler ANDs every clause UNDER its own opt-in filter, so no field can widen a
    segment beyond opted-in customers."""

    name: str
    label: str
    kind: Literal["int", "text", "text_list"]
    help: str
    clause: Callable[[Any, SegmentScope], ColumnElement[bool]]
    minimum: int = 0
    maximum: int = 100_000
    choices: tuple[str, ...] = ()


# (session, customer id, now, the module's config) → a block of context for the support prompt when
# the customer's message is a reply to something the module sent (campaigns: 02 §4.2), or None
ReplyContextHook = Callable[
    [AsyncSession, uuid.UUID, datetime, "ModuleConfig | None"], Awaitable[str | None]
]

# (session, campaign id, attribution window in days) → figures for the campaign's results
AttributionHook = Callable[[AsyncSession, uuid.UUID, int], Awaitable[dict[str, Any]]]


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
    appointment_subject: SubjectHook | None = None  # an appointment can be about my record
    segment_fields: tuple[SegmentField, ...] = ()  # keys campaign segments may filter on
    campaign_attribution: AttributionHook | None = None  # what a campaign led to (orders…)
    reply_context: ReplyContextHook | None = None  # "this is a reply to …" for the support prompt

    # --- API + dashboard
    router: APIRouter | None = None
    today: TodayHook | None = None
    contact_panel: PanelHook | None = None

    def validate_config(self, raw: dict[str, Any] | None) -> ModuleConfig:
        return self.config_model.model_validate(raw or {})

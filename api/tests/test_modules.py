"""Plug-and-play modules: the registry, per-tenant enabling, gating, and the contract every module
must meet. A new module gets all of this for free by being listed in registry.MODULE_PATHS."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest
from sqlalchemy import update

from api.agents.context import load_persona, render_customer_block
from api.agents.prompts import compose
from api.agents.text import estimate_tokens
from api.agents.tools import CORE_TOOLS, execute, toolset
from api.agents.tools.base import ToolContext
from api.config import Settings
from api.db.models import Customer, TenantModule
from api.db.session import Database
from api.main import create_app
from api.modules import registry
from api.modules.registry import Enabled, ModuleError, enabled_of
from api.tests.conftest import WATER_PRESET, DashHarness, make_customer, make_tenant

ALL = tuple(registry.all_modules())
CORE_TOOL_NAMES = {t.name for t in CORE_TOOLS}
WATER_WORDS = re.compile(r"coupon|bottle|deliver|order", re.I)


# ---------------------------------------------------------------- registry


def test_presets_resolve_and_dependencies_are_enforced() -> None:
    assert registry.expand(["water_delivery"]) == ("catalog", "orders", "coupons", "campaigns")
    assert [m.key for m in registry.resolve(["coupons", "orders", "catalog"])] == [
        "catalog",
        "orders",
        "coupons",
    ]  # canonical order, whatever order they were asked for in
    with pytest.raises(ModuleError, match="requires"):
        registry.resolve(["coupons"])
    with pytest.raises(ModuleError, match="unknown"):
        registry.resolve(["time_machine"])
    assert registry.closure(["coupons"]) == ("catalog", "orders", "coupons")


@pytest.mark.parametrize("key", ALL)
def test_module_contract(key: str) -> None:
    """Every module: dependencies listed first, tool names unique, prompt pieces render with the
    standard variables only, config has defaults."""
    module = registry.get(key)
    order = list(registry.all_modules())
    assert all(order.index(r) < order.index(key) for r in module.requires)
    assert re.fullmatch(r"[a-z][a-z0-9_]*", key)
    module.config_model()  # defaults must be valid: enabling a module needs no config
    tools = toolset(enabled_of(registry.closure([key])))
    for tool in module.tools:
        assert tool.name in tools
        assert tool.name not in CORE_TOOL_NAMES
    lines = [
        *module.capabilities,
        *module.facts,
        *module.rules,
        *module.sections,
        *module.business_facts,
        *(line for group in module.steps.values() for line in group),
    ]
    for line in lines:
        assert "\n\n" not in line.text  # a piece never ends a section on its own
    for rule in (*module.rules, *(line for g in module.steps.values() for line in g)):
        assert rule.text.startswith("#. "), "numbered items are auto-numbered"


def _vars() -> dict[str, Any]:
    return {
        "agent_name": "Noor",
        "business_name": "Test Co",
        "business_description": "test business",
        "service_areas": "Dubai",
        "cross_sell_category": "snack",
        "customer_block": "Name: x",
        "business_hours": "9-5",
        "knowledge_block": "",
        "today": "2026-09-29",
        "timezone": "Asia/Dubai",
    }


@pytest.mark.parametrize(
    "keys",
    [
        (),
        ("catalog",),
        ("catalog", "orders"),
        ("catalog", "orders", "coupons"),
        ("appointments",),
        ("listings",),
        ("appointments", "listings"),
        ALL,  # every module at once still fits the prompt budget
    ],
)
def test_every_module_combination_renders_a_complete_prompt(keys: tuple[str, ...]) -> None:
    prompt = compose.render_support(keys, **_vars())  # StrictUndefined: a stray var raises
    assert "<<" not in prompt
    assert "#. " not in prompt
    assert estimate_tokens(prompt) < Settings.model_fields["system_prompt_token_cap"].default
    # rules are numbered 1..n with no gaps
    rules = prompt.split("## ABSOLUTE RULES")[1].split("\n## ")[0]
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", rules, re.M)]
    assert numbers == list(range(1, len(numbers) + 1))
    classifier = compose.render_classifier(keys, message_text="hi")
    assert "<<" not in classifier


def test_a_core_only_tenant_hears_nothing_about_water() -> None:
    prompt = compose.render_support((), **_vars())
    assert not WATER_WORDS.search(prompt.replace("anything a customer asks", ""))
    assert "Answer questions about Test Co" in prompt
    classifier = compose.render_classifier((), message_text="hi")
    assert not re.search(r"^(order|balance|delivery|price) ", classifier, re.M)
    assert not WATER_WORDS.search(compose.context_description(()))


def test_tools_follow_the_modules() -> None:
    assert set(toolset(Enabled())) == CORE_TOOL_NAMES
    catalog_only = toolset(enabled_of(["catalog"]))
    assert "get_products" in catalog_only
    assert "create_order" not in catalog_only
    water = toolset(enabled_of(WATER_PRESET))
    assert "use_coupon_book" in water["create_order"].parameters["properties"]
    no_coupons = toolset(enabled_of(["catalog", "orders"]))
    assert "use_coupon_book" not in no_coupons["create_order"].parameters["properties"]


# ---------------------------------------------------------------- per-tenant enabling


async def test_enabled_for_reads_the_tenant_and_drops_orphans(db: Database) -> None:
    tenant = await make_tenant(db, "mods", modules=("orders", "coupons"))  # catalog missing
    async with db.platform_session() as s:
        enabled = await registry.enabled_for(s, tenant)
    # orders needs catalog, coupons needs orders: both are off rather than half-working
    assert enabled.keys == ()

    other = await make_tenant(db, "mods2", modules=("catalog",))
    async with db.platform_session() as s:
        assert (await registry.enabled_for(s, other)).keys == ("catalog",)
        assert (await registry.enabled_for(s, tenant)).keys == ()  # never another tenant's rows


async def test_bad_stored_config_falls_back_to_defaults(db: Database) -> None:
    tenant = await make_tenant(db, "cfg")
    async with db.platform_session() as s:
        await s.execute(
            update(TenantModule)
            .where(TenantModule.tenant_id == tenant, TenantModule.module_key == "orders")
            .values(config={"lead_days": "soon", "surprise": 1})
        )
    async with db.platform_session() as s:
        enabled = await registry.enabled_for(s, tenant)
    assert enabled.has("orders")
    assert getattr(enabled.config("orders"), "lead_days", "x") is None


async def test_orders_config_sets_the_delivery_lead_time(db: Database) -> None:
    tenant = await make_tenant(db, "lead")
    async with db.platform_session() as s:
        await s.execute(
            update(TenantModule)
            .where(TenantModule.tenant_id == tenant, TenantModule.module_key == "orders")
            .values(config={"lead_days": 2})
        )
    persona = await load_persona(db, tenant)
    assert getattr(persona.enabled.config("orders"), "lead_days", None) == 2


async def test_core_only_tenant_gets_the_generic_new_contact_block(db: Database) -> None:
    tenant = await make_tenant(db, "core", modules=())
    customer_id = await make_customer(db, tenant, "971500000321")
    async with db.tenant_session(tenant) as s:
        customer = await s.get(Customer, customer_id)
        assert customer is not None
        block = await render_customer_block(s, customer, customer.opt_in_at or _today(), Enabled())
    assert block == compose.DEFAULT_NEW_CONTACT
    assert "delivery" not in block


def _today() -> Any:
    from datetime import date

    return date(2026, 9, 29)


async def test_a_disabled_modules_tool_cannot_be_called(db: Database) -> None:
    """The model might name a tool it saw elsewhere; execute() refuses anything outside the
    tenant's toolset."""
    tenant = await make_tenant(db, "notools", modules=())
    customer = await make_customer(db, tenant, "971500000322")
    async with db.tenant_session(tenant) as s:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        ctx = ToolContext(
            session=s,
            tenant_id=tenant,
            customer_id=customer,
            conversation_id=uuid.uuid4(),
            inbound_wamid=None,
            now=now,
            today=now.date(),
            enabled=Enabled(),
        )
        result = await execute(ctx, "create_order", json.dumps({"items": [], "area": "x"}))
    assert result["error"] == "unknown_tool"
    assert "create_order" not in result["available"]


# ---------------------------------------------------------------- API gating


def _module_routes() -> list[tuple[str, str]]:
    paths: dict[str, dict[str, Any]] = create_app().openapi()["paths"]
    return sorted(
        (method.upper(), path)
        for path, ops in paths.items()
        if path.startswith("/api/v1/m/")
        for method in ops
        if method in {"get", "post", "put", "patch", "delete"}
    )


def test_every_module_route_is_under_its_key() -> None:
    routes = _module_routes()
    assert routes, "modules mount their routers"
    for _, path in routes:
        assert path.split("/")[4] in registry.all_modules()


@pytest.mark.parametrize("route", _module_routes(), ids=lambda r: f"{r[0]} {r[1]}")
async def test_module_routes_are_404_for_a_tenant_without_the_module(
    dash: DashHarness, route: tuple[str, str]
) -> None:
    method, path = route
    tenant = await make_tenant(dash.db, "nomod", modules=())
    headers = await dash.login(tenant, "admin")
    url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
    r = await dash.client.request(method, url, headers=headers, json={})
    assert r.status_code == 404, (method, path, r.status_code)
    assert r.json() == {"detail": "Not Found"}  # indistinguishable from a route that isn't there
    # and still 401 without a token: gating never runs before authentication
    assert (await dash.client.request(method, url, json={})).status_code == 401


async def test_core_screens_work_with_no_modules(dash: DashHarness) -> None:
    tenant = await make_tenant(dash.db, "bare", modules=())
    headers = await dash.login(tenant, "admin")
    today = (await dash.client.get("/api/v1/today", headers=headers)).json()
    assert today["modules"] == {}
    customer = await make_customer(dash.db, tenant, "971500000323")
    contact = (await dash.client.get(f"/api/v1/contacts/{customer}", headers=headers)).json()
    assert contact["modules"] == {}
    me = (await dash.client.get("/api/v1/auth/me", headers=headers)).json()
    assert me["tenant"]["modules"] == []


# ---------------------------------------------------------------- admin (HMH Labz only)


async def test_admin_enable_disable_and_config(db: Database) -> None:
    from api.modules import admin

    tenant = await make_tenant(db, "adm", modules=())
    async with db.platform_session() as s:
        with pytest.raises(ModuleError, match="requires"):
            await admin.enable(s, tenant, ["orders"])  # catalog not named, not enabled
    async with db.platform_session() as s:
        assert await admin.enable(s, tenant, ["water_delivery"]) == (
            "catalog",
            "orders",
            "coupons",
            "campaigns",
        )
    async with db.platform_session() as s:
        with pytest.raises(ModuleError, match="still needs"):
            await admin.disable(s, tenant, ["orders"])  # coupons needs it
    async with db.platform_session() as s:
        with pytest.raises(ValueError, match="lead_days"):
            await admin.enable(s, tenant, [], configs={"orders": {"lead_days": 99}})
    async with db.platform_session() as s:
        await admin.enable(s, tenant, [], configs={"orders": {"lead_days": 1}})
        assert await admin.disable(s, tenant, ["coupons"]) == ("catalog", "orders", "campaigns")
    async with db.platform_session() as s:
        enabled = await registry.enabled_for(s, tenant)
    assert getattr(enabled.config("orders"), "lead_days", None) == 1


def test_cli_lists_modules_and_presets(capsys: pytest.CaptureFixture[str]) -> None:
    from api.scripts import modules as cli

    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "water_delivery" in out
    assert "coupons" in out


def test_dashboard_modules_exist_in_the_api() -> None:
    """The React registry (dashboard/src/modules) may only name modules the API has, with the
    same keys — a typo there would silently hide a module's screens."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "modules"
    keys = {
        m.group(1)
        for p in root.glob("*/index.tsx")
        for m in re.finditer(r'key:\s*"([a-z_]+)"', p.read_text())
    }
    assert keys, "dashboard module registry not found"
    assert keys <= set(registry.all_modules()), keys - set(registry.all_modules())

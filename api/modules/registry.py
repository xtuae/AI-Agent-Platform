"""Which modules exist, which presets bundle them, and which are enabled for a tenant.

Modules are listed here by import path and loaded lazily, so importing the registry never pulls
in a module's routes or tools (and never creates an import cycle with api.db.models). Order in
MODULE_PATHS is the canonical order: a dependency always comes before what needs it.

Enabling is HMH Labz's decision (a tenant_modules row, written by `python -m
api.scripts.modules`); a client admin can change a module's config, never whether they have it.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.logging import get_logger
from api.modules.base import Module, ModuleConfig

log = get_logger(__name__)

MODULE_PATHS: Final[tuple[str, ...]] = (
    "api.modules.catalog.module",
    "api.modules.orders.module",
    "api.modules.coupons.module",
    "api.modules.appointments.module",
    "api.modules.listings.module",
)

# An industry is a bundle of capability modules, plus the configs that industry starts with.
# (Leads for real estate, intake and matters for law firms, come as those modules are built.)
PRESETS: Final[dict[str, tuple[str, ...]]] = {
    "water_delivery": ("catalog", "orders", "coupons"),
    "real_estate": ("listings", "appointments"),
    "law_firm": ("appointments",),
}
PRESET_CONFIGS: Final[dict[str, dict[str, dict[str, Any]]]] = {
    # a person confirms each consultation before it is final (04 §8 decision 2)
    "law_firm": {"appointments": {"require_team_confirmation": True}},
}


class ModuleError(ValueError):
    pass


@cache
def all_modules() -> dict[str, Module]:
    """Every installed module, in canonical order."""
    out: dict[str, Module] = {}
    for path in MODULE_PATHS:
        module: Module = importlib.import_module(path).MODULE
        if module.key in out:
            raise ModuleError(f"duplicate module key {module.key!r}")
        missing = [r for r in module.requires if r not in out]
        if missing:
            raise ModuleError(f"{module.key} requires {missing}, which must be listed before it")
        out[module.key] = module
    return out


def get(key: str) -> Module:
    try:
        return all_modules()[key]
    except KeyError as exc:
        raise ModuleError(f"unknown module {key!r}") from exc


def resolve(keys: Iterable[str]) -> tuple[Module, ...]:
    """Modules for `keys`, in canonical order. Raises if a dependency is missing."""
    wanted = set(keys)
    known = all_modules()
    unknown = sorted(wanted - set(known))
    if unknown:
        raise ModuleError(f"unknown modules {unknown}")
    for key in wanted:
        missing = [r for r in known[key].requires if r not in wanted]
        if missing:
            raise ModuleError(f"{key} requires {missing}")
    return tuple(m for k, m in known.items() if k in wanted)


def expand(names: Iterable[str]) -> tuple[str, ...]:
    """Preset names and module keys → module keys (presets expanded, order preserved)."""
    out: list[str] = []
    for name in names:
        for key in PRESETS.get(name, (name,)):
            if key not in out:
                out.append(key)
    return tuple(out)


def preset_configs(names: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The starting configs of the presets among `names`, by module key."""
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        for key, cfg in PRESET_CONFIGS.get(name, {}).items():
            out[key] = {**out.get(key, {}), **cfg}
    return out


@dataclass(frozen=True)
class Enabled:
    """The modules a tenant has, with their validated configs. Frozen and hashable by keys."""

    modules: tuple[Module, ...] = ()
    configs: dict[str, ModuleConfig] = field(default_factory=dict, compare=False, hash=False)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(m.key for m in self.modules)

    def has(self, key: str) -> bool:
        return key in self.keys

    def config(self, key: str) -> ModuleConfig | None:
        return self.configs.get(key)


def closure(keys: Iterable[str]) -> tuple[str, ...]:
    """`keys` plus everything they need, transitively, in canonical order."""
    known = all_modules()
    out: set[str] = set()
    stack = list(keys)
    while stack:
        key = stack.pop()
        if key in out:
            continue
        if key not in known:
            raise ModuleError(f"unknown module {key!r}")
        out.add(key)
        stack.extend(known[key].requires)
    return tuple(k for k in known if k in out)


def enabled_of(names: Iterable[str]) -> Enabled:
    """Modules (or presets) with default configs — for scripts and tests."""
    modules = resolve(expand(names))
    return Enabled(modules=modules, configs={m.key: m.config_model() for m in modules})


async def enabled_for(s: AsyncSession, tenant_id: uuid.UUID) -> Enabled:
    """Load a tenant's enabled modules from tenant_modules (a platform table: filter by tenant)."""
    from api.db.models import TenantModule

    rows = (
        await s.scalars(
            select(TenantModule).where(
                TenantModule.tenant_id == tenant_id, TenantModule.enabled.is_(True)
            )
        )
    ).all()
    known = all_modules()
    raw: dict[str, dict[str, Any]] = {
        r.module_key: r.config or {} for r in rows if r.module_key in known
    }
    # A dependency that was switched off disables what needs it, rather than failing the tenant.
    keys = set(raw)
    changed = True
    while changed:
        changed = False
        for k in list(keys):
            if any(r not in keys for r in known[k].requires):
                keys.discard(k)
                changed = True
    modules = tuple(m for k, m in known.items() if k in keys)
    configs: dict[str, ModuleConfig] = {}
    for m in modules:
        try:
            configs[m.key] = m.validate_config(raw[m.key])
        except ValidationError as exc:
            # A bad stored config must not take the tenant's agent down: run on defaults, loudly.
            log.error(
                "module_config_invalid",
                tenant_id=str(tenant_id),
                module=m.key,
                errors=exc.error_count(),
            )
            configs[m.key] = m.config_model()
    return Enabled(modules=modules, configs=configs)

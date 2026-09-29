"""Switching modules on and off for a tenant. HMH Labz only: used by api.scripts.modules and the
tenant seed, never exposed to a client's dashboard (a client admin may edit a module's config
through that module's own settings, not which modules they have)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Tenant, TenantModule
from api.modules import registry
from api.modules.registry import ModuleError


async def enable(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    names: Iterable[str],
    *,
    configs: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, ...]:
    """Enable modules and presets for a tenant. Dependencies must be named too (or be enabled
    already) — nothing is switched on silently. Configs are validated before anything is written.
    A preset brings its starting configs (registry.PRESET_CONFIGS) for modules it switches on for
    the first time; a module already configured keeps its config. `configs` replaces a config.
    Returns the tenant's enabled keys afterwards."""
    if await s.get(Tenant, tenant_id) is None:
        raise ModuleError("unknown tenant")
    names = tuple(names)
    keys = registry.expand(names)
    configs = configs or {}
    preset = registry.preset_configs(names)
    for key, raw in preset.items():
        registry.get(key).validate_config(raw)
    current = (await registry.enabled_for(s, tenant_id)).keys
    registry.resolve({*current, *keys})  # raises if a dependency is missing
    unknown_cfg = sorted(set(configs) - set(keys) - set(current))
    if unknown_cfg:
        raise ModuleError(f"config given for modules that are not enabled: {unknown_cfg}")
    for key, raw in configs.items():
        registry.get(key).validate_config(raw)
    for key in keys:
        values: dict[str, Any] = {"tenant_id": tenant_id, "module_key": key, "enabled": True}
        update: dict[str, Any] = {"enabled": True, "updated_at": func.now()}
        if key in configs:
            values["config"] = configs[key]
            update["config"] = configs[key]
        elif key in preset:  # first time only: on conflict the stored config stays
            values["config"] = preset[key]
        await s.execute(
            insert(TenantModule)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[TenantModule.tenant_id, TenantModule.module_key], set_=update
            )
        )
    for key, raw in configs.items():
        if key not in keys:
            row = await s.get(TenantModule, (tenant_id, key))
            if row is not None:
                row.config = raw
    return (await registry.enabled_for(s, tenant_id)).keys


async def disable(s: AsyncSession, tenant_id: uuid.UUID, keys: Iterable[str]) -> tuple[str, ...]:
    """Switch modules off. Refuses if an enabled module still needs one of them. Data is kept:
    switching back on restores everything."""
    keys = tuple(keys)
    current = (await registry.enabled_for(s, tenant_id)).keys
    remaining = [k for k in current if k not in keys]
    for k in remaining:
        needs = [r for r in registry.get(k).requires if r in keys]
        if needs:
            raise ModuleError(f"{k} still needs {needs}; disable {k} first")
    rows = (
        await s.scalars(
            select(TenantModule).where(
                TenantModule.tenant_id == tenant_id, TenantModule.module_key.in_(keys)
            )
        )
    ).all()
    for row in rows:
        row.enabled = False
    await s.flush()
    return (await registry.enabled_for(s, tenant_id)).keys

"""Manage a tenant's modules (HMH Labz staff only).

    python -m api.scripts.modules list                        # every module and preset
    python -m api.scripts.modules show    --slug aquamena
    python -m api.scripts.modules enable  --slug aquamena water_delivery
    python -m api.scripts.modules enable  --slug acme catalog orders
    python -m api.scripts.modules disable --slug acme orders
    python -m api.scripts.modules config  --slug aquamena orders '{"lead_days": 1}'
    python -m api.scripts.modules label   --slug acme Clients

Runs as the app role against DATABASE_URL. Disabling keeps the module's data; enabling again
brings it back. The dashboard and agent pick changes up on the next request / message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from api.config import get_settings
from api.db.models import Tenant, TenantSettings
from api.db.session import Database
from api.modules import admin, registry
from api.modules.registry import ModuleError


def _print_catalog() -> None:
    print("Modules:")
    for key, m in registry.all_modules().items():
        needs = f"  (needs {', '.join(m.requires)})" if m.requires else ""
        print(f"  {key:<12} {m.name}{needs}\n  {'':<12} {m.description}")
    print("Presets:")
    for name, keys in registry.PRESETS.items():
        print(f"  {name:<16} {', '.join(keys)}")


async def _tenant_id(db: Database, slug: str) -> uuid.UUID:
    async with db.platform_session() as s:
        tid = await s.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tid is None:
        raise ModuleError(f"no tenant with slug {slug!r}")
    return tid


async def _run(args: argparse.Namespace) -> None:
    if args.cmd == "list":
        _print_catalog()
        return
    db = Database(get_settings())
    try:
        tid = await _tenant_id(db, args.slug)
        async with db.platform_session() as s:
            if args.cmd == "enable":
                keys = await admin.enable(s, tid, args.names)
            elif args.cmd == "disable":
                keys = await admin.disable(s, tid, args.names)
            elif args.cmd == "config":
                keys = await admin.enable(s, tid, [], configs={args.module: json.loads(args.json)})
            elif args.cmd == "label":
                ts = await s.get(TenantSettings, tid)
                if ts is None:
                    ts = TenantSettings(tenant_id=tid)
                    s.add(ts)
                ts.contact_label = args.label
                keys = (await registry.enabled_for(s, tid)).keys
            else:  # show
                enabled = await registry.enabled_for(s, tid)
                keys = enabled.keys
                for k in keys:
                    cfg = enabled.config(k)
                    print(f"  {k:<12} {cfg.model_dump_json() if cfg else '{}'}")
        print(f"{args.slug}: {', '.join(keys) or '(core only)'}")
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "enable", "disable", "config", "label"):
        p = sub.add_parser(name)
        p.add_argument("--slug", required=True)
        if name in ("enable", "disable"):
            p.add_argument("names", nargs="+", help="module keys or preset names")
        if name == "config":
            p.add_argument("module")
            p.add_argument("json")
        if name == "label":
            p.add_argument("label")
    args = ap.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (ModuleError, ValueError) as exc:  # ValidationError is a ValueError
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

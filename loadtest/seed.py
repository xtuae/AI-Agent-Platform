"""Seed the load-test tenant (a generic water business, nothing client-specific).

python -m loadtest.seed     # idempotent
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from api.config import get_settings
from api.core.crypto import encrypt_secret
from api.db.models import Tenant, TenantChannel, TenantModule, TenantSettings
from api.db.session import Database
from api.modules.registry import PRESETS

PNID = os.environ.get("LOAD_PNID", "900100200300")
WABA = os.environ.get("LOAD_WABA", "900100200301")


async def main() -> None:
    db = Database(get_settings())
    async with db.platform_session() as s:
        existing = await s.scalar(select(Tenant).where(Tenant.slug == "load-test"))
        if existing:
            ch = await s.scalar(select(TenantChannel).where(TenantChannel.tenant_id == existing.id))
            if ch is not None:
                ch.phone_number_id, ch.waba_id = PNID, WABA
                ch.access_token_encrypted = encrypt_secret("load-token")  # under the current key
            ts = await s.get(TenantSettings, existing.id)
            if ts is not None:
                ts.agent_persona = {
                    "name": "Sara",
                    "business_description": "water delivery",
                    "service_areas": ["Al Nahda", "Al Qusais"],
                }
            print("load-test tenant exists")
            return
        t = Tenant(name="Load Test Water", slug="load-test", status="active")
        s.add(t)
        await s.flush()
        s.add(
            TenantSettings(
                tenant_id=t.id,
                agent_persona={
                    "name": "Sara",
                    "business_description": "water delivery",
                    "service_areas": ["Al Nahda", "Al Qusais"],
                },
            )
        )
        s.add(
            TenantChannel(
                tenant_id=t.id,
                phone_number_id=PNID,
                waba_id=WABA,
                access_token_encrypted=encrypt_secret("load-token"),
                display_phone="+971600000000",
            )
        )
        s.add_all(TenantModule(tenant_id=t.id, module_key=k) for k in PRESETS["water_delivery"])
    await db.dispose()
    print("seeded load-test tenant")


if __name__ == "__main__":
    asyncio.run(main())

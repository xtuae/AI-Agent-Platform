"""Create or update a tenant: tenant row, WhatsApp channel, settings, admin login, catalog.

THIS FILE is the only place tenant-specific facts may live in code (see TENANT_PROFILES).

    python -m api.scripts.seed_tenant --slug aquamena \\
        --phone-number-id 1234567890 --waba-id 9876543210 --display-phone "+971 5x xxx xxxx" \\
        --admin-email owner@example.com --escalation-phone 9715xxxxxxxx \\
        --catalog /secure/path/aquamena_catalog.json

Secrets come from the environment only, never from argv (argv is visible in `ps`):
    SEED_META_ACCESS_TOKEN   system-user token for the channel (stored Fernet-encrypted)
    SEED_ADMIN_PASSWORD      initial password for --admin-email (min 12 chars)

Prices are NEVER hardcoded here. Products and coupon packages come from --catalog, a JSON file
confirmed by the client, validated below. Without it the tenant is created with an empty
catalog, and the agent (Phase 2) cannot quote or sell anything — the safe failure.

Catalog file shape:
    {"products": [{"sku": "...", "name_en": "...", "name_ar": "...", "category": "water",
                   "brand": "...", "price_aed": "0.00", "cross_sell_priority": 1}],
     "coupon_packages": [{"sku": "...", "name_en": "...", "price_aed": "0.00",
                          "bottles_paid": 20, "bottles_free": 3, "emirate": null,
                          "validity_days": 120}]}

Idempotent: re-running updates in place (tenant by slug, channel by phone_number_id, catalog
rows by sku). It refuses to move a phone_number_id that belongs to another tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from api.config import get_settings
from api.core.crypto import encrypt_secret
from api.core.logging import configure_logging, get_logger
from api.core.passwords import hash_password
from api.db.models import (
    CouponPackage,
    Product,
    Tenant,
    TenantChannel,
    TenantSettings,
    TenantUser,
)
from api.db.session import Database
from api.modules import admin as modules_admin
from api.webhooks.router import TenantRouter

log = get_logger("seed_tenant")


# ---------------------------------------------------------------- tenant profiles


@dataclass(frozen=True)
class TenantProfile:
    name: str
    legal_name: str
    contract_ref: str | None = None
    status: Literal["trial", "active"] = "active"
    timezone: str = "Asia/Dubai"
    locale_default: str = "en"
    service_start: date | None = None
    free_months_until: date | None = None
    meta_charges_borne_by_us_until: date | None = None
    monthly_message_cap_aed: Decimal | None = None
    agent_persona: dict[str, Any] = field(default_factory=dict)
    # modules: preset names or module keys (api/modules/registry.py), plus per-module config
    modules: tuple[str, ...] = ()
    module_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    contact_label: str | None = None


TENANT_PROFILES: dict[str, TenantProfile] = {
    "aquamena": TenantProfile(
        name="Aquamena",
        legal_name="Aquamena Water Treatment L.L.C",
        contract_ref="HMH-AQ-2026-CSA-01",
        # 01_architecture §5.1 comment. service_start / free_months_until are not stated in the
        # specs — set them with --service-start / --free-months-until once confirmed.
        meta_charges_borne_by_us_until=date(2026, 12, 14),
        # 01_architecture §5.3: "the AED 1,500 monthly ceiling".
        monthly_message_cap_aed=Decimal("1500.00"),
        agent_persona={"languages": ["en", "ar", "ar-latn"]},
        # Only 5-gallon cans (water) + snacks as cross-sell; coupon books for 30/60/120 days.
        modules=("water_delivery",),
        contact_label="Customers",
    ),
}


# ---------------------------------------------------------------- catalog file


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductIn(_Strict):
    sku: str = Field(min_length=1, max_length=64)
    name_en: str
    name_ar: str | None = None
    category: Literal["water", "snack"]
    brand: str | None = None
    price_aed: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    cross_sell_priority: int | None = None
    stock_note: str | None = None
    is_active: bool = True


class CouponPackageIn(_Strict):
    sku: str = Field(min_length=1, max_length=64)
    name_en: str
    name_ar: str | None = None
    price_aed: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    bottles_paid: int = Field(ge=1)
    bottles_free: int = Field(default=0, ge=0)
    emirate: str | None = None
    validity_days: int | None = Field(default=None, ge=1)
    is_active: bool = True


class Catalog(_Strict):
    products: list[ProductIn] = Field(default_factory=list)
    coupon_packages: list[CouponPackageIn] = Field(default_factory=list)


# ---------------------------------------------------------------- seeding


class SeedError(Exception):
    pass


@dataclass
class SeedArgs:
    slug: str
    phone_number_id: str
    waba_id: str | None = None
    display_phone: str | None = None
    admin_email: str | None = None
    escalation_phone: str | None = None
    catalog: Catalog | None = None
    service_start: date | None = None
    free_months_until: date | None = None
    access_token: str | None = None
    admin_password: str | None = None


@dataclass
class SeedResult:
    tenant_id: uuid.UUID
    channel_id: uuid.UUID
    products: int = 0
    coupon_packages: int = 0
    admin_user: bool = False


async def seed(db: Database, args: SeedArgs, *, router: TenantRouter | None = None) -> SeedResult:
    profile = TENANT_PROFILES.get(args.slug)
    if profile is None:
        raise SeedError(f"no profile for slug {args.slug!r}; add one to TENANT_PROFILES")
    if args.admin_email and not args.admin_password:
        raise SeedError("SEED_ADMIN_PASSWORD must be set when --admin-email is given")

    # ---- platform tables (no tenant context)
    async with db.platform_session() as s:
        tenant = await s.scalar(select(Tenant).where(Tenant.slug == args.slug))
        if tenant is None:
            # Status is set only on creation: a re-seed must never reactivate a suspended tenant.
            tenant = Tenant(slug=args.slug, name=profile.name, status=profile.status)
            s.add(tenant)
        tenant.name = profile.name
        tenant.legal_name = profile.legal_name
        tenant.contract_ref = profile.contract_ref
        tenant.timezone = profile.timezone
        tenant.locale_default = profile.locale_default
        tenant.meta_charges_borne_by_us_until = profile.meta_charges_borne_by_us_until
        tenant.service_start = args.service_start or profile.service_start or tenant.service_start
        tenant.free_months_until = (
            args.free_months_until or profile.free_months_until or tenant.free_months_until
        )
        await s.flush()
        tenant_id = tenant.id

        channel = await s.scalar(
            select(TenantChannel).where(TenantChannel.phone_number_id == args.phone_number_id)
        )
        if channel is not None and channel.tenant_id != tenant_id:
            raise SeedError(
                "phone_number_id is already bound to a different tenant — refusing to reroute it"
            )
        if channel is None:
            channel = TenantChannel(tenant_id=tenant_id, phone_number_id=args.phone_number_id)
            s.add(channel)
        channel.waba_id = args.waba_id or channel.waba_id
        channel.display_phone = args.display_phone or channel.display_phone
        channel.is_active = True
        if args.access_token:
            channel.access_token_encrypted = encrypt_secret(args.access_token)
        await s.flush()
        channel_id = channel.id

        ts = await s.get(TenantSettings, tenant_id)
        if ts is None:
            ts = TenantSettings(tenant_id=tenant_id)
            s.add(ts)
        ts.monthly_message_cap_aed = profile.monthly_message_cap_aed
        ts.agent_persona = profile.agent_persona or ts.agent_persona
        ts.escalation_phone = args.escalation_phone or ts.escalation_phone
        ts.contact_label = profile.contact_label or ts.contact_label
        await s.flush()
        if profile.modules:
            await modules_admin.enable(
                s, tenant_id, profile.modules, configs=profile.module_config or None
            )

        admin = False
        if args.admin_email and args.admin_password:
            user = await s.scalar(
                select(TenantUser).where(
                    TenantUser.tenant_id == tenant_id, TenantUser.email == args.admin_email.lower()
                )
            )
            if user is None:
                s.add(
                    TenantUser(
                        tenant_id=tenant_id,
                        email=args.admin_email.lower(),
                        password_hash=hash_password(args.admin_password),
                        role="admin",
                    )
                )
            admin = True  # an existing user's password is never overwritten by a re-seed

    result = SeedResult(tenant_id=tenant_id, channel_id=channel_id, admin_user=admin)

    # ---- catalog (tenant-scoped, RLS)
    if args.catalog is not None:
        async with db.tenant_session(tenant_id) as s:
            for p in args.catalog.products:
                stmt = insert(Product).values(tenant_id=tenant_id, **p.model_dump())
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[Product.tenant_id, Product.sku],
                        set_={k: getattr(stmt.excluded, k) for k in p.model_dump() if k != "sku"},
                    )
                )
            for c in args.catalog.coupon_packages:
                stmt = insert(CouponPackage).values(tenant_id=tenant_id, **c.model_dump())
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[CouponPackage.tenant_id, CouponPackage.sku],
                        set_={k: getattr(stmt.excluded, k) for k in c.model_dump() if k != "sku"},
                    )
                )
        result.products = len(args.catalog.products)
        result.coupon_packages = len(args.catalog.coupon_packages)

    if router is not None:
        await router.invalidate(phone_number_id=args.phone_number_id, waba_id=args.waba_id)
    return result


# ---------------------------------------------------------------- CLI


def _parse(argv: list[str]) -> SeedArgs:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--slug", required=True, choices=sorted(TENANT_PROFILES))
    ap.add_argument("--phone-number-id", required=True)
    ap.add_argument("--waba-id")
    ap.add_argument("--display-phone")
    ap.add_argument("--admin-email")
    ap.add_argument("--escalation-phone")
    ap.add_argument("--catalog", type=Path)
    ap.add_argument("--service-start", type=date.fromisoformat)
    ap.add_argument("--free-months-until", type=date.fromisoformat)
    ns = ap.parse_args(argv)
    catalog = Catalog.model_validate_json(ns.catalog.read_text()) if ns.catalog else None
    return SeedArgs(
        slug=ns.slug,
        phone_number_id=ns.phone_number_id,
        waba_id=ns.waba_id,
        display_phone=ns.display_phone,
        admin_email=ns.admin_email,
        escalation_phone=ns.escalation_phone,
        catalog=catalog,
        service_start=ns.service_start,
        free_months_until=ns.free_months_until,
        access_token=os.environ.get("SEED_META_ACCESS_TOKEN") or None,
        admin_password=os.environ.get("SEED_ADMIN_PASSWORD") or None,
    )


async def _main(argv: list[str]) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    args = _parse(argv)
    db = Database(settings)
    redis = Redis.from_url(str(settings.redis_url), socket_timeout=settings.redis_timeout_s)
    try:
        result = await seed(db, args, router=TenantRouter(db, redis))
    except SeedError as exc:
        log.error("seed_failed", reason=str(exc))
        return 1
    finally:
        await redis.aclose()
        await db.dispose()
    log.info(
        "seed_done",
        tenant_id=str(result.tenant_id),
        channel_id=str(result.channel_id),
        products=result.products,
        coupon_packages=result.coupon_packages,
        admin_user=result.admin_user,
        access_token_set=args.access_token is not None,
    )
    if args.catalog is None:
        log.warning("seed_catalog_empty", effect="no products or coupon packages; pass --catalog")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))

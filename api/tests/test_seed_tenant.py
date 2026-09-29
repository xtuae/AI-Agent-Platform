"""scripts/seed_tenant.py: idempotent, never reroutes a channel, never stores a plaintext token."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select

from api.config import get_settings
from api.core.crypto import decrypt_secret
from api.core.passwords import verify_password
from api.db.models import CouponPackage, Product, Tenant, TenantChannel, TenantSettings, TenantUser
from api.db.session import Database
from api.scripts import seed_tenant as st
from api.tests.conftest import meta_id
from api.webhooks.router import TenantRouter

CATALOG = st.Catalog.model_validate(
    {
        "products": [
            {"sku": "W-5G", "name_en": "Water 5 gallon", "category": "water", "price_aed": "10.00"},
            {
                "sku": "S-1",
                "name_en": "Snack",
                "category": "snack",
                "price_aed": "2.50",
                "cross_sell_priority": 1,
            },
        ],
        "coupon_packages": [
            {
                "sku": "CB-A",
                "name_en": "Book A",
                "price_aed": "100.00",
                "bottles_paid": 10,
                "bottles_free": 1,
            },
        ],
    }
)


@pytest.fixture
def profile_slug(monkeypatch: pytest.MonkeyPatch) -> str:
    slug = f"test-{uuid.uuid4().hex[:10]}"
    monkeypatch.setitem(
        st.TENANT_PROFILES,
        slug,
        st.TenantProfile(
            name="Test Co", legal_name="Test Co LLC", monthly_message_cap_aed=Decimal("100.00")
        ),
    )
    return slug


async def test_seed_is_idempotent_and_complete(
    db: Database, migrated: None, profile_slug: str
) -> None:
    pnid, waba = meta_id(), meta_id()
    args = st.SeedArgs(
        slug=profile_slug,
        phone_number_id=pnid,
        waba_id=waba,
        admin_email="Owner@Example.com",
        admin_password="correct horse battery staple",
        access_token="EAAG-seed-token",
        catalog=CATALOG,
    )
    first = await st.seed(db, args)
    second = await st.seed(db, args)
    assert (first.tenant_id, first.channel_id) == (second.tenant_id, second.channel_id)

    async with db.platform_session() as s:
        assert len((await s.scalars(select(Tenant).where(Tenant.slug == profile_slug))).all()) == 1
        ch = await s.scalar(select(TenantChannel).where(TenantChannel.phone_number_id == pnid))
        assert ch is not None
        assert ch.access_token_encrypted is not None
        assert b"EAAG-seed-token" not in ch.access_token_encrypted
        assert decrypt_secret(ch.access_token_encrypted) == "EAAG-seed-token"
        users = (
            await s.scalars(select(TenantUser).where(TenantUser.tenant_id == first.tenant_id))
        ).all()
        assert [(u.email, u.role) for u in users] == [("owner@example.com", "admin")]
        assert verify_password("correct horse battery staple", users[0].password_hash)
        ts = await s.get(TenantSettings, first.tenant_id)
        assert ts is not None
        assert ts.monthly_message_cap_aed == Decimal("100.00")

    async with db.tenant_session(first.tenant_id) as s:
        assert sorted((await s.scalars(select(Product.sku))).all()) == ["S-1", "W-5G"]
        assert (await s.scalars(select(CouponPackage.sku))).all() == ["CB-A"]


async def test_seed_updates_catalog_prices(db: Database, migrated: None, profile_slug: str) -> None:
    args = st.SeedArgs(slug=profile_slug, phone_number_id=meta_id(), catalog=CATALOG)
    res = await st.seed(db, args)
    repriced = CATALOG.model_copy(deep=True)
    repriced.products[0].price_aed = Decimal("11.00")
    await st.seed(
        db, st.SeedArgs(slug=profile_slug, phone_number_id=args.phone_number_id, catalog=repriced)
    )
    async with db.tenant_session(res.tenant_id) as s:
        price = await s.scalar(select(Product.price_aed).where(Product.sku == "W-5G"))
        assert price == Decimal("11.00")


async def test_seed_refuses_to_reroute_another_tenants_number(
    db: Database, migrated: None, profile_slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pnid = meta_id()
    await st.seed(db, st.SeedArgs(slug=profile_slug, phone_number_id=pnid))
    other = f"test-{uuid.uuid4().hex[:10]}"
    monkeypatch.setitem(
        st.TENANT_PROFILES, other, st.TenantProfile(name="Other", legal_name="Other LLC")
    )
    with pytest.raises(st.SeedError, match="different tenant"):
        await st.seed(db, st.SeedArgs(slug=other, phone_number_id=pnid))


async def test_seed_does_not_reactivate_a_suspended_tenant(
    db: Database, migrated: None, profile_slug: str
) -> None:
    args = st.SeedArgs(slug=profile_slug, phone_number_id=meta_id())
    res = await st.seed(db, args)
    async with db.platform_session() as s:
        t = await s.get(Tenant, res.tenant_id)
        assert t is not None
        t.status = "suspended"
    await st.seed(db, args)
    async with db.platform_session() as s:
        t = await s.get(Tenant, res.tenant_id)
        assert t is not None
        assert t.status == "suspended"


async def test_seed_requires_password_with_admin_email(
    db: Database, migrated: None, profile_slug: str
) -> None:
    with pytest.raises(st.SeedError, match="SEED_ADMIN_PASSWORD"):
        await st.seed(
            db, st.SeedArgs(slug=profile_slug, phone_number_id=meta_id(), admin_email="a@b.co")
        )


async def test_seed_invalidates_route_cache(
    db: Database, migrated: None, profile_slug: str
) -> None:
    redis = Redis.from_url(str(get_settings().redis_url))
    try:
        pnid = meta_id()
        router = TenantRouter(db, redis)
        assert await router.resolve(pnid) is None  # caches a negative result
        res = await st.seed(db, st.SeedArgs(slug=profile_slug, phone_number_id=pnid), router=router)
        route = await router.resolve(pnid)
        assert route is not None
        assert route.tenant_id == res.tenant_id
    finally:
        await redis.aclose()


def test_catalog_rejects_unknown_fields_and_negative_prices() -> None:
    with pytest.raises(ValidationError, match="greater than or equal"):
        st.Catalog.model_validate(
            {"products": [{"sku": "x", "name_en": "x", "category": "water", "price_aed": "-1"}]}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        st.Catalog.model_validate(
            {
                "products": [
                    {
                        "sku": "x",
                        "name_en": "x",
                        "category": "water",
                        "price_aed": "1",
                        "discount": 5,
                    }
                ]
            }
        )


def test_aquamena_profile_contains_no_prices() -> None:
    """Prices come only from the confirmed catalog file, never from code."""
    profile = st.TENANT_PROFILES["aquamena"]
    assert profile.legal_name == "Aquamena Water Treatment L.L.C"
    assert not hasattr(profile, "products")

"""Row-Level Security actually works (constraint 1).

Two layers are tested separately:
  * the DATABASE (raw SQL, bypassing every application guard) — RLS is the authority;
  * the APPLICATION session guard — a forgotten tenant context fails loudly.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.base import TENANT_SCOPED_TABLES
from api.db.models import Customer
from api.db.session import TENANT_KEY, Database, TenantContextMissingError, TenantMismatchError
from api.tests.conftest import TenantPair

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")
PLATFORM_TABLES_WITH_TENANT_ID = {
    "tenant_channels",
    "tenant_users",
    "tenant_settings",
    "auth_refresh_tokens",  # looked up by cookie before a tenant is known (Phase 3)
}


# ---------------------------------------------------------------- acceptance (Phase 0)


async def test_tenant_a_sees_exactly_its_own_row(db: Database, tenants: TenantPair) -> None:
    async with db.tenant_session(tenants.a) as s:
        rows = (await s.scalars(select(Customer))).all()
    assert [r.id for r in rows] == [tenants.customer_a]
    assert all(r.tenant_id == tenants.a for r in rows)


async def test_tenant_b_row_is_invisible_to_tenant_a(db: Database, tenants: TenantPair) -> None:
    async with db.tenant_session(tenants.a) as s:
        assert await s.get(Customer, tenants.customer_b) is None
        by_id = await s.scalar(select(Customer).where(Customer.id == tenants.customer_b))
        assert by_id is None
        # Even an explicit filter for B's tenant_id returns nothing.
        n = await s.scalar(
            select(func.count()).select_from(Customer).where(Customer.tenant_id == tenants.b)
        )
        assert n == 0


async def test_no_tenant_context_sees_zero_rows(db: Database, tenants: TenantPair) -> None:
    # Raw SQL: bypasses the ORM guard, so this is the database alone answering.
    async with db.platform_session() as s:
        assert await s.scalar(text("SELECT count(*) FROM customers")) == 0
        assert (
            await s.scalar(
                text("SELECT count(*) FROM customers WHERE id = :id"), {"id": tenants.customer_b}
            )
            == 0
        )


async def test_every_tenant_table_is_empty_without_context(
    db: Database, tenants: TenantPair
) -> None:
    async with db.platform_session() as s:
        for table in sorted(TENANT_SCOPED_TABLES):
            assert await s.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table  # noqa: S608


# ---------------------------------------------------------------- pooled-connection safety


async def test_tenant_setting_does_not_survive_the_transaction(
    db: Database, tenants: TenantPair
) -> None:
    """SET LOCAL semantics: after tenant A's transaction ends, the SAME connection sees zero rows.

    This is also the case that breaks the spec's literal policy — `current_setting` returns ''
    here, and ''::uuid raises. The NULLIF form returns zero rows instead.
    """
    async with db.engine.connect() as conn:
        async with conn.begin():
            await conn.execute(SET_TENANT, {"t": str(tenants.a)})
            assert await conn.scalar(text("SELECT count(*) FROM customers")) == 1
        async with conn.begin():
            assert await conn.scalar(text("SELECT count(*) FROM customers")) == 0


async def test_never_set_setting_returns_zero_rows_not_an_error(db: Database) -> None:
    async with db.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM customers")) == 0


async def test_malformed_tenant_setting_is_an_error_not_a_leak(db: Database) -> None:
    async with db.engine.connect() as conn:
        await conn.execute(SET_TENANT, {"t": "' OR 1=1 --"})
        with pytest.raises(DBAPIError):
            await conn.scalar(text("SELECT count(*) FROM customers"))


# ---------------------------------------------------------------- writes across tenants (DB layer)


async def test_insert_for_another_tenant_is_rejected_by_rls(
    db: Database, tenants: TenantPair
) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db.tenant_session(tenants.a) as s:
            await s.execute(
                text("INSERT INTO customers (tenant_id, wa_id) VALUES (:t, '971509999999')"),
                {"t": tenants.b},
            )


async def test_update_and_delete_cannot_touch_another_tenant(
    db: Database, tenants: TenantPair
) -> None:
    async with db.tenant_session(tenants.a) as s:
        upd = await s.execute(
            text("UPDATE customers SET name = 'pwned' WHERE id = :id"), {"id": tenants.customer_b}
        )
        dele = await s.execute(
            text("DELETE FROM customers WHERE id = :id"), {"id": tenants.customer_b}
        )
        assert upd.rowcount == 0  # type: ignore[attr-defined]
        assert dele.rowcount == 0  # type: ignore[attr-defined]
    async with db.tenant_session(tenants.b) as s:
        c = await s.get(Customer, tenants.customer_b)
        assert c is not None
        assert c.name == "Test Customer"


async def test_moving_a_row_to_another_tenant_is_rejected(
    db: Database, tenants: TenantPair
) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with db.tenant_session(tenants.a) as s:
            await s.execute(
                text("UPDATE customers SET tenant_id = :b WHERE id = :id"),
                {"b": tenants.b, "id": tenants.customer_a},
            )


async def test_composite_fk_blocks_cross_tenant_references(
    db: Database, tenants: TenantPair
) -> None:
    """FK checks bypass RLS, so a plain FK would accept B's customer id on A's order."""
    with pytest.raises(IntegrityError):
        async with db.tenant_session(tenants.a) as s:
            await s.execute(
                text(
                    "INSERT INTO orders (tenant_id, customer_id, order_no) "
                    "VALUES (:a, :cust_b, 'A-0001')"
                ),
                {"a": tenants.a, "cust_b": tenants.customer_b},
            )


# ---------------------------------------------------------------- application guard (fail loudly)


async def test_orm_query_without_tenant_context_raises(db: Database, tenants: TenantPair) -> None:
    with pytest.raises(TenantContextMissingError):
        async with db.platform_session() as s:
            await s.scalars(select(Customer))


async def test_orm_get_without_tenant_context_raises(db: Database, tenants: TenantPair) -> None:
    with pytest.raises(TenantContextMissingError):
        async with db.platform_session() as s:
            await s.get(Customer, tenants.customer_a)


async def test_orm_write_without_tenant_context_raises(db: Database, tenants: TenantPair) -> None:
    with pytest.raises(TenantContextMissingError):
        async with db.platform_session() as s:
            s.add(Customer(tenant_id=tenants.a, wa_id="971501111111"))


async def test_orm_write_for_another_tenant_raises(db: Database, tenants: TenantPair) -> None:
    with pytest.raises(TenantMismatchError):
        async with db.tenant_session(tenants.a) as s:
            s.add(Customer(tenant_id=tenants.b, wa_id="971502222222"))


async def test_tenant_session_fills_tenant_id(db: Database, tenants: TenantPair) -> None:
    async with db.tenant_session(tenants.a) as s:
        c = Customer(wa_id="971503333333")
        s.add(c)
        await s.flush()
        assert c.tenant_id == tenants.a


async def test_tenant_session_rejects_non_uuid(db: Database) -> None:
    with pytest.raises(TypeError):
        async with db.tenant_session("not-a-uuid"):  # type: ignore[arg-type]
            pass


async def test_tenant_context_survives_commit_within_a_session(
    db: Database, tenants: TenantPair
) -> None:
    """after_begin re-applies the tenant on every transaction, so a session that commits and
    carries on stays scoped (SET LOCAL alone would be lost at the first commit)."""
    async with AsyncSession(db.engine) as s:
        s.info[TENANT_KEY] = tenants.a
        async with s.begin():
            first = (await s.scalars(select(Customer.id))).all()
        async with s.begin():
            second = (await s.scalars(select(Customer.id))).all()
    assert first == second == [tenants.customer_a]


# ---------------------------------------------------------------- the database agrees with the code


async def test_app_role_cannot_bypass_rls(db: Database) -> None:
    async with db.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        assert row.rolsuper is False
        assert row.rolbypassrls is False
        owned = await conn.scalar(
            text("SELECT count(*) FROM pg_tables WHERE tableowner = current_user")
        )
        assert owned == 0, "the app role must own no tables (owners bypass RLS)"


async def test_every_tenant_table_has_forced_rls_and_policy(db: Database) -> None:
    async with db.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                           EXISTS (SELECT 1 FROM pg_policies p
                                   WHERE p.tablename = c.relname
                                     AND p.policyname = 'tenant_isolation') AS has_policy
                    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                    """
                )
            )
        ).all()
    by_name = {r.relname: r for r in rows}
    for table in TENANT_SCOPED_TABLES:
        r = by_name[table]
        assert r.relrowsecurity, f"{table}: RLS not enabled"
        assert r.relforcerowsecurity, f"{table}: RLS not forced"
        assert r.has_policy, f"{table}: no tenant_isolation policy"
    rls_tables = {r.relname for r in rows if r.relrowsecurity}
    assert rls_tables == TENANT_SCOPED_TABLES


async def test_no_unprotected_table_has_a_tenant_id(db: Database) -> None:
    """A new table with tenant_id must be RLS-protected or explicitly listed as platform."""
    async with db.engine.connect() as conn:
        tables = set(
            (
                await conn.scalars(
                    text(
                        "SELECT table_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
                    )
                )
            ).all()
        )
    assert tables - TENANT_SCOPED_TABLES == PLATFORM_TABLES_WITH_TENANT_ID


async def test_tenant_id_is_not_null_everywhere(db: Database) -> None:
    async with db.engine.connect() as conn:
        nullable = (
            await conn.scalars(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'tenant_id' "
                    "AND is_nullable = 'YES'"
                )
            )
        ).all()
    assert nullable == []


async def test_random_tenant_sees_nothing(db: Database, tenants: TenantPair) -> None:
    async with db.tenant_session(uuid.uuid4()) as s:
        assert (await s.scalars(select(Customer))).all() == []

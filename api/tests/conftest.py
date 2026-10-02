"""Shared fixtures. Always at least two tenants — isolation is tested, not assumed.

Tests run against a real PostgreSQL 16 + pgvector and a real Redis, configured through the same
environment variables as the app (DATABASE_URL = app role, MIGRATIONS_DATABASE_URL = owner role,
REDIS_URL, APP_ENCRYPTION_KEY). Roles are created by docker/postgres/init/01-roles.sh.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr

from api.config import Settings, get_settings
from api.core.passwords import hash_password
from api.db.models import Customer, Tenant, TenantChannel, TenantModule, TenantUser
from api.db.session import Database
from api.main import create_app
from api.meta.client import MetaClient
from api.webhooks.ingest import WebhookIngestor
from api.webhooks.signature import compute_signature

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def alembic_config() -> Config:
    cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "api/db/migrations"))
    return cfg


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def migrated(settings: Settings) -> None:
    """Bring the schema to head once per session (runs as the owner role)."""
    command.upgrade(alembic_config(), "head")


@pytest.fixture
async def db(settings: Settings, migrated: None) -> AsyncIterator[Database]:
    database = Database(settings)
    try:
        yield database
    finally:
        await database.dispose()


@dataclass(frozen=True)
class TenantPair:
    a: uuid.UUID
    b: uuid.UUID
    customer_a: uuid.UUID
    customer_b: uuid.UUID


WATER_PRESET = ("catalog", "orders", "coupons")


async def make_tenant(
    db: Database, label: str, *, modules: tuple[str, ...] = WATER_PRESET
) -> uuid.UUID:
    """A fresh tenant. Test tenants get the water preset unless a test says otherwise, because
    that is what every Phase 0-3 behaviour was specified against."""
    tenant_id = uuid.uuid4()
    async with db.platform_session() as s:
        s.add(Tenant(id=tenant_id, name=f"Test {label}", slug=f"t-{label}-{tenant_id.hex[:10]}"))
        await s.flush()
        s.add_all(TenantModule(tenant_id=tenant_id, module_key=k) for k in modules)
    return tenant_id


async def make_customer(db: Database, tenant_id: uuid.UUID, wa_id: str) -> uuid.UUID:
    customer_id = uuid.uuid4()
    async with db.tenant_session(tenant_id) as s:
        s.add(Customer(id=customer_id, wa_id=wa_id, name="Test Customer"))
    return customer_id


@pytest.fixture
async def tenants(db: Database) -> TenantPair:
    """Two fresh tenants, one customer each. Fresh ids per test, so tests never share rows."""
    a = await make_tenant(db, "a")
    b = await make_tenant(db, "b")
    return TenantPair(
        a=a,
        b=b,
        customer_a=await make_customer(db, a, "971500000001"),
        customer_b=await make_customer(db, b, "971500000002"),
    )


# ---------------------------------------------------------------- Phase 1: channels + webhook


@pytest.fixture(scope="session", autouse=True)
def meta_secrets(settings: Settings) -> None:
    """Per-run random Meta secrets unless the environment provides them."""
    if settings.meta_app_secret is None:
        settings.meta_app_secret = SecretStr(secrets.token_hex(32))
    if settings.meta_verify_token is None:
        settings.meta_verify_token = SecretStr(secrets.token_hex(16))


def meta_id() -> str:
    """A random numeric Meta-style id (phone_number_id / WABA id)."""
    return str(10**14 + secrets.randbelow(9 * 10**14))


def wamid() -> str:
    return f"wamid.TEST{uuid.uuid4().hex}"


async def make_channel(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    phone_number_id: str | None = None,
    waba_id: str | None = None,
    active: bool = True,
) -> TenantChannel:
    ch = TenantChannel(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id or meta_id(),
        waba_id=waba_id or meta_id(),
        is_active=active,
    )
    async with db.platform_session() as s:
        s.add(ch)
    return ch


@dataclass(frozen=True)
class ChannelPair:
    t: TenantPair
    a: TenantChannel
    b: TenantChannel


@pytest.fixture
async def channels(db: Database, tenants: TenantPair) -> ChannelPair:
    return ChannelPair(
        t=tenants,
        a=await make_channel(db, tenants.a),
        b=await make_channel(db, tenants.b),
    )


class RecordingEnqueuer:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], str | None]] = []
        self.deferred: dict[str | None, float] = {}

    async def enqueue_job(
        self, function: str, *args: Any, _job_id: str | None = None, _defer_by: float | None = None
    ) -> Any:
        self.jobs.append((function, args, _job_id))
        if _defer_by:
            self.deferred[_job_id] = _defer_by
        return object()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constraint 3: the webhook handler never calls the Graph API (or anything else)."""

    async def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("network call from the webhook request path")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    monkeypatch.setattr(MetaClient, "_request", refuse)


@dataclass
class WebhookHarness:
    app: FastAPI
    client: httpx.AsyncClient
    jobs: RecordingEnqueuer
    db: Database
    secret: str

    async def post(
        self, payload: dict[str, Any] | bytes, *, signature: str | None = "auto"
    ) -> httpx.Response:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {"content-type": "application/json"}
        if signature == "auto":
            headers["x-hub-signature-256"] = compute_signature(body, self.secret)
        elif signature is not None:
            headers["x-hub-signature-256"] = signature
        return await self.client.post("/webhook/meta", content=body, headers=headers)


@pytest.fixture
async def webhook(
    settings: Settings, migrated: None, no_network: None
) -> AsyncIterator[WebhookHarness]:
    application = create_app()
    async with LifespanManager(application):
        # the webhook buffer is shared Redis state: every test starts with it empty
        await application.state.redis.delete("webhook:buffer", "webhook:buffer:processing")
        jobs = RecordingEnqueuer()
        application.state.ingestor = WebhookIngestor(
            application.state.db,
            application.state.redis,
            application.state.router,
            jobs,
            dedup_ttl_s=settings.webhook_dedup_ttl_s,
        )
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert settings.meta_app_secret is not None
            yield WebhookHarness(
                app=application,
                client=client,
                jobs=jobs,
                db=application.state.db,
                secret=settings.meta_app_secret.get_secret_value(),
            )


# ---------------------------------------------------------------- Meta payload builders


def text_message(
    sender: str, message_id: str, body: str = "hello", ts: int | None = None
) -> dict[str, Any]:
    return {
        "from": sender,
        "id": message_id,
        "timestamp": str(ts or int(time.time())),
        "type": "text",
        "text": {"body": body},
    }


def messages_change(
    phone_number_id: str | None,
    *,
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"messaging_product": "whatsapp"}
    if phone_number_id is not None:
        value["metadata"] = {
            "display_phone_number": "971500000000",
            "phone_number_id": phone_number_id,
        }
    if contacts is not None:
        value["contacts"] = contacts
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {"field": "messages", "value": value}


def entry(waba_id: str, *changes: dict[str, Any]) -> dict[str, Any]:
    return {"id": waba_id, "changes": list(changes)}


def envelope(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"object": "whatsapp_business_account", "entry": list(entries)}


# ---------------------------------------------------------------- Phase 3: dashboard


@pytest.fixture(scope="session", autouse=True)
def jwt_secret(settings: Settings) -> None:
    if settings.jwt_secret is None:
        settings.jwt_secret = SecretStr(secrets.token_urlsafe(48))


DEFAULT_PASSWORD = "correct horse battery staple"


@dataclass
class DashHarness:
    app: FastAPI
    client: httpx.AsyncClient
    db: Database

    async def user(
        self,
        tenant_id: uuid.UUID,
        role: str = "admin",
        *,
        email: str | None = None,
        password: str = DEFAULT_PASSWORD,
        active: bool = True,
    ) -> TenantUser:
        u = TenantUser(
            tenant_id=tenant_id,
            email=email or f"{role}-{uuid.uuid4().hex[:8]}@example.test",
            name=f"{role.title()} Person",
            role=role,
            password_hash=_password_hash(password),
            is_active=active,
        )
        async with self.db.platform_session() as s:
            s.add(u)
        return u

    async def login(
        self, tenant_id: uuid.UUID, role: str = "admin", *, email: str | None = None
    ) -> dict[str, str]:
        """A fresh login of `role` in `tenant_id`; returns request headers carrying its token."""
        u = await self.user(tenant_id, role, email=email)
        r = await self.client.post(
            "/api/v1/auth/login", json={"email": u.email, "password": DEFAULT_PASSWORD}
        )
        assert r.status_code == 200, r.text
        return {"authorization": f"Bearer {r.json()['access_token']}"}


_HASH_CACHE: dict[str, str] = {}


def _password_hash(password: str) -> str:
    if password not in _HASH_CACHE:  # scrypt is deliberately slow; tests reuse one hash
        _HASH_CACHE[password] = hash_password(password)
    return _HASH_CACHE[password]


@pytest.fixture
async def dash(settings: Settings, migrated: None) -> AsyncIterator[DashHarness]:
    application = create_app()
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application)
        # https: the refresh cookie is Secure, so the client only sends it over https
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
            yield DashHarness(app=application, client=client, db=application.state.db)

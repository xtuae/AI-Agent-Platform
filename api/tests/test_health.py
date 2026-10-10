"""GET /health and the correlation-id middleware."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from redis.asyncio import Redis

from api.main import create_app


@pytest.fixture
async def app(migrated: None) -> AsyncIterator[FastAPI]:
    application = create_app()
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_ok(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["redis"] == "ok"
    assert set(body) == {"status", "db", "redis", "version", "git_sha"}


async def test_health_head(client: httpx.AsyncClient) -> None:
    # free uptime monitors send HEAD only; a GET-only route would answer 405
    r = await client.head("/health")
    assert r.status_code == 200
    assert r.content == b""


async def test_health_degraded_when_redis_down(app: FastAPI, client: httpx.AsyncClient) -> None:
    real = app.state.redis
    app.state.redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        r = await client.get("/health")
    finally:
        await app.state.redis.aclose()
        app.state.redis = real
    assert r.status_code == 503
    assert r.json()["redis"] == "error"
    assert r.json()["status"] == "degraded"


async def test_correlation_id_generated(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert len(r.headers["x-request-id"]) == 32


async def test_correlation_id_propagated_when_well_formed(client: httpx.AsyncClient) -> None:
    r = await client.get("/health", headers={"x-request-id": "turn-abc123def456"})
    assert r.headers["x-request-id"] == "turn-abc123def456"


async def test_correlation_id_rejected_when_malformed(client: httpx.AsyncClient) -> None:
    r = await client.get("/health", headers={"x-request-id": "bad id\r\ninjected: 1"})
    assert r.headers["x-request-id"] != "bad id\r\ninjected: 1"
    assert len(r.headers["x-request-id"]) == 32

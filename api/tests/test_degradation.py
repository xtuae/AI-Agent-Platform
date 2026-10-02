"""Phase 6: graceful degradation. Postgres down → the webhook still answers 200 and the message
waits in Redis; nothing is lost when the database comes back. Redis down too → 500, so Meta keeps
the message and retries."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from api.config import Settings
from api.db.models import Message
from api.db.session import Database
from api.tests.conftest import (
    ChannelPair,
    RecordingEnqueuer,
    WebhookHarness,
    entry,
    envelope,
    messages_change,
    text_message,
    wamid,
)
from api.webhooks import buffer
from api.webhooks.buffer import Breaker
from api.webhooks.ingest import WebhookIngestor
from api.webhooks.router import TenantRouter

SENDER = "971509998877"


class KillableProxy:
    """A TCP proxy in front of Postgres. `kill()` drops every open connection and refuses new
    ones — what the app sees when the Postgres container dies."""

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, OSError, asyncio.CancelledError):
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        w.close()

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            sr, sw = await asyncio.open_connection(*self._target)
        except OSError:
            cw.close()
            return
        self._writers |= {cw, sw}
        await asyncio.gather(self._pipe(cr, sw), self._pipe(sr, cw))

    async def kill(self) -> None:
        assert self._server is not None
        self._server.close()
        for w in self._writers:
            w.transport.abort()
        self._writers.clear()
        await self._server.wait_closed()


@pytest.fixture
async def proxied_db(settings: Settings) -> AsyncIterator[tuple[Database, KillableProxy]]:
    url = settings.database_url
    proxy = KillableProxy(url.hosts()[0]["host"] or "", url.hosts()[0]["port"] or 5432)
    await proxy.start()
    dsn = str(url).replace(f":{url.hosts()[0]['port']}/", f":{proxy.port}/")
    db = Database(settings.model_copy(update={"db_connect_timeout_s": 1.0}), url=dsn)
    try:
        yield db, proxy
    finally:
        await db.dispose()
        with contextlib.suppress(Exception):
            await proxy.kill()


async def _messages(db: Database, tenant: Any) -> list[str]:
    async with db.tenant_session(tenant) as s:
        return [
            m.wamid or ""
            for m in (await s.scalars(select(Message).order_by(Message.created_at))).all()
        ]


def _payload(channels: ChannelPair, *ids: str) -> dict[str, Any]:
    return envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(
                channels.a.phone_number_id, messages=[text_message(SENDER, i) for i in ids]
            ),
        )
    )


async def test_killing_postgres_loses_no_inbound_message(
    webhook: WebhookHarness, channels: ChannelPair, proxied_db: tuple[Database, KillableProxy]
) -> None:
    """The acceptance test: the app's database connections die mid-flight; messages keep
    arriving; every one is stored, once, after Postgres comes back."""
    db, proxy = proxied_db
    app = webhook.app
    # the app talks to Postgres only through the proxy (routing cache is cold: real DB lookups)
    await TenantRouter(db, app.state.redis).invalidate(
        phone_number_id=channels.a.phone_number_id, waba_id=channels.a.waba_id
    )
    jobs = RecordingEnqueuer()
    app.state.ingestor = WebhookIngestor(
        db, app.state.redis, TenantRouter(db, app.state.redis), jobs, dedup_ttl_s=3600
    )

    first = wamid()
    assert (await webhook.post(_payload(channels, first))).status_code == 200
    await proxy.kill()  # the Postgres container dies

    during = [wamid() for _ in range(5)]
    for w in during:
        r = await webhook.post(_payload(channels, w))
        assert r.status_code == 200  # Meta is told "got it"
    assert await buffer.depth(app.state.redis) == 5

    # draining while still down changes nothing and keeps every body
    report = await buffer.drain(app.state.redis, app.state.ingestor)
    assert (report.replayed, report.stopped_on_failure, report.remaining) == (0, True, 5)

    # Postgres is back: the worker drains through a fresh connection
    revived = WebhookIngestor(
        webhook.db,
        app.state.redis,
        TenantRouter(webhook.db, app.state.redis),
        jobs,
        dedup_ttl_s=3600,
    )
    report = await buffer.drain(app.state.redis, revived)
    assert (report.replayed, report.remaining) == (5, 0)
    assert await _messages(webhook.db, channels.t.a) == [first, *during]  # all, once, in order
    assert sorted(str(j[2]) for j in jobs.jobs) == sorted(f"in:{w}" for w in [first, *during])

    # a late Meta retry of one of them is a duplicate, not a second message
    await webhook.post(_payload(channels, during[0]))
    assert len(await _messages(webhook.db, channels.t.a)) == 6


async def test_breaker_skips_the_database_while_it_is_down(
    webhook: WebhookHarness, channels: ChannelPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real = webhook.app.state.ingestor.ingest

    async def counting(payload: Any) -> Any:
        calls["n"] += 1
        return await real(payload)

    webhook.app.state.webhook_breaker.trip()
    monkeypatch.setattr(webhook.app.state.ingestor, "ingest", counting)
    assert (await webhook.post(_payload(channels, wamid()))).status_code == 200
    assert calls["n"] == 0  # straight to the buffer: no 5 s connect timeout per message
    assert await buffer.depth(webhook.app.state.redis) == 1


async def test_postgres_and_redis_both_down_answers_500_so_meta_retries(
    webhook: WebhookHarness, channels: ChannelPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_: Any, **__: Any) -> Any:
        raise RedisConnectionError("down")

    webhook.app.state.webhook_breaker.trip()
    monkeypatch.setattr(webhook.app.state.redis, "lpush", down)
    assert (await webhook.post(_payload(channels, wamid()))).status_code == 500


async def test_a_body_left_mid_drain_by_a_crash_is_replayed(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    redis = webhook.app.state.redis
    w = wamid()
    import json

    await redis.lpush(buffer.PROCESSING_KEY, json.dumps(_payload(channels, w)).encode())
    report = await buffer.drain(redis, webhook.app.state.ingestor)
    assert report.replayed == 1
    assert await _messages(webhook.db, channels.t.a) == [w]


def test_breaker_closes_again() -> None:
    b = Breaker(0.0)
    b.trip()
    assert not b.is_open
    assert not Breaker().is_open


async def test_inbound_jobs_wait_out_the_debounce_in_the_queue(
    webhook: WebhookHarness, channels: ChannelPair, settings: Settings
) -> None:
    """The turn debounce is spent queued (_defer_by), not asleep in a worker slot: a sleeping job
    held a slot for 2 s and capped a worker at max_jobs / 2.5 turns a second (Phase 6 load test)."""
    jobs = RecordingEnqueuer()
    redis = webhook.app.state.redis
    webhook.app.state.ingestor = WebhookIngestor(
        webhook.db,
        redis,
        TenantRouter(webhook.db, redis),
        jobs,
        dedup_ttl_s=3600,
        inbound_defer_s=2.0,
    )
    w = wamid()
    assert (await webhook.post(_payload(channels, w))).status_code == 200
    assert jobs.deferred == {f"in:{w}": 2.0}

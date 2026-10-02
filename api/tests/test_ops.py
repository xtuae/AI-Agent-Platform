"""Phase 6: alerts to HMH Labz's WhatsApp, the watchdog's checks, recovery jobs, and encrypted
backups that are actually restored and verified."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pyrage
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update

from api import alerts
from api.alerts import send as alert_send
from api.alerts import watchdog as wd
from api.config import Settings
from api.core.crypto import encrypt_secret
from api.db.models import (
    Conversation,
    Customer,
    Message,
    Tenant,
    TenantChannel,
    TenantSettings,
    UsageDaily,
)
from api.db.session import Database
from api.metering import record_usage
from api.modules.campaigns import jobs as campaign_jobs
from api.ops import backup as bk
from api.ops.jobs import nightly_backup
from api.tests.conftest import WebhookHarness, make_tenant, meta_id, wamid
from api.tests.test_campaigns import FakeMeta, make_shop, worker_ctx
from api.tests.test_llm_router import Graph, chat, ok, router
from api.workers.jobs import recovery


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    r = Redis.from_url(str(settings.redis_url))
    await r.delete(alerts.OUTBOX)
    for key in await r.keys(alerts.COOLDOWN_PREFIX + "*"):
        await r.delete(key)
    for key in await r.keys("metrics:*"):
        await r.delete(key)
    yield r
    await r.aclose()


class FakeArq:
    """Redis plus arq's enqueue_job, recording what was queued and where."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self.jobs: list[tuple[str, tuple[Any, ...], str | None, str | None]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._redis, name)

    async def enqueue_job(
        self,
        fn: str,
        *args: Any,
        _job_id: str | None = None,
        _queue_name: str | None = None,
        _defer_by: float | None = None,
    ) -> object | None:
        if any(j[2] == _job_id for j in self.jobs):
            return None  # arq refuses a duplicate job id
        self.jobs.append((fn, args, _job_id, _queue_name))
        return object()


# ---------------------------------------------------------------- alerts


async def test_an_alert_fires_once_per_cooldown(redis: Redis) -> None:
    assert await alerts.raise_alert(redis, "x", "Something broke.", cooldown_s=60)
    assert not await alerts.raise_alert(redis, "x", "Something broke.", cooldown_s=60)
    assert await alerts.raise_alert(redis, "y", "Something else.", cooldown_s=60)
    assert [a["key"] for a in await alerts.pending(redis)] == ["x", "y"]


async def _ops_tenant(db: Database) -> tuple[uuid.UUID, str]:
    tid = await make_tenant(db, "ops")
    async with db.platform_session() as s:
        slug = await s.scalar(select(Tenant.slug).where(Tenant.id == tid))
        s.add(
            TenantChannel(
                tenant_id=tid, phone_number_id=meta_id(), access_token_encrypted=encrypt_secret("t")
            )
        )
    assert slug is not None
    return tid, slug


async def test_alerts_go_out_as_a_template_and_are_metered(
    db: Database, settings: Settings, redis: Redis
) -> None:
    tid, slug = await _ops_tenant(db)
    meta = FakeMeta()
    ctx = worker_ctx(
        db,
        settings.model_copy(update={"alert_tenant_slug": slug, "alert_to": "+971500000777"}),
        meta,
    )
    ctx["redis"] = redis
    await alerts.raise_alert(redis, "queue_depth", "Job queue depth 900 (limit 500).")
    out = await alert_send.send_alerts(ctx)
    assert out["sent"] == 1
    body = meta.sent[-1]
    assert body["to"] == "971500000777"
    assert body["template"]["name"] == "platform_alert"
    assert body["template"]["components"][0]["parameters"][0]["text"].startswith("Job queue")
    async with db.tenant_session(tid) as s:
        u = await s.scalar(select(UsageDaily))
    assert u is not None
    assert (u.msgs_out, u.utility_count) == (1, 1)
    assert await alerts.pending(redis) == []


async def test_meta_refusing_an_alert_keeps_it_for_the_next_minute(
    db: Database, settings: Settings, redis: Redis
) -> None:
    _, slug = await _ops_tenant(db)
    meta = FakeMeta(fail_to={"971500000777": 131026})
    ctx = worker_ctx(
        db,
        settings.model_copy(update={"alert_tenant_slug": slug, "alert_to": "971500000777"}),
        meta,
    )
    ctx["redis"] = redis
    await alerts.raise_alert(redis, "a", "One.")
    assert (await alert_send.send_alerts(ctx))["failed"] == 1
    assert [a["tries"] for a in await alerts.pending(redis)] == [1]


async def test_unconfigured_alerts_are_only_logged(
    db: Database, settings: Settings, redis: Redis
) -> None:
    ctx = worker_ctx(db, settings.model_copy(update={"alert_to": None}), FakeMeta())
    ctx["redis"] = redis
    await alerts.raise_alert(redis, "a", "One.")
    assert (await alert_send.send_alerts(ctx))["dropped"] == 1


# ---------------------------------------------------------------- watchdog


def _ctx(db: Database, settings: Settings, redis: Redis, **update: Any) -> dict[str, Any]:
    return {
        "db": db,
        "redis": redis,
        "settings": settings.model_copy(update=update),
        "clock": lambda: datetime.now(UTC),  # webhook counters are keyed by the real minute
    }


async def _keys(redis: Redis) -> list[str]:
    return [a["key"] for a in await alerts.pending(redis)]


async def test_webhook_5xx_rate(db: Database, settings: Settings, redis: Redis) -> None:
    for _ in range(19):
        await wd.count_webhook(redis, 200)
    await wd.count_webhook(redis, 500)
    await wd.watchdog(_ctx(db, settings, redis, disk_check_paths=[]))
    assert "webhook_5xx" not in await _keys(redis)  # 1 in 20: not over 5%
    await wd.count_webhook(redis, 503)
    await wd.watchdog(_ctx(db, settings, redis, disk_check_paths=[]))
    assert "webhook_5xx" in await _keys(redis)


async def test_webhook_responses_are_counted(webhook: WebhookHarness) -> None:
    redis = webhook.app.state.redis
    key = wd.WEBHOOK_METRIC + str(int(time.time()) // 60) + ":total"
    before = int(await redis.get(key) or 0)
    await webhook.post({"object": "whatsapp_business_account", "entry": []}, signature="sha256=bad")
    assert int(await redis.get(key) or 0) == before + 1


async def test_queue_depth_llm_disk_backup_and_buffer(
    db: Database, settings: Settings, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = "test:arq:queue:" + uuid.uuid4().hex
    await redis.zadd(queue, {f"job{i}": i for i in range(501)})
    await wd.note_llm(redis, "failover")
    await redis.delete(wd.BACKUP_OK)
    await redis.delete("webhook:buffer", "webhook:buffer:processing")
    await redis.lpush("webhook:buffer", b"{}")  # type: ignore[misc]

    class Usage:
        total, used = 100, 91

    monkeypatch.setattr("api.alerts.watchdog.shutil.disk_usage", lambda _p: Usage())
    await wd.watchdog(
        _ctx(
            db, settings, redis, arq_queue_name=queue, disk_check_paths=["/data"], backup_bucket="b"
        )
    )
    keys = await _keys(redis)
    for expected in (
        "queue_depth",
        "llm_failover",
        "disk:/data",
        "backup_stale",
        "webhook_buffering",
    ):
        assert expected in keys
    await redis.delete(queue, "webhook:buffer")


async def test_llm_router_reports_failover_and_outage() -> None:
    events: list[str] = []

    async def note(name: str) -> None:
        events.append(name)

    r = router(Graph(httpx.Response(500), httpx.Response(500), httpx.Response(500), ok("hi")), [])
    r._on_event = note
    await chat(r)
    assert events == ["failover"]


async def test_tenant_at_80_percent_of_cap(db: Database, settings: Settings, redis: Redis) -> None:
    tid = await make_tenant(db, "cap")
    async with db.platform_session() as s:
        await s.execute(
            update(Tenant).where(Tenant.id == tid).values(status="active", name="Cap Co")
        )
        s.add(TenantSettings(tenant_id=tid, monthly_message_cap_aed=Decimal("10")))
    now = datetime.now(UTC)
    async with db.tenant_session(tid) as s:
        await record_usage(
            s, tid, at=now, msgs_out=1, category="marketing", meta_cost_aed=Decimal("8.1")
        )
    assert await wd.tenant_caps(redis, db, now) >= 1
    texts = [a["text"] for a in await alerts.pending(redis) if a["key"].startswith(f"cap80:{tid}")]
    assert texts == ["Cap Co is at 81% of its AED 10.00 monthly message cap."]
    await wd.tenant_caps(redis, db, now)  # once a month, not every 15 minutes
    assert len([a for a in await alerts.pending(redis) if a["key"].startswith(f"cap80:{tid}")]) == 1


async def test_quality_drop_alerts(db: Database, settings: Settings, redis: Redis) -> None:
    shop = await make_shop(db, opted_in=1, label="qual")
    async with db.platform_session() as s:
        await s.execute(
            update(TenantChannel)
            .where(TenantChannel.id == shop.channel.id)
            .values(quality_rating="GREEN")
        )
    ctx = worker_ctx(db, settings, FakeMeta(quality={shop.channel.phone_number_id: "YELLOW"}))
    ctx["redis"] = redis
    await campaign_jobs.refresh_quality(ctx, str(shop.tenant))
    assert [a["text"] for a in await alerts.pending(redis) if a["key"].startswith("quality:")] == [
        f"Quality rating for a number ({str(shop.tenant)[:8]}) changed GREEN → YELLOW."
    ]


# ---------------------------------------------------------------- recovery


async def test_stranded_messages_are_requeued_once(
    db: Database, settings: Settings, redis: Redis
) -> None:
    tid = await make_tenant(db, "stranded")
    async with db.platform_session() as s:
        await s.execute(update(Tenant).where(Tenant.id == tid).values(status="active"))
        ch = TenantChannel(tenant_id=tid, phone_number_id=meta_id())
        s.add(ch)
    now = datetime.now(UTC)
    ids: dict[str, str] = {}
    async with db.tenant_session(tid) as s:
        for label, age, state in (
            ("stuck", timedelta(minutes=10), "open"),
            ("fresh", timedelta(seconds=30), "open"),
            ("human", timedelta(minutes=10), "awaiting_human"),
            ("ancient", timedelta(days=2), "open"),
        ):
            c = Customer(wa_id=f"97150{uuid.uuid4().int % 10**7:07d}")
            s.add(c)
            await s.flush()
            conv = Conversation(customer_id=c.id, channel_id=ch.id, state=state)
            s.add(conv)
            await s.flush()
            w = wamid()
            s.add(Message(conversation_id=conv.id, wamid=w, direction="in", msg_type="text",
                          body="hi", status="received", created_at=now - age))  # fmt: skip
            ids[label] = w
    arq = FakeArq(redis)
    ctx = {"db": db, "redis": arq, "clock": lambda: now}
    out = await recovery.requeue_stranded(ctx)
    mine = [j for j in arq.jobs if j[1][0] == str(tid)]
    assert [j[2] for j in mine] == [f"in:{ids['stuck']}"]
    assert mine[0][3] == settings.arq_queue_name  # the worker's queue, not the scheduler's
    assert out["requeued"] >= 1
    await recovery.requeue_stranded(ctx)
    assert len([j for j in arq.jobs if j[1][0] == str(tid)]) == 1


async def test_drain_job_uses_the_main_queue(webhook: WebhookHarness, settings: Settings) -> None:
    from api.tests.conftest import entry, envelope, messages_change, text_message
    from api.tests.test_degradation import _messages

    redis = webhook.app.state.redis
    tid = await make_tenant(webhook.db, "drain")
    pnid, waba = meta_id(), meta_id()
    async with webhook.db.platform_session() as s:
        s.add(TenantChannel(tenant_id=tid, phone_number_id=pnid, waba_id=waba))
    w = wamid()
    body = envelope(entry(waba, messages_change(pnid, messages=[text_message("971501231234", w)])))
    await redis.lpush("webhook:buffer", json.dumps(body).encode())
    arq = FakeArq(redis)
    out = await recovery.drain_webhook_buffer({"db": webhook.db, "redis": arq})
    assert out == {"replayed": 1, "remaining": 0}
    assert await _messages(webhook.db, tid) == [w]
    assert [(j[0], j[3]) for j in arq.jobs] == [("handle_inbound_message", settings.arq_queue_name)]


# ---------------------------------------------------------------- backup


def _superuser_dsn(settings: Settings) -> str:
    if url := os.environ.get("TEST_SUPERUSER_URL"):  # CI: the service container's superuser
        return url
    h = settings.database_url.hosts()[0]
    path = (settings.database_url.path or "/").lstrip("/")
    return f"postgresql://postgres@{h['host']}:{h['port']}/{path}"


@pytest.fixture
async def superuser(settings: Settings) -> str:
    dsn = _superuser_dsn(settings)
    try:
        conn = await asyncpg.connect(dsn, timeout=3)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no superuser connection for backup tests: {type(exc).__name__}")
    await conn.close()
    return dsn


async def _drop(dsn: str, name: str) -> None:
    conn = await asyncpg.connect(bk.with_database(dsn, "postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


async def test_backup_restores_into_a_clean_database_with_matching_row_counts(
    db: Database, superuser: str, tmp_path: Path
) -> None:
    """The Phase 6 acceptance test: encrypted backup → restore into a fresh database → every
    table's row count matches. Tenant tables included (RLS is forced: a wrong role dumps them
    empty, and this test would see it)."""
    tid = await make_tenant(db, "backup")
    async with db.tenant_session(tid) as s:
        s.add(Customer(wa_id="971500000123", name="Backed Up"))
    ident = pyrage.x25519.Identity.generate()
    storage = bk.LocalStorage(tmp_path / "bucket")
    result = await bk.backup(
        dsn=superuser,
        recipient=str(ident.to_public()),
        storage=storage,
        prefix="t",
        retention_days=30,
    )
    assert result.rows > 0
    blob = (tmp_path / "bucket" / (result.key + bk.SUFFIX)).read_bytes()
    assert b"PGDMP" not in blob[:4096]  # encrypted at rest
    assert b"Backed Up" not in blob
    manifest = json.loads((tmp_path / "bucket" / (result.key + bk.MANIFEST)).read_text())
    assert manifest["counts"]["customers"] >= 1

    target = "restore_" + uuid.uuid4().hex[:10]
    try:
        r = await bk.restore(
            key=bk.latest(storage, "t"),
            identity=str(ident),
            storage=storage,
            admin_dsn=superuser,
            target_db=target,
            owners=False,
        )
        assert r.mismatches == {}
        assert r.tables == result.tables
        conn = await asyncpg.connect(bk.with_database(superuser, target))
        try:
            assert (
                await conn.fetchval("SELECT name FROM customers WHERE wa_id = '971500000123'")
                == "Backed Up"
            )
        finally:
            await conn.close()
        with pytest.raises(bk.BackupError, match="already exists"):
            await bk.restore(key=result.key, identity=str(ident), storage=storage,
                             admin_dsn=superuser, target_db=target)  # fmt: skip
    finally:
        await _drop(superuser, target)

    wrong = pyrage.x25519.Identity.generate()
    with pytest.raises(bk.BackupError, match="cannot decrypt"):
        await bk.restore(
            key=result.key,
            identity=str(wrong),
            storage=storage,
            admin_dsn=superuser,
            target_db="never_" + uuid.uuid4().hex[:8],
        )


async def test_retention_removes_backups_older_than_the_window(
    superuser: str, tmp_path: Path
) -> None:
    ident = pyrage.x25519.Identity.generate()
    storage = bk.LocalStorage(tmp_path)
    old = await bk.backup(
        dsn=superuser, recipient=str(ident.to_public()), storage=storage, prefix="r",
        retention_days=30, now=datetime(2026, 8, 1, tzinfo=UTC),
    )  # fmt: skip
    for suffix in (bk.SUFFIX, bk.MANIFEST):
        p = tmp_path / (old.key + suffix)
        stamp = datetime(2026, 8, 1, tzinfo=UTC).timestamp()
        os.utime(p, (stamp, stamp))
    new = await bk.backup(
        dsn=superuser,
        recipient=str(ident.to_public()),
        storage=storage,
        prefix="r",
        retention_days=30,
    )
    assert sorted(new.deleted) == sorted([old.key + bk.SUFFIX, old.key + bk.MANIFEST])
    assert bk.latest(storage, "r") == new.key


async def test_a_dump_as_the_app_role_would_be_empty_so_backups_need_bypassrls(
    settings: Settings, superuser: str
) -> None:
    """Why BACKUP_DATABASE_URL is its own role: pg_dump as the RLS-bound app role refuses."""
    with pytest.raises(bk.BackupError):
        await bk.backup(
            dsn=str(settings.database_url),
            recipient=str(pyrage.x25519.Identity.generate().to_public()),
            storage=bk.LocalStorage(Path("/nonexistent-never-written")),
            prefix="x",
            retention_days=1,
        )  # fmt: skip


async def test_failed_nightly_backup_alerts(settings: Settings, redis: Redis) -> None:
    ctx = {
        "redis": redis,
        "settings": settings.model_copy(update={"backup_bucket": "b", "backup_database_url": None}),
    }
    assert (await nightly_backup(ctx))["status"] == "failed"
    assert "backup_failed" in await _keys(redis)


def test_bad_recipient_key_is_refused() -> None:
    with pytest.raises(bk.BackupError, match="age public key"):
        bk._keypair_check("not-a-key")


# ---------------------------------------------------------------- DPA + token rotation


async def test_dpa_describes_what_the_tenant_has_switched_on(db: Database) -> None:
    from api.scripts import dpa

    tid = await make_tenant(db, "dpa", modules=("appointments", "listings", "campaigns"))
    async with db.platform_session() as s:
        slug = await s.scalar(select(Tenant.slug).where(Tenant.id == tid))
    assert slug is not None
    text = await dpa.render(slug)
    assert "**Appointments**" in text
    assert "**Orders" not in text  # not switched on, not described
    assert "Meta Platforms Ireland" in text
    with pytest.raises(ValueError, match="no tenant"):
        await dpa.render("no-such-tenant")


async def test_token_rotation_is_checked_with_meta_and_stored_encrypted(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from api.core.crypto import decrypt_secret
    from api.meta.client import MetaAPIError, MetaClient, PhoneStatus
    from api.scripts import channel_token

    tid = await make_tenant(db, "rotate")
    pnid = meta_id()
    async with db.platform_session() as s:
        s.add(
            TenantChannel(
                tenant_id=tid, phone_number_id=pnid, access_token_encrypted=encrypt_secret("old")
            )
        )

    async def ok(self: MetaClient) -> PhoneStatus:
        return PhoneStatus(quality_rating="GREEN", messaging_limit_tier="TIER_1K")

    async def bad(self: MetaClient) -> PhoneStatus:
        raise MetaAPIError(401, "invalid token")

    args = argparse.Namespace(cmd="set", phone_number_id=pnid, expires=None)
    monkeypatch.setattr(MetaClient, "phone_status", bad)
    assert await channel_token._run(args, "rejected-token") == 1
    monkeypatch.setattr(MetaClient, "phone_status", ok)
    assert await channel_token._run(args, "new-token") == 0
    async with db.platform_session() as s:
        ch = await s.scalar(select(TenantChannel).where(TenantChannel.phone_number_id == pnid))
    assert ch is not None
    assert ch.access_token_encrypted is not None
    assert decrypt_secret(ch.access_token_encrypted) == "new-token"

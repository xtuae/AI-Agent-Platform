"""Tenant routing by phone_number_id — the highest-risk code in the platform (constraint 2).

Every test that routes a message also asserts the OTHER tenant saw nothing.
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from api.config import get_settings
from api.db.models import (
    Conversation,
    Customer,
    Message,
    Tenant,
    TenantChannel,
    UsageDaily,
    WebhookEvent,
)
from api.db.session import Database
from api.metering import usage_day
from api.tests.conftest import (
    ChannelPair,
    WebhookHarness,
    entry,
    envelope,
    make_channel,
    make_tenant,
    messages_change,
    meta_id,
    text_message,
    wamid,
)
from api.webhooks import ingest as ingest_module
from api.webhooks.router import TenantRouter
from api.webhooks.signature import compute_signature

FIXTURES = Path(__file__).parent / "fixtures"
SENDER = "971501234567"


async def messages_for(db: Database, tenant_id: uuid.UUID) -> list[Message]:
    async with db.tenant_session(tenant_id) as s:
        return list((await s.scalars(select(Message).order_by(Message.created_at))).all())


async def events_for(db: Database, tenant_id: uuid.UUID) -> list[WebhookEvent]:
    async with db.tenant_session(tenant_id) as s:
        return list((await s.scalars(select(WebhookEvent))).all())


async def usage_in(db: Database, tenant_id: uuid.UUID) -> int:
    async with db.tenant_session(tenant_id) as s:
        row = await s.get(UsageDaily, (tenant_id, usage_day()))
        return row.msgs_in if row else 0


# ================================================================ 1. valid single entry


async def test_valid_single_entry_lands_under_correct_tenant(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    w = wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(
                channels.a.phone_number_id,
                messages=[text_message(SENDER, w, "5 bottles please")],
                contacts=[{"wa_id": SENDER, "profile": {"name": "Ahmed"}}],
            ),
        )
    )
    r = await webhook.post(payload)
    assert r.status_code == 200

    rows = await messages_for(webhook.db, channels.t.a)
    assert len(rows) == 1
    msg = rows[0]
    assert (msg.wamid, msg.tenant_id, msg.direction, msg.status) == (
        w,
        channels.t.a,
        "in",
        "received",
    )
    assert msg.body == "5 bottles please"

    async with webhook.db.tenant_session(channels.t.a) as s:
        conv = await s.get(Conversation, msg.conversation_id)
        assert conv is not None
        assert conv.channel_id == channels.a.id
        assert conv.service_window_expires_at is not None
        cust = await s.get(Customer, conv.customer_id)
        assert cust is not None
        assert (cust.wa_id, cust.name, cust.source, cust.opt_in_status) == (
            SENDER,
            "Ahmed",
            "inbound",
            "pending",
        )

    assert await usage_in(webhook.db, channels.t.a) == 1
    assert len(await events_for(webhook.db, channels.t.a)) == 1
    assert webhook.jobs.jobs == [
        ("handle_inbound_message", (str(channels.t.a), str(msg.id)), f"in:{w}")
    ]
    # The other tenant saw nothing at all.
    assert await messages_for(webhook.db, channels.t.b) == []
    assert await events_for(webhook.db, channels.t.b) == []
    assert await usage_in(webhook.db, channels.t.b) == 0


# ================================================================ 2. multiple entries


async def test_multiple_entries_route_each_to_its_own_tenant(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    wa, wb = wamid(), wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(
                channels.a.phone_number_id, messages=[text_message(SENDER, wa, "for A")]
            ),
        ),
        entry(
            channels.b.waba_id or "",
            messages_change(
                channels.b.phone_number_id, messages=[text_message(SENDER, wb, "for B")]
            ),
        ),
    )
    assert (await webhook.post(payload)).status_code == 200

    a_rows, b_rows = (
        await messages_for(webhook.db, channels.t.a),
        await messages_for(webhook.db, channels.t.b),
    )
    assert [(m.wamid, m.body) for m in a_rows] == [(wa, "for A")]
    assert [(m.wamid, m.body) for m in b_rows] == [(wb, "for B")]
    assert {(j[1][0], j[2]) for j in webhook.jobs.jobs} == {
        (str(channels.t.a), f"in:{wa}"),
        (str(channels.t.b), f"in:{wb}"),
    }


async def test_changes_in_one_entry_route_on_their_own_metadata(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    """Never route a change using entry-level or first-change data."""
    wa, wb = wamid(), wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",  # entry says A's WABA …
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, wa)]),
            messages_change(
                channels.b.phone_number_id, messages=[text_message(SENDER, wb)]
            ),  # … but this change is B's
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    assert [m.wamid for m in await messages_for(webhook.db, channels.t.a)] == [wa]
    assert [m.wamid for m in await messages_for(webhook.db, channels.t.b)] == [wb]


async def test_several_messages_in_one_change(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    w1, w2 = wamid(), wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(
                channels.a.phone_number_id,
                messages=[text_message(SENDER, w1, "one"), text_message("971509998877", w2, "two")],
            ),
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    rows = await messages_for(webhook.db, channels.t.a)
    assert {m.wamid for m in rows} == {w1, w2}
    assert len({m.conversation_id for m in rows}) == 2  # two customers, two conversations
    assert await usage_in(webhook.db, channels.t.a) == 2


# ================================================================ 3-6. unroutable


async def test_unknown_phone_number_id_is_dropped_with_200(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    unknown, w_ok = meta_id(), wamid()
    payload = envelope(
        entry(meta_id(), messages_change(unknown, messages=[text_message(SENDER, wamid())])),
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, w_ok)]),
        ),
    )
    r = await webhook.post(payload)
    assert r.status_code == 200  # never 5xx: Meta would retry forever
    assert [m.wamid for m in await messages_for(webhook.db, channels.t.a)] == [w_ok]
    assert await messages_for(webhook.db, channels.t.b) == []
    assert len(webhook.jobs.jobs) == 1


async def test_inactive_channel_is_dropped(
    webhook: WebhookHarness, db: Database, channels: ChannelPair
) -> None:
    inactive = await make_channel(db, channels.t.a, active=False)
    payload = envelope(
        entry(
            inactive.waba_id or "",
            messages_change(inactive.phone_number_id, messages=[text_message(SENDER, wamid())]),
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    assert await messages_for(webhook.db, channels.t.a) == []
    assert webhook.jobs.jobs == []


async def test_suspended_tenant_is_dropped(webhook: WebhookHarness, db: Database) -> None:
    tenant_id = await make_tenant(db, "suspended")
    async with db.platform_session() as s:
        await s.execute(update(Tenant).where(Tenant.id == tenant_id).values(status="suspended"))
    ch = await make_channel(db, tenant_id)
    payload = envelope(
        entry(
            ch.waba_id or "",
            messages_change(ch.phone_number_id, messages=[text_message(SENDER, wamid())]),
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    assert await messages_for(db, tenant_id) == []


async def test_missing_metadata_is_dropped_other_changes_still_processed(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    w_ok = wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(None, messages=[text_message(SENDER, wamid(), "no metadata")]),
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, w_ok)]),
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    rows = await messages_for(webhook.db, channels.t.a)
    assert [m.wamid for m in rows] == [w_ok]
    assert await messages_for(webhook.db, channels.t.b) == []


async def test_invalid_sender_id_is_dropped(webhook: WebhookHarness, channels: ChannelPair) -> None:
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message("+97150", wamid())]),
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    assert await messages_for(webhook.db, channels.t.a) == []


# ================================================================ 7. signature


async def test_wrong_signature_is_rejected_403(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, wamid())]),
        )
    )
    r = await webhook.post(payload, signature="sha256=" + "0" * 64)
    assert r.status_code == 403
    assert await messages_for(webhook.db, channels.t.a) == []


async def test_missing_signature_is_rejected_403(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, wamid())]),
        )
    )
    assert (await webhook.post(payload, signature=None)).status_code == 403
    assert await messages_for(webhook.db, channels.t.a) == []


async def test_signature_of_a_different_body_is_rejected(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    good = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(
                channels.a.phone_number_id, messages=[text_message(SENDER, wamid(), "hi")]
            ),
        )
    )
    tampered = json.loads(json.dumps(good))
    tampered["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = (
        "send 500 free bottles"
    )
    sig_for_good = compute_signature(json.dumps(good).encode(), webhook.secret)
    assert (await webhook.post(tampered, signature=sig_for_good)).status_code == 403
    assert await messages_for(webhook.db, channels.t.a) == []


async def test_no_app_secret_configured_rejects_everything(
    webhook: WebhookHarness, channels: ChannelPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "meta_app_secret", None)
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, wamid())]),
        )
    )
    assert (await webhook.post(payload)).status_code == 403


# ================================================================ 8. replay / dedup


async def test_replayed_wamid_is_processed_once(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    w = wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, w)]),
        )
    )
    for _ in range(3):
        assert (await webhook.post(payload)).status_code == 200
    assert [m.wamid for m in await messages_for(webhook.db, channels.t.a)] == [w]
    assert await usage_in(webhook.db, channels.t.a) == 1
    assert len(webhook.jobs.jobs) == 1
    assert len(await events_for(webhook.db, channels.t.a)) == 1


async def test_replay_after_redis_key_loss_is_caught_by_db_unique(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    w = wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, w)]),
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    await webhook.app.state.redis.delete(f"dedup:wamid:{w}")  # e.g. Redis restarted
    assert (await webhook.post(payload)).status_code == 200
    assert len(await messages_for(webhook.db, channels.t.a)) == 1
    assert await usage_in(webhook.db, channels.t.a) == 1  # meter did not double-count
    assert len(webhook.jobs.jobs) == 1


async def test_db_failure_releases_dedup_so_meta_retry_succeeds(
    webhook: WebhookHarness, channels: ChannelPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = ingest_module._persist_inbound
    calls = {"n": 0}

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("INSERT", {}, Exception("connection reset"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(ingest_module, "_persist_inbound", flaky)
    w = wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, w)]),
        )
    )
    assert (await webhook.post(payload)).status_code == 500  # Meta will retry
    assert await messages_for(webhook.db, channels.t.a) == []
    assert (
        await webhook.post(payload)
    ).status_code == 200  # the retry is NOT treated as a duplicate
    assert [m.wamid for m in await messages_for(webhook.db, channels.t.a)] == [w]


# ================================================================ 9. malformed


async def test_malformed_json_returns_200_and_stores_nothing(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    r = await webhook.post(b'{"object": "whatsapp_business_account", "entry": [ {broken')
    assert r.status_code == 200
    assert webhook.jobs.jobs == []


async def test_non_whatsapp_object_is_ignored(webhook: WebhookHarness) -> None:
    assert (await webhook.post({"object": "page", "entry": []})).status_code == 200
    assert webhook.jobs.jobs == []


async def test_malformed_change_does_not_block_valid_ones(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    w_ok = wamid()
    bad = messages_change(
        channels.a.phone_number_id, messages=[{"id": "x"}]
    )  # no from/type/timestamp
    good = messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, w_ok)])
    assert (
        await webhook.post(envelope(entry(channels.a.waba_id or "", bad, good)))
    ).status_code == 200
    assert [m.wamid for m in await messages_for(webhook.db, channels.t.a)] == [w_ok]


# ================================================================ 10. recorded payload fixture


async def test_meta_payload_fixture_lands_with_correct_tenant(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    w = wamid()
    raw = (FIXTURES / "meta_text_message.json").read_text()
    raw = (
        raw.replace("__WABA_ID__", channels.b.waba_id or "")
        .replace("__PHONE_NUMBER_ID__", channels.b.phone_number_id)
        .replace("__WAMID__", w)
    )
    assert (await webhook.post(raw.encode())).status_code == 200
    rows = await messages_for(webhook.db, channels.t.b)
    assert [(m.wamid, m.tenant_id, m.body) for m in rows] == [(w, channels.t.b, "5 bottles please")]
    assert await messages_for(webhook.db, channels.t.a) == []


# ================================================================ conversation / customer behaviour


async def test_same_customer_reuses_open_conversation(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    for body in ("hi", "5 bottles please"):
        payload = envelope(
            entry(
                channels.a.waba_id or "",
                messages_change(
                    channels.a.phone_number_id, messages=[text_message(SENDER, wamid(), body)]
                ),
            )
        )
        assert (await webhook.post(payload)).status_code == 200
    rows = await messages_for(webhook.db, channels.t.a)
    assert len(rows) == 2
    assert rows[0].conversation_id == rows[1].conversation_id


async def test_same_wa_id_under_two_tenants_is_two_customers(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, wamid())]),
        ),
        entry(
            channels.b.waba_id or "",
            messages_change(channels.b.phone_number_id, messages=[text_message(SENDER, wamid())]),
        ),
    )
    assert (await webhook.post(payload)).status_code == 200
    for tenant_id in (channels.t.a, channels.t.b):
        async with webhook.db.tenant_session(tenant_id) as s:
            n = await s.scalar(
                select(func.count()).select_from(Customer).where(Customer.wa_id == SENDER)
            )
            assert n == 1


# ================================================================ statuses + template updates


async def test_status_change_is_stored_and_enqueued_once(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    status = {
        "id": wamid(),
        "status": "delivered",
        "timestamp": str(int(time.time())),
        "recipient_id": SENDER,
    }
    payload = envelope(
        entry(
            channels.a.waba_id or "", messages_change(channels.a.phone_number_id, statuses=[status])
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    assert (await webhook.post(payload)).status_code == 200  # replay
    events = await events_for(webhook.db, channels.t.a)
    assert [e.kind for e in events] == ["statuses"]
    assert [j[0] for j in webhook.jobs.jobs] == ["apply_status_event"]
    assert await events_for(webhook.db, channels.t.b) == []


async def test_template_status_update_routes_by_waba(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    change = {
        "field": "message_template_status_update",
        "value": {
            "event": "APPROVED",
            "message_template_id": 123456,
            "message_template_name": "promo",
            "message_template_language": "en",
        },
    }
    assert (
        await webhook.post(envelope(entry(channels.b.waba_id or "", change)))
    ).status_code == 200
    assert [e.kind for e in await events_for(webhook.db, channels.t.b)] == ["template_status"]
    assert await events_for(webhook.db, channels.t.a) == []
    assert [j[0] for j in webhook.jobs.jobs] == ["apply_template_status_event"]


async def test_waba_shared_by_two_tenants_is_not_routed(
    webhook: WebhookHarness, db: Database, channels: ChannelPair
) -> None:
    shared = meta_id()
    await make_channel(db, channels.t.a, waba_id=shared)
    await make_channel(db, channels.t.b, waba_id=shared)
    change = {
        "field": "message_template_status_update",
        "value": {"event": "APPROVED", "message_template_id": 1},
    }
    assert (await webhook.post(envelope(entry(shared, change)))).status_code == 200
    assert await events_for(db, channels.t.a) == []
    assert await events_for(db, channels.t.b) == []


# ================================================================ router unit tests (cache)


async def test_router_cache_serves_then_invalidates(db: Database, channels: ChannelPair) -> None:
    redis = Redis.from_url(str(get_settings().redis_url))
    try:
        router = TenantRouter(db, redis)
        first = await router.resolve(channels.a.phone_number_id)
        assert first is not None
        assert first.tenant_id == channels.t.a

        async with db.platform_session() as s:
            await s.execute(
                update(TenantChannel)
                .where(TenantChannel.id == channels.a.id)
                .values(is_active=False)
            )
        cached = await router.resolve(channels.a.phone_number_id)
        assert cached == first  # served from the 5-minute cache, by design

        await router.invalidate(phone_number_id=channels.a.phone_number_id)
        assert await router.resolve(channels.a.phone_number_id) is None
    finally:
        await redis.aclose()


async def test_router_falls_back_to_db_when_redis_is_down(
    db: Database, channels: ChannelPair
) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        route = await TenantRouter(db, dead).resolve(channels.b.phone_number_id)
        assert route is not None
        assert route.tenant_id == channels.t.b
    finally:
        await dead.aclose()


@pytest.mark.parametrize("bad", [None, "", "abc", "12 34", "123'; DROP TABLE tenants;--", "1" * 40])
async def test_router_rejects_malformed_ids_without_touching_db(
    db: Database, bad: str | None
) -> None:
    redis = Redis.from_url(str(get_settings().redis_url))
    try:
        assert await TenantRouter(db, redis).resolve(bad) is None
    finally:
        await redis.aclose()


# ================================================================ GET verification


async def test_verify_handshake(webhook: WebhookHarness) -> None:
    token = get_settings().meta_verify_token
    assert token is not None
    ok = await webhook.client.get(
        "/webhook/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": token.get_secret_value(),
            "hub.challenge": "1158201444",
        },
    )
    assert (ok.status_code, ok.text) == (200, "1158201444")
    bad = await webhook.client.get(
        "/webhook/meta",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"},
    )
    assert bad.status_code == 403
    xss = await webhook.client.get(
        "/webhook/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": token.get_secret_value(),
            "hub.challenge": "<script>",
        },
    )
    assert xss.status_code == 403


# ================================================================ latency (constraint 3)


async def test_handler_p95_under_200ms(webhook: WebhookHarness, channels: ChannelPair) -> None:
    """LLM and Graph API are not stubbed-slow — they are forbidden (no_network fixture)."""
    timings: list[float] = []
    for i in range(60):
        payload = envelope(
            entry(
                channels.a.waba_id or "",
                messages_change(
                    channels.a.phone_number_id,
                    messages=[text_message(f"97150{i:07d}", wamid(), "hello")],
                ),
            )
        )
        started = time.perf_counter()
        r = await webhook.post(payload)
        timings.append((time.perf_counter() - started) * 1000)
        assert r.status_code == 200
    warm = timings[5:]  # exclude pool warm-up
    p95 = statistics.quantiles(warm, n=20)[18]
    assert p95 < 200, f"p95={p95:.1f}ms"


# ================================================================ degraded dependencies


async def test_enqueue_failure_still_returns_200_and_keeps_the_row(
    webhook: WebhookHarness, channels: ChannelPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RedisConnectionError("queue down")

    monkeypatch.setattr(webhook.jobs, "enqueue_job", broken)
    w = wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, w)]),
        )
    )
    assert (await webhook.post(payload)).status_code == 200
    assert [m.wamid for m in await messages_for(webhook.db, channels.t.a)] == [w]


async def test_redis_down_for_dedup_falls_back_to_db_unique(
    webhook: WebhookHarness, channels: ChannelPair, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def down(*_a: Any, **_k: Any) -> Any:
        raise RedisConnectionError("redis down")

    monkeypatch.setattr(webhook.app.state.redis, "set", down)
    w = wamid()
    payload = envelope(
        entry(
            channels.a.waba_id or "",
            messages_change(channels.a.phone_number_id, messages=[text_message(SENDER, w)]),
        )
    )
    for _ in range(2):
        assert (await webhook.post(payload)).status_code == 200
    assert [m.wamid for m in await messages_for(webhook.db, channels.t.a)] == [w]
    assert await usage_in(webhook.db, channels.t.a) == 1


async def test_quality_update_is_stored_under_waba_tenant(
    webhook: WebhookHarness, channels: ChannelPair
) -> None:
    change = {
        "field": "phone_number_quality_update",
        "value": {
            "display_phone_number": "971500000000",
            "event": "FLAGGED",
            "current_limit": "TIER_1K",
        },
    }
    assert (
        await webhook.post(envelope(entry(channels.a.waba_id or "", change)))
    ).status_code == 200
    events = await events_for(webhook.db, channels.t.a)
    assert [(e.field, e.kind) for e in events] == [("phone_number_quality_update", "other")]
    assert webhook.jobs.jobs == []  # consumed by the Phase 4 quality guard
    assert await events_for(webhook.db, channels.t.b) == []

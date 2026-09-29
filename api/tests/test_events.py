"""Live updates: Postgres NOTIFY → broker → per-tenant SSE. Ids only, never bodies."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update

from api.api.v1.stream import event_stream
from api.auth.tokens import issue_access
from api.config import get_settings
from api.db.models import Conversation, Customer, Message
from api.events import QUEUE_MAX, RESYNC, ChangeEvent, EventBroker, Subscription, parse
from api.tests.conftest import DashHarness, TenantPair, make_channel, wamid


async def _connected(h: DashHarness) -> EventBroker:
    broker: EventBroker = h.app.state.events
    await asyncio.wait_for(broker.connected.wait(), 10)
    return broker


async def _next(sub: Subscription, wait_s: float = 3.0) -> ChangeEvent:
    return await asyncio.wait_for(sub.get(), wait_s)


async def _nothing(sub: Subscription, wait_s: float = 0.4) -> bool:
    try:
        await asyncio.wait_for(sub.get(), wait_s)
    except TimeoutError:
        return True
    return False


def test_parse_rejects_junk_and_unknown_tables() -> None:
    t = uuid.uuid4()
    ok = parse(json.dumps({"t": str(t), "e": "orders", "id": "x", "op": "insert", "p": "c"}))
    assert ok == (t, ChangeEvent("orders", "x", "insert", "c"))
    for bad in ("not json", "{}", json.dumps({"t": "nope", "e": "orders"})):
        assert parse(bad) is None
    assert parse(json.dumps({"t": str(t), "e": "tenant_users", "id": "x"})) is None


async def test_committed_changes_reach_only_their_tenant(
    dash: DashHarness, tenants: TenantPair
) -> None:
    broker = await _connected(dash)
    async with broker.subscribe(tenants.a) as sub_a, broker.subscribe(tenants.b) as sub_b:
        async with dash.db.tenant_session(tenants.a) as s:
            await s.execute(
                update(Customer).where(Customer.id == tenants.customer_a).values(area="Marina")
            )
        ev = await _next(sub_a)
        assert (ev.entity, ev.id, ev.op) == ("customers", str(tenants.customer_a), "update")
        assert await _nothing(sub_b)


async def test_rolled_back_changes_are_never_announced(
    dash: DashHarness, tenants: TenantPair
) -> None:
    broker = await _connected(dash)
    async with broker.subscribe(tenants.a) as sub:
        try:
            async with dash.db.tenant_session(tenants.a) as s:
                await s.execute(
                    update(Customer).where(Customer.id == tenants.customer_a).values(area="X")
                )
                raise RuntimeError("roll back")
        except RuntimeError:
            pass
        assert await _nothing(sub)


async def test_message_events_name_the_thread_but_carry_no_body(
    dash: DashHarness, tenants: TenantPair
) -> None:
    broker = await _connected(dash)
    channel = await make_channel(dash.db, tenants.a)
    async with dash.db.tenant_session(tenants.a) as s:
        conv = Conversation(customer_id=tenants.customer_a, channel_id=channel.id)
        s.add(conv)
        await s.flush()
        conv_id = conv.id
    async with broker.subscribe(tenants.a) as sub:
        async with dash.db.tenant_session(tenants.a) as s:
            s.add(
                Message(
                    conversation_id=conv_id,
                    wamid=wamid(),
                    direction="in",
                    msg_type="text",
                    body="my secret address is villa 12",
                )
            )
        ev = await _next(sub)
        assert ev.entity == "messages"
        assert ev.parent == str(conv_id)
        assert "villa" not in json.dumps(ev.as_dict())


async def test_a_slow_client_gets_resync_instead_of_unbounded_memory() -> None:
    sub = Subscription()
    for i in range(QUEUE_MAX + 1):
        sub.push(ChangeEvent("orders", str(i)))
    assert await sub.get() == RESYNC  # the backlog was dropped for one "refetch everything"
    assert sub._queue.empty()


async def test_event_stream_frames_heartbeat_and_expiry() -> None:
    sub = Subscription()
    sub.push(ChangeEvent("orders", "o1", "insert", "c1"))
    chunks: list[str] = []

    async def connected() -> bool:
        return False

    async for chunk in event_stream(
        sub,
        expires_at=datetime.now(UTC) + timedelta(seconds=0.35),
        heartbeat_s=0.1,
        is_disconnected=connected,
    ):
        chunks.append(chunk)
    text = "".join(chunks)
    assert text.startswith("retry: 5000\n\n")
    assert 'event: change\ndata: {"entity":"orders","id":"o1","op":"insert","parent":"c1"}' in text
    assert ": ping" in text
    assert text.endswith("event: expired\ndata: {}\n\n")


async def test_event_stream_stops_when_the_client_leaves() -> None:
    async def gone() -> bool:
        return True

    chunks = [
        c
        async for c in event_stream(
            Subscription(),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            heartbeat_s=10,
            is_disconnected=gone,
        )
    ]
    assert len(chunks) == 2  # retry + ready, then out


async def stream_events(
    h: DashHarness,
    tenant: uuid.UUID,
    other_tenant: uuid.UUID,
    customer: uuid.UUID,
    other_customer: uuid.UUID,
) -> list[dict[str, Any]]:
    """Open /api/v1/stream as `tenant` with a ~2 s token, change a customer in each tenant while it
    is open, and return the change events the stream delivered."""
    broker = await _connected(h)
    settings = get_settings().model_copy(update={"jwt_access_ttl_s": 2})
    token, _ = issue_access(settings, user_id=uuid.uuid4(), tenant_id=tenant, role="viewer")

    async def touch() -> None:
        for _ in range(100):
            if broker.subscriber_count(tenant):
                break
            await asyncio.sleep(0.02)
        for t, c in ((other_tenant, other_customer), (tenant, customer)):
            async with h.db.tenant_session(t) as s:
                await s.execute(update(Customer).where(Customer.id == c).values(emirate="Dubai"))

    toucher = asyncio.create_task(touch())
    r = await h.client.get("/api/v1/stream", headers={"authorization": f"Bearer {token}"})
    await toucher
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events: list[dict[str, Any]] = []
    for frame in r.text.split("\n\n"):
        if frame.startswith("event: change"):
            events.append(json.loads(frame.split("data: ", 1)[1]))
    assert "event: ready" in r.text
    assert "event: expired" in r.text
    return events


async def test_stream_endpoint_is_tenant_scoped(dash: DashHarness, tenants: TenantPair) -> None:
    events = await stream_events(dash, tenants.a, tenants.b, tenants.customer_a, tenants.customer_b)
    assert [e["id"] for e in events] == [str(tenants.customer_a)]


async def test_stream_requires_a_token(dash: DashHarness) -> None:
    assert (await dash.client.get("/api/v1/stream")).status_code == 401


async def test_broker_announces_resync_after_reconnecting(
    dash: DashHarness, tenants: TenantPair
) -> None:
    broker = await _connected(dash)
    async with broker.subscribe(tenants.a) as sub:
        broker._broadcast_resync()
        assert await _next(sub) == RESYNC

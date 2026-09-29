"""Live change feed for the dashboard: Postgres NOTIFY → per-tenant subscriber queues → SSE.

Migration 0004 puts an AFTER INSERT/UPDATE trigger on customers, orders, conversations and
messages that NOTIFYs `tenant_events` with {t, e, id, op, p}. NOTIFY is delivered on commit only,
so every writer (API, worker, future campaign sender) is covered without remembering to publish,
and the dashboard never hears about a rolled-back row.

One LISTEN connection per API process fans out to in-memory queues keyed by tenant. An event is
only ever put on the queues of the tenant named in the payload — the only routing decision here,
and it is covered by the cross-tenant tests. Payloads carry ids, never message bodies.

If the LISTEN connection drops, subscribers get a `resync` event once it is back (they may have
missed changes) and the dashboard refetches; while it is down the dashboard's 15 s poll covers it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg

from api.core.logging import get_logger

log = get_logger(__name__)

CHANNEL = "tenant_events"
ENTITIES = frozenset({"customers", "orders", "conversations", "messages"})
QUEUE_MAX = 256
PING_EVERY_S = 30.0


@dataclass(frozen=True)
class ChangeEvent:
    entity: str  # customers | orders | conversations | messages | resync
    id: str | None = None
    op: str | None = None
    parent: str | None = None  # messages → conversation id; orders/conversations → customer id

    def as_dict(self) -> dict[str, Any]:
        return {"entity": self.entity, "id": self.id, "op": self.op, "parent": self.parent}


RESYNC = ChangeEvent(entity="resync")


class Subscription:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[ChangeEvent] = asyncio.Queue(QUEUE_MAX)

    def push(self, event: ChangeEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # A client this far behind refetches everything instead.
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(RESYNC)

    async def get(self) -> ChangeEvent:
        return await self._queue.get()


def parse(payload: str) -> tuple[uuid.UUID, ChangeEvent] | None:
    try:
        data = json.loads(payload)
        tenant = uuid.UUID(str(data["t"]))
        entity = str(data["e"])
    except (ValueError, KeyError, TypeError):
        return None
    if entity not in ENTITIES:
        return None
    parent = data.get("p")
    return tenant, ChangeEvent(
        entity=entity,
        id=str(data.get("id")) if data.get("id") is not None else None,
        op=str(data.get("op")) if data.get("op") is not None else None,
        parent=str(parent) if parent is not None else None,
    )


class EventBroker:
    def __init__(self, dsn: str, *, connect_timeout_s: float = 5.0) -> None:
        # asyncpg wants a plain postgres DSN, not SQLAlchemy's "+asyncpg" form.
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._connect_timeout_s = connect_timeout_s
        self._subs: defaultdict[uuid.UUID, set[Subscription]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self.connected = asyncio.Event()

    # ---------------------------------------------------------------- fan-out

    def dispatch(self, payload: str) -> None:
        parsed = parse(payload)
        if parsed is None:
            log.warning("event_payload_invalid")
            return
        tenant, event = parsed
        for sub in tuple(self._subs.get(tenant, ())):
            sub.push(event)

    def _broadcast_resync(self) -> None:
        for subs in self._subs.values():
            for sub in subs:
                sub.push(RESYNC)

    @asynccontextmanager
    async def subscribe(self, tenant_id: uuid.UUID) -> AsyncIterator[Subscription]:
        sub = Subscription()
        self._subs[tenant_id].add(sub)
        try:
            yield sub
        finally:
            self._subs[tenant_id].discard(sub)
            if not self._subs[tenant_id]:
                del self._subs[tenant_id]

    def subscriber_count(self, tenant_id: uuid.UUID | None = None) -> int:
        if tenant_id is not None:
            return len(self._subs.get(tenant_id, ()))
        return sum(len(s) for s in self._subs.values())

    # ---------------------------------------------------------------- LISTEN loop

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="event-broker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        self.dispatch(payload)

    async def _run(self) -> None:
        backoff = 1.0
        first = True
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(self._dsn, timeout=self._connect_timeout_s)
                await conn.add_listener(CHANNEL, self._on_notify)
                self.connected.set()
                if not first:
                    self._broadcast_resync()
                first, backoff = False, 1.0
                log.info("event_broker_listening")
                while True:  # detect a half-open connection
                    await asyncio.sleep(PING_EVERY_S)
                    await asyncio.wait_for(conn.execute("SELECT 1"), self._connect_timeout_s)
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
                self.connected.clear()
                log.warning("event_broker_disconnected", error=type(exc).__name__, retry_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if conn is not None and not conn.is_closed():
                    with contextlib.suppress(
                        OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError
                    ):
                        await asyncio.wait_for(conn.close(), 2)

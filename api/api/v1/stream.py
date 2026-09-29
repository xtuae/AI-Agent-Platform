"""GET /api/v1/stream — Server-Sent Events for the open dashboard, scoped to the token's tenant.

Authenticated with the same Bearer header as every other route: the dashboard reads the stream
with fetch(), not EventSource, so the token never goes into a URL (and so into access logs).
The stream ends when the access token expires (`event: expired`); the client refreshes and
reconnects. A comment line every 15 s keeps proxies from closing an idle connection.

Events carry ids only: {"entity", "id", "op", "parent"}. The client refetches what it shows.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from api.auth.deps import Viewer
from api.config import get_settings
from api.events import EventBroker, Subscription

router = APIRouter(tags=["stream"])


def _frame(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def event_stream(
    sub: Subscription,
    *,
    expires_at: datetime,
    heartbeat_s: float,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    yield "retry: 5000\n\n"
    yield _frame("ready", {})
    while True:
        if await is_disconnected():
            return
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            yield _frame("expired", {})
            return
        try:
            event = await asyncio.wait_for(sub.get(), timeout=min(heartbeat_s, remaining))
        except TimeoutError:
            yield ": ping\n\n"
            continue
        yield _frame("change", event.as_dict())


@router.get("/stream")
async def stream(request: Request, ctx: Viewer) -> StreamingResponse:
    broker: EventBroker = request.app.state.events
    settings = get_settings()

    async def body() -> AsyncIterator[str]:
        async with broker.subscribe(ctx.tenant_id) as sub:
            async for chunk in event_stream(
                sub,
                expires_at=ctx.principal.expires_at,
                heartbeat_s=settings.stream_heartbeat_s,
                is_disconnected=request.is_disconnected,
            ):
                yield chunk

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )

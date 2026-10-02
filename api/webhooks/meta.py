"""GET /webhook/meta (verification handshake) and POST /webhook/meta (ingest).

POST, in this exact order (03 Phase 1): verify signature → parse → route per change → dedup →
persist raw + message rows → enqueue → 200. Target < 200 ms. Never calls an LLM or Graph API.
"""

from __future__ import annotations

import hmac
import re
import time

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from redis.asyncio import Redis

from api.config import get_settings
from api.core.logging import get_logger
from api.webhooks import buffer
from api.webhooks.buffer import Breaker
from api.webhooks.ingest import IngestFailedError, WebhookIngestor
from api.webhooks.payloads import WebhookPayload
from api.webhooks.signature import verify_signature

router = APIRouter()
log = get_logger(__name__)

SIGNATURE_HEADER = "x-hub-signature-256"
_CHALLENGE = re.compile(r"^[0-9A-Za-z_-]{1,128}$")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _buffered(ok: bool) -> Response:
    # 200 once Redis holds it; if Redis is down too, 500 makes Meta keep it and retry
    return Response(status_code=status.HTTP_200_OK if ok else status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.get("/webhook/meta")
async def verify_subscription(request: Request) -> Response:
    """Meta's subscription handshake. One global verify token: the GET carries no
    phone_number_id, so a per-channel token could never be selected."""
    q = request.query_params
    expected = get_settings().meta_verify_token
    token = q.get("hub.verify_token") or ""
    challenge = q.get("hub.challenge") or ""
    if (
        q.get("hub.mode") == "subscribe"
        and expected is not None
        and hmac.compare_digest(token.encode(), expected.get_secret_value().encode())
        and _CHALLENGE.match(challenge)
    ):
        return PlainTextResponse(challenge)
    log.warning("webhook_verify_rejected", source_ip=_client_ip(request))
    return Response(status_code=status.HTTP_403_FORBIDDEN)


@router.post("/webhook/meta")
async def receive(request: Request) -> Response:
    started = time.perf_counter()
    body = await request.body()

    # 1. signature — on the raw bytes, before anything else touches the body
    secret = get_settings().meta_app_secret
    if not verify_signature(
        body, request.headers.get(SIGNATURE_HEADER), secret.get_secret_value() if secret else None
    ):
        log.warning(
            "webhook_signature_rejected",
            source_ip=_client_ip(request),
            has_header=SIGNATURE_HEADER in request.headers,
            secret_configured=secret is not None,
        )
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    # 2. parse — a signed but unparseable body will not parse on retry either: 200 and drop
    try:
        payload = WebhookPayload.model_validate_json(body)
    except ValidationError as exc:
        log.error("webhook_malformed", errors=exc.error_count(), size=len(body))
        return Response(status_code=status.HTTP_200_OK)
    if payload.object != "whatsapp_business_account":
        log.warning("webhook_unexpected_object", object=payload.object)
        return Response(status_code=status.HTTP_200_OK)

    # 3-6. route, dedup, persist, enqueue — or, with the database down, park the body in Redis
    ingestor: WebhookIngestor = request.app.state.ingestor
    breaker: Breaker = request.app.state.webhook_breaker
    redis: Redis = request.app.state.redis
    if breaker.is_open:
        return _buffered(await buffer.push(redis, body))
    try:
        report = await ingestor.ingest(payload)
    except IngestFailedError:
        breaker.trip()
        return _buffered(await buffer.push(redis, body))

    # 7. 200
    log.info(
        "webhook_handled",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        messages_new=report.messages_new,
        messages_duplicate=report.messages_duplicate,
        statuses_new=report.statuses_new,
        changes_dropped=report.changes_dropped,
        jobs_enqueued=report.jobs_enqueued,
        tenant_ids=sorted(str(t) for t in report.tenants),
    )
    return Response(status_code=status.HTTP_200_OK)

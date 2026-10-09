"""POST /webhook/telegram/{channel_key} — one bot's updates (06 §5.2).

In this exact order; nothing touches the body until step 4:
  1. channel_key shape                         bad → 404, no Redis/DB lookup
  2. key → active Telegram channel, routable tenant (cached)
                                               unknown/inactive/suspended → 404, same body for all
  3. X-Telegram-Bot-Api-Secret-Token: sha256 compared in constant time with the hash stored when
     the server registered this webhook        missing/wrong → 403
     (+ optional source-IP check against Telegram's published ranges)
  4. size cap, then parse                      unparseable → 200 and drop (a retry won't parse)
  5. dedup → persist → enqueue (TelegramIngestor) → 200
     database down → 503: Telegram keeps the update and redelivers it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from typing import Final

from fastapi import APIRouter, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from api.channels.telegram.ingest import TelegramIngestor
from api.channels.telegram.payloads import Update
from api.channels.telegram.router import CHANNEL_KEY_RE, TelegramRouter
from api.config import get_settings
from api.core.logging import get_logger
from api.webhooks.ingest import IngestFailedError

router = APIRouter()
log = get_logger(__name__)

SECRET_HEADER: Final = "x-telegram-bot-api-secret-token"  # noqa: S105 — a header name
# https://core.telegram.org/bots/webhooks — the ranges Telegram delivers webhooks from
TELEGRAM_NETWORKS: Final = (
    ipaddress.ip_network("149.154.160.0/20"),
    ipaddress.ip_network("91.108.4.0/22"),
)


def _from_telegram(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in TELEGRAM_NETWORKS)


def secret_matches(header: str | None, stored_hash: bytes | None) -> bool:
    if not header or not stored_hash:
        return False
    return hmac.compare_digest(hashlib.sha256(header.encode()).digest(), stored_hash)


@router.post("/webhook/telegram/{channel_key}")
async def receive(channel_key: str, request: Request) -> Response:
    started = time.perf_counter()
    settings = get_settings()
    not_found = Response(status_code=status.HTTP_404_NOT_FOUND)

    # 1-2. which channel — before anything about the request is believed
    if not CHANNEL_KEY_RE.match(channel_key):
        return not_found
    tg_router: TelegramRouter = request.app.state.telegram_router
    try:
        route = await tg_router.resolve(channel_key)
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        log.error("webhook_route_failed", channel="telegram", error=type(exc).__name__)
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    if route is None:
        return not_found

    # 3. the secret we gave Telegram for THIS channel
    client_ip = request.client.host if request.client else None
    if not secret_matches(request.headers.get(SECRET_HEADER), route.secret_hash):
        log.warning(
            "telegram_secret_rejected", channel_id=str(route.channel_id),
            has_header=SECRET_HEADER in request.headers, registered=route.secret_hash is not None,
        )  # fmt: skip
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    if settings.telegram_check_source_ip and not _from_telegram(client_ip):
        log.warning("telegram_source_rejected", channel_id=str(route.channel_id))
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    # 4. read and parse — verified now
    body = await request.body()
    if len(body) > settings.telegram_webhook_max_body_bytes:
        log.error("telegram_update_too_large", channel_id=str(route.channel_id), size=len(body))
        return Response(status_code=status.HTTP_200_OK)
    try:
        update = Update.model_validate_json(body)
    except ValidationError as exc:
        log.error("webhook_malformed", channel="telegram", errors=exc.error_count(), size=len(body))
        return Response(status_code=status.HTTP_200_OK)

    # 5. store and queue
    ingestor: TelegramIngestor = request.app.state.telegram_ingestor
    try:
        report = await ingestor.ingest(route, update)
    except IngestFailedError:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    log.info(
        "webhook_handled", channel="telegram",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        messages_new=report.messages_new, duplicate=report.duplicate, ignored=report.ignored,
        jobs_enqueued=report.jobs_enqueued, tenant_ids=sorted(str(t) for t in report.tenants),
    )  # fmt: skip
    return Response(status_code=status.HTTP_200_OK)

"""Meta Graph API client: sends, media, templates (create / list / status), the phone's
quality rating and the WABA's pricing analytics (Meta's own per-day message charges). Never used
from the webhook request handler (it must answer in < 200 ms): sends run in workers; template
submission and syncing also run from dashboard requests.

* Every request has an explicit timeout.
* Retries 429 and 5xx with exponential backoff + jitter, honouring Retry-After.
* Sends (POST /messages) are NOT retried on a read timeout: the request may have reached Meta,
  and a retry would message the customer twice. Only failures that prove the request never
  left (connect errors / connect timeouts) are retried for sends. GETs retry any timeout.
* The access token lives only in the Authorization header. It is never logged and never
  appears in exception text; request/response bodies are never logged either.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from api.channels.errors import ChannelAPIError
from api.core.logging import get_logger

log = get_logger(__name__)

RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S: Final = 30.0
BASE_BACKOFF_S: Final = 0.5

Sleep = Callable[[float], Awaitable[None]]


class MetaAPIError(ChannelAPIError):
    def __init__(
        self,
        status_code: int | None,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        fbtrace_id: str | None = None,
    ) -> None:
        super().__init__(f"Meta API error status={status_code} code={code}: {message}")
        self.status_code = status_code
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id


class MetaMediaTooLargeError(MetaAPIError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class _SendContact(_Model):
    wa_id: str | None = None


class _SentMessage(_Model):
    id: str
    message_status: str | None = None


class _SendResponse(_Model):
    contacts: list[_SendContact] = []
    messages: list[_SentMessage]


class _MediaInfo(_Model):
    url: str
    mime_type: str | None = None
    file_size: int | None = None


class TemplateInfo(_Model):
    id: str
    name: str
    language: str | None = None
    status: str
    category: str | None = None
    rejected_reason: str | None = None
    components: list[dict[str, Any]] = []

    def body_text(self) -> str | None:
        for c in self.components:
            if str(c.get("type", "")).upper() == "BODY" and isinstance(c.get("text"), str):
                return str(c["text"])
        return None


class _Paging(_Model):
    next: str | None = None


class _TemplateList(_Model):
    data: list[TemplateInfo] = []
    paging: _Paging | None = None


class _CreatedTemplate(_Model):
    id: str
    status: str | None = None
    category: str | None = None


class PhoneStatus(_Model):
    quality_rating: str | None = None  # GREEN | YELLOW | RED | UNKNOWN
    messaging_limit_tier: str | None = None  # TIER_250 | TIER_1K | TIER_10K | TIER_100K | …


class PricingPoint(_Model):
    """One WABA pricing-analytics data point: a day's charged volume and cost for a category,
    country and pricing type, in the WABA's currency."""

    start: int  # unix seconds, UTC day start
    end: int
    country: str | None = None
    pricing_category: str | None = None
    pricing_type: str | None = None
    volume: int = 0
    cost: float = 0.0


class _PricingData(_Model):
    data_points: list[PricingPoint] = []


class _PricingAnalytics(_Model):
    data: list[_PricingData] = []


class _PricingResponse(_Model):
    currency: str | None = None
    pricing_analytics: _PricingAnalytics | None = None


@dataclass(frozen=True)
class SendResult:
    wamid: str
    recipient_wa_id: str


@dataclass(frozen=True)
class MediaDownload:
    content: bytes
    mime_type: str | None


class MetaClient:
    """One client per channel (access token + phone_number_id). Share the httpx.AsyncClient."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        access_token: str,
        phone_number_id: str,
        api_version: str,
        base_url: str = "https://graph.facebook.com",
        max_retries: int = 3,
        timeout: httpx.Timeout | None = None,
        media_max_bytes: int = 25 * 1024 * 1024,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._token = access_token
        self._pnid = phone_number_id
        self._root = f"{base_url.rstrip('/')}/{api_version}"
        self._max_retries = max_retries
        self._timeout = timeout or httpx.Timeout(10.0, connect=5.0)
        self._media_max = media_max_bytes
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"MetaClient(phone_number_id={self._pnid})"

    # ------------------------------------------------------------ sends

    async def send_text(self, to: str, body: str, *, preview_url: bool = False) -> SendResult:
        return await self._send(
            to, {"type": "text", "text": {"body": body, "preview_url": preview_url}}
        )

    async def send_template(
        self,
        to: str,
        name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if components:
            template["components"] = components
        return await self._send(to, {"type": "template", "template": template})

    async def send_interactive(self, to: str, interactive: dict[str, Any]) -> SendResult:
        return await self._send(to, {"type": "interactive", "interactive": interactive})

    async def _send(self, to: str, message: dict[str, Any]) -> SendResult:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            **message,
        }
        resp = await self._request(
            "POST", f"{self._root}/{self._pnid}/messages", json=payload, idempotent=False
        )
        try:
            parsed = _SendResponse.model_validate(resp.json())
        except (ValidationError, ValueError) as exc:
            raise MetaAPIError(resp.status_code, "unexpected send response shape") from exc
        if not parsed.messages:
            raise MetaAPIError(resp.status_code, "send response had no message id")
        wa_id = parsed.contacts[0].wa_id if parsed.contacts and parsed.contacts[0].wa_id else to
        log.info("meta_sent", wamid=parsed.messages[0].id, msg_type=message["type"])
        return SendResult(wamid=parsed.messages[0].id, recipient_wa_id=wa_id)

    # ------------------------------------------------------------ media

    async def download_media(self, media_id: str) -> MediaDownload:
        meta = await self._request("GET", f"{self._root}/{media_id}", idempotent=True)
        try:
            info = _MediaInfo.model_validate(meta.json())
        except (ValidationError, ValueError) as exc:
            raise MetaAPIError(meta.status_code, "unexpected media response shape") from exc
        if info.file_size is not None and info.file_size > self._media_max:
            raise MetaMediaTooLargeError(meta.status_code, f"media is {info.file_size} bytes")
        # The media URL is on a Meta CDN host and also requires the bearer token.
        data = await self._request("GET", info.url, idempotent=True)
        if len(data.content) > self._media_max:
            raise MetaMediaTooLargeError(data.status_code, f"media is {len(data.content)} bytes")
        return MediaDownload(content=data.content, mime_type=info.mime_type)

    # ------------------------------------------------------------ templates

    async def create_template(
        self,
        waba_id: str,
        *,
        name: str,
        language: str,
        category: str,
        body: str,
        examples: list[str],
    ) -> tuple[str, str | None]:
        """Submit a body-only template for approval. Returns (meta template id, status)."""
        component: dict[str, Any] = {"type": "BODY", "text": body}
        if examples:
            component["example"] = {"body_text": [examples]}
        resp = await self._request(
            "POST",
            f"{self._root}/{waba_id}/message_templates",
            json={
                "name": name,
                "language": language,
                "category": category,
                "components": [component],
            },
            idempotent=False,
        )
        try:
            created = _CreatedTemplate.model_validate(resp.json())
        except (ValidationError, ValueError) as exc:
            raise MetaAPIError(resp.status_code, "unexpected template create response") from exc
        return created.id, created.status

    async def list_templates(self, waba_id: str, *, limit: int = 500) -> list[TemplateInfo]:
        """Every template on the WABA (paged), with status and body."""
        out: list[TemplateInfo] = []
        url: str | None = f"{self._root}/{waba_id}/message_templates"
        params: dict[str, str] | None = {
            "fields": "id,name,language,status,category,rejected_reason,components",
            "limit": "100",
        }
        while url and len(out) < limit:
            resp = await self._request("GET", url, params=params, idempotent=True)
            try:
                page = _TemplateList.model_validate(resp.json())
            except (ValidationError, ValueError) as exc:
                raise MetaAPIError(resp.status_code, "unexpected template list shape") from exc
            out.extend(page.data)
            url = page.paging.next if page.paging and page.paging.next else None
            params = None  # the next link carries its own query
        return out[:limit]

    async def phone_status(self) -> PhoneStatus:
        resp = await self._request(
            "GET",
            f"{self._root}/{self._pnid}",
            params={"fields": "quality_rating,messaging_limit_tier"},
            idempotent=True,
        )
        try:
            return PhoneStatus.model_validate(resp.json())
        except (ValidationError, ValueError) as exc:
            raise MetaAPIError(resp.status_code, "unexpected phone status shape") from exc

    async def get_template_status(self, waba_id: str, name: str) -> list[TemplateInfo]:
        resp = await self._request(
            "GET",
            f"{self._root}/{waba_id}/message_templates",
            params={"name": name, "fields": "id,name,language,status,category,rejected_reason"},
            idempotent=True,
        )
        try:
            return _TemplateList.model_validate(resp.json()).data
        except (ValidationError, ValueError) as exc:
            raise MetaAPIError(resp.status_code, "unexpected template response shape") from exc

    async def pricing_analytics(
        self, waba_id: str, start: int, end: int
    ) -> tuple[str | None, list[PricingPoint]]:
        """Meta's charges per UTC day, category, country and pricing type in [start, end) (unix
        seconds), and the WABA's billing currency."""
        field = (
            f"pricing_analytics.start({start}).end({end}).granularity(DAILY)"
            '.dimensions(["PRICING_CATEGORY","PRICING_TYPE","COUNTRY"])'
        )
        resp = await self._request(
            "GET",
            f"{self._root}/{waba_id}",
            params={"fields": f"currency,{field}"},
            idempotent=True,
        )
        try:
            body = _PricingResponse.model_validate(resp.json())
        except (ValidationError, ValueError) as exc:
            raise MetaAPIError(resp.status_code, "unexpected pricing analytics shape") from exc
        points = [p for d in (body.pricing_analytics.data if body.pricing_analytics else [])
                  for p in d.data_points]  # fmt: skip
        return body.currency, points

    # ------------------------------------------------------------ transport

    async def _request(
        self,
        method: str,
        url: str,
        *,
        idempotent: bool,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        attempt = 0
        while True:
            try:
                resp = await self._http.request(
                    method, url, json=json, params=params, headers=headers, timeout=self._timeout
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                # The request never reached Meta: safe to retry even a send.
                if attempt >= self._max_retries:
                    raise MetaAPIError(None, f"connect failed: {type(exc).__name__}") from None
                await self._backoff(attempt, None, reason=type(exc).__name__)
                attempt += 1
                continue
            except httpx.TimeoutException as exc:
                if not idempotent or attempt >= self._max_retries:
                    # For a send this is ambiguous — it may have been delivered. Do not retry.
                    raise MetaAPIError(None, f"timeout: {type(exc).__name__}") from None
                await self._backoff(attempt, None, reason=type(exc).__name__)
                attempt += 1
                continue
            except httpx.HTTPError as exc:
                raise MetaAPIError(None, f"transport error: {type(exc).__name__}") from None

            if resp.status_code < 400:
                return resp
            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                await self._backoff(
                    attempt, resp.headers.get("retry-after"), reason=str(resp.status_code)
                )
                attempt += 1
                continue
            raise _error_from(resp)

    async def _backoff(self, attempt: int, retry_after: str | None, *, reason: str) -> None:
        delay = BASE_BACKOFF_S * (2**attempt) * (1 + random.random() * 0.25)  # noqa: S311 — jitter
        if retry_after is not None:
            # Seconds form only; an HTTP-date Retry-After falls back to exponential backoff.
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_S))
        log.warning("meta_retry", attempt=attempt + 1, delay_s=round(delay, 2), reason=reason)
        await self._sleep(delay)


def _error_from(resp: httpx.Response) -> MetaAPIError:
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    if not isinstance(err, dict):
        err = {}
    return MetaAPIError(
        resp.status_code,
        str(err.get("message", "request failed"))[:300],
        code=err.get("code"),
        subcode=err.get("error_subcode"),
        fbtrace_id=err.get("fbtrace_id"),
    )

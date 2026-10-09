"""Telegram Bot API client: getMe, webhook registration, sendMessage, chat actions, files.

Mirrors api/meta/client.py:

* One client per bot; share the httpx.AsyncClient. Every request has an explicit timeout.
* 429 is retried after Telegram's `retry_after` (the request was refused, so even a send is safe
  to repeat). 5xx and read timeouts are retried for reads only: a send that timed out may have been
  delivered, and a retry would message the customer twice. Connect failures are always retried.
* **The bot token is in the URL path** (`/bot<token>/<method>`). It is never logged and never in
  exception text; `install_token_log_filter()` scrubs it from httpx's own request log lines, which
  print the URL at INFO. Request and response bodies are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from api.channels.errors import ChannelAPIError, RecipientBlockedError
from api.core.logging import get_logger

log = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]

# BotFather tokens: "<bot id>:<35-ish urlsafe chars>". Checked before any network call.
TOKEN_RE: Final = re.compile(r"^([0-9]{5,16}):[A-Za-z0-9_-]{30,64}$")
_TOKEN_IN_TEXT: Final = re.compile(r"bot[0-9]{5,16}:[A-Za-z0-9_-]{30,64}")
MAX_RETRY_AFTER_S: Final = 30.0
BASE_BACKOFF_S: Final = 0.5


# ---------------------------------------------------------------- the token never reaches a log


class _TokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        if _TOKEN_IN_TEXT.search(message):
            record.msg = _TOKEN_IN_TEXT.sub("bot[redacted]", message)
            record.args = ()
        return True


_FILTER: Final = _TokenFilter()


def install_token_log_filter() -> None:
    """httpx/httpcore log each request's URL at INFO/DEBUG; a bot token is part of that URL.
    Idempotent; runs at import so tests and workers are covered without configure_logging."""
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)


install_token_log_filter()


def bot_id_of(token: str) -> str | None:
    m = TOKEN_RE.match(token)
    return m.group(1) if m else None


# ---------------------------------------------------------------- errors and models


class TelegramAPIError(ChannelAPIError):
    def __init__(self, status_code: int | None, description: str) -> None:
        # Telegram's descriptions never contain the token; scrub anyway, and cap the length.
        clean = _TOKEN_IN_TEXT.sub("bot[redacted]", description)[:300]
        super().__init__(f"Telegram API error status={status_code}: {clean}")
        self.status_code = status_code
        self.description = clean


class TelegramBlockedError(TelegramAPIError, RecipientBlockedError):
    """403 on a send: the user blocked the bot, deleted their account, or never started it."""


class TelegramParseError(TelegramAPIError):
    """400 "can't parse entities": the HTML we sent was not acceptable. Resend as plain text."""


class TelegramFileTooLargeError(TelegramAPIError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class BotInfo(_Model):
    id: int
    is_bot: bool
    first_name: str | None = None
    username: str | None = None


class WebhookInfo(_Model):
    url: str = ""
    pending_update_count: int = 0
    last_error_date: int | None = None
    last_error_message: str | None = None
    max_connections: int | None = None


class _Chat(_Model):
    id: int


class _SentMessage(_Model):
    message_id: int
    chat: _Chat


class _File(_Model):
    file_id: str
    file_size: int | None = None
    file_path: str | None = None


@dataclass(frozen=True)
class FileDownload:
    content: bytes
    file_path: str


# ---------------------------------------------------------------- client


class TelegramClient:
    """One client per bot token. Share the httpx.AsyncClient."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        token: str,
        base_url: str = "https://api.telegram.org",
        max_retries: int = 3,
        timeout: httpx.Timeout | None = None,
        media_max_bytes: int = 20 * 1024 * 1024,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._token = token
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries
        self._timeout = timeout or httpx.Timeout(10.0, connect=5.0)
        self._media_max = media_max_bytes
        self._sleep = sleep
        self.bot_id = bot_id_of(token) or "unknown"

    def __repr__(self) -> str:
        return f"TelegramClient(bot_id={self.bot_id})"

    # ------------------------------------------------------------ bot and webhook

    async def get_me(self) -> BotInfo:
        result = await self._call("getMe", {}, idempotent=True)
        try:
            return BotInfo.model_validate(result)
        except ValidationError as exc:
            raise TelegramAPIError(None, "unexpected getMe response shape") from exc

    async def set_webhook(
        self,
        url: str,
        secret_token: str,
        *,
        allowed_updates: list[str],
        max_connections: int,
    ) -> None:
        await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": allowed_updates,
                "max_connections": max_connections,
                "drop_pending_updates": False,
            },
            idempotent=True,  # setting the same webhook twice is harmless
        )

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook", {"drop_pending_updates": False}, idempotent=True)

    async def get_webhook_info(self) -> WebhookInfo:
        result = await self._call("getWebhookInfo", {}, idempotent=True)
        try:
            return WebhookInfo.model_validate(result)
        except ValidationError as exc:
            raise TelegramAPIError(None, "unexpected getWebhookInfo response shape") from exc

    # ------------------------------------------------------------ messages

    async def send_message(self, chat_id: str, text: str, *, parse_mode: str | None) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        result = await self._call("sendMessage", payload, idempotent=False)
        try:
            sent = _SentMessage.model_validate(result)
        except ValidationError as exc:
            raise TelegramAPIError(None, "unexpected sendMessage response shape") from exc
        log.info("telegram_sent", bot_id=self.bot_id, message_id=sent.message_id)
        return sent.message_id

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action}, idempotent=True)

    # ------------------------------------------------------------ files

    async def download_file(self, file_id: str) -> FileDownload:
        result = await self._call("getFile", {"file_id": file_id}, idempotent=True)
        try:
            info = _File.model_validate(result)
        except ValidationError as exc:
            raise TelegramAPIError(None, "unexpected getFile response shape") from exc
        if info.file_size is not None and info.file_size > self._media_max:
            raise TelegramFileTooLargeError(None, f"file is {info.file_size} bytes")
        if not info.file_path:
            raise TelegramAPIError(None, "file has no download path")
        url = f"{self._base}/file/bot{self._token}/{info.file_path}"
        resp = await self._send("GET", url, None, idempotent=True, what="file")
        if resp.status_code >= 400:
            raise TelegramAPIError(resp.status_code, "file download failed")
        if len(resp.content) > self._media_max:
            raise TelegramFileTooLargeError(resp.status_code, f"file is {len(resp.content)} bytes")
        return FileDownload(content=resp.content, file_path=info.file_path)

    # ------------------------------------------------------------ transport

    async def _call(self, method: str, payload: dict[str, Any], *, idempotent: bool) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        resp = await self._send("POST", url, payload, idempotent=idempotent, what=method)
        try:
            body = resp.json()
        except ValueError:
            raise TelegramAPIError(resp.status_code, f"{method}: response is not JSON") from None
        if not isinstance(body, dict):
            raise TelegramAPIError(resp.status_code, f"{method}: unexpected response")
        if body.get("ok") is True:
            return body.get("result")
        raise _error_from(resp.status_code, method, body)

    async def _send(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        *,
        idempotent: bool,
        what: str,
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = await self._http.request(method, url, json=payload, timeout=self._timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                if attempt >= self._max_retries:  # never reached Telegram: safe for a send too
                    raise TelegramAPIError(None, f"{what}: connect failed") from None
                await self._backoff(attempt, None, reason=type(exc).__name__)
                attempt += 1
                continue
            except httpx.TimeoutException as exc:
                if not idempotent or attempt >= self._max_retries:
                    raise TelegramAPIError(None, f"{what}: timeout") from None
                await self._backoff(attempt, None, reason=type(exc).__name__)
                attempt += 1
                continue
            except httpx.HTTPError as exc:
                reason = f"{what}: transport error {type(exc).__name__}"
                raise TelegramAPIError(None, reason) from None

            retryable = resp.status_code == 429 or (resp.status_code >= 500 and idempotent)
            if retryable and attempt < self._max_retries:
                await self._backoff(attempt, _retry_after(resp), reason=str(resp.status_code))
                attempt += 1
                continue
            return resp

    async def _backoff(self, attempt: int, retry_after: float | None, *, reason: str) -> None:
        delay = BASE_BACKOFF_S * (2**attempt) * (1 + random.random() * 0.25)  # noqa: S311 — jitter
        if retry_after is not None:
            delay = max(delay, min(retry_after, MAX_RETRY_AFTER_S))
        log.warning(
            "telegram_retry", bot_id=self.bot_id, attempt=attempt + 1, delay_s=round(delay, 2),
            reason=reason,
        )  # fmt: skip
        await self._sleep(delay)


def _retry_after(resp: httpx.Response) -> float | None:
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        params = resp.json().get("parameters") or {}
        value = params.get("retry_after")
        if value is not None:
            return float(value)
    return None


def _error_from(status_code: int, method: str, body: dict[str, Any]) -> TelegramAPIError:
    code = body.get("error_code")
    status = code if isinstance(code, int) else status_code
    description = str(body.get("description") or "request failed")
    text = f"{method}: {description}"
    if status == 403:
        return TelegramBlockedError(status, text)
    if status == 400 and "parse entities" in description.lower():
        return TelegramParseError(status, text)
    return TelegramAPIError(status, text)

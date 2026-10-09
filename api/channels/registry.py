"""channel row → its sender. The one place that knows which class sends for which kind."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from api.channels.base import ChannelSender
from api.channels.outbound import ChannelNotConfiguredError
from api.channels.telegram.client import TelegramClient
from api.channels.telegram.sender import TelegramSender
from api.config import Settings
from api.core.crypto import decrypt_secret
from api.db.models import TenantChannel
from api.meta.outbound import client_for_channel

SenderFactory = Callable[[TenantChannel], ChannelSender]


def telegram_client(token: str, http: httpx.AsyncClient, settings: Settings) -> TelegramClient:
    return TelegramClient(
        http=http,
        token=token,
        base_url=settings.telegram_api_base_url,
        max_retries=settings.telegram_max_retries,
        timeout=httpx.Timeout(
            settings.telegram_http_timeout_s, connect=settings.telegram_connect_timeout_s
        ),
        media_max_bytes=settings.telegram_media_max_bytes,
    )


def telegram_client_for(
    channel: TenantChannel, http: httpx.AsyncClient, settings: Settings
) -> TelegramClient:
    if channel.kind != "telegram" or channel.access_token_encrypted is None:
        raise ChannelNotConfiguredError(f"channel {channel.id} has no bot token")
    return telegram_client(decrypt_secret(channel.access_token_encrypted), http, settings)


def sender_for(
    channel: TenantChannel, http: httpx.AsyncClient, settings: Settings
) -> ChannelSender:
    if channel.kind == "telegram":
        return TelegramSender(telegram_client_for(channel, http, settings))
    return client_for_channel(channel, http, settings)

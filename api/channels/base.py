"""What a messaging channel is to the rest of the platform (06_multichannel_telegram.md §3).

Everything channel-specific is either here, as data (Capabilities), or behind ChannelSender. Code
outside api/channels and the per-channel webhook modules asks `caps_for(kind)` instead of
checking `kind == "telegram"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal, Protocol, get_args

from api.channels.errors import ChannelAPIError, RecipientUnreachableError
from api.meta.client import MediaDownload, SendResult

ChannelKind = Literal["whatsapp", "telegram"]
CHANNEL_KINDS: Final[tuple[str, ...]] = get_args(ChannelKind)


@dataclass(frozen=True)
class Capabilities:
    name: str  # what a customer calls it: "WhatsApp", "Telegram"
    service_window: timedelta | None  # free-form replies only inside it; None = no window
    priced: bool  # the channel charges per message (Meta does; Telegram does not)
    templates: bool  # campaigns / out-of-window sends need pre-approved templates
    delivery_receipts: bool  # sent → delivered → read webhooks exist
    max_text_chars: int  # transport limit per message
    identity_is_phone: bool  # the external id is a phone number (pricing market, tel: links)


CAPS: Final[dict[str, Capabilities]] = {
    "whatsapp": Capabilities(
        name="WhatsApp",
        service_window=timedelta(hours=24),
        priced=True,
        templates=True,
        delivery_receipts=True,
        max_text_chars=4096,
        identity_is_phone=True,
    ),
    "telegram": Capabilities(
        name="Telegram",
        service_window=None,
        priced=False,
        templates=False,
        delivery_receipts=False,
        max_text_chars=4096,
        identity_is_phone=False,
    ),
}


def caps_for(kind: str) -> Capabilities:
    try:
        return CAPS[kind]
    except KeyError:
        raise ValueError(f"unknown channel kind {kind!r}") from None


class ChannelSender(Protocol):
    """What the turn runner and the dashboard reply need from a channel. `MetaClient` already has
    this shape; `TelegramSender` implements it for bots."""

    async def send_text(self, to: str, body: str) -> SendResult: ...

    async def download_media(self, media_id: str) -> MediaDownload: ...


__all__ = [
    "CAPS",
    "CHANNEL_KINDS",
    "Capabilities",
    "ChannelAPIError",
    "ChannelKind",
    "ChannelSender",
    "MediaDownload",
    "RecipientUnreachableError",
    "SendResult",
    "caps_for",
]

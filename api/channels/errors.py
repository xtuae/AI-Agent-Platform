"""Channel errors with no imports, so api.meta and api.channels can both depend on them."""

from __future__ import annotations


class ChannelAPIError(Exception):
    """A channel's API refused or failed a request. MetaAPIError and TelegramAPIError are both
    caught as this by channel-agnostic code."""


class RecipientBlockedError(ChannelAPIError):
    """The channel refused a send because the recipient blocked us (Telegram 403)."""


class RecipientUnreachableError(Exception):
    """The customer cannot be messaged on this channel: no identity there, or they blocked us."""

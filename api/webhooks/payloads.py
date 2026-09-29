"""Pydantic models for the Meta WhatsApp Cloud API webhook.

The envelope (object → entry[] → changes[]) is parsed strictly enough to route; each change's
`value` is parsed per `field` separately so one malformed change cannot drop the others.
Unknown extra fields are allowed: Meta adds fields without notice and must not break ingest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------- envelope


class Change(_Model):
    field: str
    value: dict[str, Any]


class Entry(_Model):
    id: str  # WABA id
    changes: list[Change] = Field(default_factory=list)


class WebhookPayload(_Model):
    object: str
    entry: list[Entry] = Field(default_factory=list)


# ---------------------------------------------------------------- field = "messages"


class Metadata(_Model):
    display_phone_number: str | None = None
    phone_number_id: str


class Profile(_Model):
    name: str | None = None


class Contact(_Model):
    wa_id: str
    profile: Profile | None = None


class Text(_Model):
    body: str


class Media(_Model):
    id: str
    mime_type: str | None = None
    caption: str | None = None
    sha256: str | None = None
    voice: bool | None = None


class Reply(_Model):
    id: str
    title: str | None = None
    description: str | None = None


class Interactive(_Model):
    type: str
    button_reply: Reply | None = None
    list_reply: Reply | None = None


class Button(_Model):
    payload: str | None = None
    text: str | None = None


class MessageContext(_Model):
    from_: str | None = Field(default=None, alias="from")
    id: str | None = None  # wamid of the message being replied to (campaign attribution)


class InboundMessage(_Model):
    from_: str = Field(alias="from")
    id: str  # wamid
    timestamp: str
    type: str
    text: Text | None = None
    image: Media | None = None
    audio: Media | None = None
    video: Media | None = None
    document: Media | None = None
    sticker: Media | None = None
    interactive: Interactive | None = None
    button: Button | None = None
    context: MessageContext | None = None

    @property
    def sent_at(self) -> datetime:
        try:
            return datetime.fromtimestamp(int(self.timestamp), tz=UTC)
        except (ValueError, OverflowError):
            return datetime.now(UTC)

    @property
    def media(self) -> Media | None:
        return self.image or self.audio or self.video or self.document or self.sticker

    @property
    def body_text(self) -> str | None:
        """Customer-visible text of the message. Stored, never logged."""
        if self.text is not None:
            return self.text.body
        if self.interactive is not None:
            reply = self.interactive.button_reply or self.interactive.list_reply
            return reply.title if reply else None
        if self.button is not None:
            return self.button.text
        media = self.media
        return media.caption if media else None


class StatusError(_Model):
    code: int | None = None
    title: str | None = None


class Pricing(_Model):
    billable: bool | None = None
    pricing_model: str | None = None
    category: str | None = None


class Status(_Model):
    id: str  # wamid of OUR outbound message
    status: str  # sent | delivered | read | failed | deleted
    timestamp: str | None = None
    recipient_id: str | None = None
    errors: list[StatusError] = Field(default_factory=list)
    pricing: Pricing | None = None


class MessagesValue(_Model):
    messaging_product: str | None = None
    metadata: Metadata
    contacts: list[Contact] = Field(default_factory=list)
    messages: list[InboundMessage] = Field(default_factory=list)
    statuses: list[Status] = Field(default_factory=list)

    def profile_name(self, wa_id: str) -> str | None:
        for c in self.contacts:
            if c.wa_id == wa_id and c.profile is not None:
                return c.profile.name
        return None


# ------------------------------------------ field = "message_template_status_update"


class TemplateStatusValue(_Model):
    event: str  # APPROVED | REJECTED | PENDING | PAUSED | DISABLED | …
    message_template_id: int | str
    message_template_name: str | None = None
    message_template_language: str | None = None
    reason: str | None = None

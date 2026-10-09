"""Pydantic models for Telegram Bot API updates, and the normaliser to InboundMessage.

Parsed only after the webhook's secret_token check. Extra fields are allowed (Telegram adds fields
without notice). Only private chats with a human sender are ever turned into messages.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from api.channels.inbound import InboundMessage
from api.channels.telegram.sender import message_ref
from api.core.logging import get_logger

log = get_logger(__name__)

_START = re.compile(r"^/start(?:@\w+)?(?:\s+([A-Za-z0-9_-]{1,64}))?\s*$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class User(_Model):
    id: int
    is_bot: bool = False
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None

    @property
    def display_name(self) -> str | None:
        name = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        return name or None


class Chat(_Model):
    id: int
    type: str  # private | group | supergroup | channel


class File(_Model):
    file_id: str
    file_size: int | None = None
    mime_type: str | None = None


class Message(_Model):
    message_id: int
    date: int
    chat: Chat
    from_: User | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    voice: File | None = None
    audio: File | None = None
    video: File | None = None
    video_note: File | None = None
    document: File | None = None
    photo: list[File] | None = None
    sticker: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    contact: dict[str, Any] | None = None

    @property
    def sent_at(self) -> datetime:
        try:
            return datetime.fromtimestamp(self.date, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return datetime.now(UTC)

    def kind_and_file(self) -> tuple[str, str | None]:
        """The platform's msg_type vocabulary (WhatsApp's), and the file to fetch if any."""
        if self.text is not None:
            return "text", None
        if self.voice is not None:
            return "audio", self.voice.file_id
        if self.audio is not None:
            return "audio", self.audio.file_id
        if self.photo:
            return "image", self.photo[-1].file_id  # largest size last
        if self.video is not None or self.video_note is not None:
            f = self.video or self.video_note
            return "video", f.file_id if f else None
        if self.document is not None:
            return "document", self.document.file_id
        if self.sticker is not None:
            return "sticker", None
        if self.location is not None:
            return "location", None
        if self.contact is not None:
            return "contacts", None
        return "unsupported", None


class ChatMember(_Model):
    status: str  # creator | administrator | member | restricted | left | kicked


class ChatMemberUpdated(_Model):
    chat: Chat
    from_: User = Field(alias="from")
    date: int
    new_chat_member: ChatMember


class Update(_Model):
    update_id: int
    message: Message | None = None
    edited_message: Message | None = None
    my_chat_member: ChatMemberUpdated | None = None


def start_payload(text: str | None) -> str | None:
    """`/start <payload>` from a t.me/<bot>?start=<payload> deep link (consent pages)."""
    if not text:
        return None
    m = _START.match(text.strip())
    return m.group(1) if m and m.group(1) else None


def normalise(bot_id: str, m: Message) -> InboundMessage | None:
    """A private, human message → the platform's InboundMessage. Anything else → None."""
    if m.chat.type != "private" or m.from_ is None or m.from_.is_bot:
        return None
    if m.chat.id != m.from_.id:  # private chat id is the user id; anything else is not ours
        return None
    msg_type, file_id = m.kind_and_file()
    return InboundMessage(
        channel_kind="telegram",
        external_user_id=str(m.from_.id),
        external_message_id=message_ref(bot_id, m.chat.id, m.message_id),
        sent_at=m.sent_at,
        msg_type=msg_type,
        text=m.text if m.text is not None else m.caption,
        media_ref=f"tg-file:{file_id}" if file_id else None,
        profile_name=m.from_.display_name,
        username=m.from_.username,
        start_payload=start_payload(m.text),
    )

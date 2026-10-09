"""TelegramSender: the ChannelSender for a bot (06 §5.4, §5.5).

* `to` is the Telegram chat id (= the user id, private chats only).
* Text is converted to Telegram HTML and split at paragraph/sentence boundaries; a part Telegram
  cannot parse is resent once as plain text, so a reply is never lost over formatting.
* The returned id is namespaced, `tg:<bot_id>:<chat_id>:<message_id>` (first part), so it can
  share messages.wamid's global UNIQUE with Meta's `wamid.…` ids without ever colliding.
* Media ids are Telegram file_ids; voice notes are OGG/Opus like WhatsApp's.
"""

from __future__ import annotations

import mimetypes

from api.channels.telegram.client import TelegramClient, TelegramParseError
from api.channels.telegram.format import split_text, to_telegram_html
from api.meta.client import MediaDownload, SendResult


def message_ref(bot_id: str, chat_id: str | int, message_id: int) -> str:
    return f"tg:{bot_id}:{chat_id}:{message_id}"


def _mime(file_path: str) -> str | None:
    if file_path.endswith((".oga", ".ogg", ".opus")):
        return "audio/ogg"
    return mimetypes.guess_type(file_path)[0]


class TelegramSender:
    kind = "telegram"

    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    def __repr__(self) -> str:
        return f"TelegramSender(bot_id={self.client.bot_id})"

    async def send_text(self, to: str, body: str) -> SendResult:
        first: int | None = None
        for part in split_text(body):
            try:
                message_id = await self.client.send_message(
                    to, to_telegram_html(part), parse_mode="HTML"
                )
            except TelegramParseError:
                message_id = await self.client.send_message(to, part, parse_mode=None)
            if first is None:
                first = message_id
        if first is None:  # an empty body: nothing was sent, and nothing should be recorded
            raise ValueError("empty message")
        return SendResult(wamid=message_ref(self.client.bot_id, to, first), recipient_wa_id=to)

    async def download_media(self, media_id: str) -> MediaDownload:
        f = await self.client.download_file(media_id)
        return MediaDownload(content=f.content, mime_type=_mime(f.file_path))

    async def typing(self, to: str) -> None:
        await self.client.send_chat_action(to, "typing")

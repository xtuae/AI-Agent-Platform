"""The agent writes WhatsApp markup (one prompt, one validator for every channel). Telegram gets it
as HTML parse mode — not MarkdownV2, whose 18 reserved characters make model text fail to send.

    *bold*  → <b>     _italic_ → <i>     ~strike~ → <s>     `code` → <code>     ```block``` → <pre>

Everything else is HTML-escaped. A marker that does not open and close around non-space text, on
one line, stays literal — so "2 * 3 * 4" and snake_case_names are not mangled.
"""

from __future__ import annotations

import html
import re
from typing import Final

TELEGRAM_MAX_CHARS: Final = 4096
# Split a little under the limit: the limit counts characters after entity parsing, and a part
# must never end up 1 over because of a surrogate pair or an escaped character.
SPLIT_AT: Final = 4000

_CODE: Final = re.compile(r"```(.+?)```|`([^`\n]+)`", re.S)
_INLINE: Final = tuple(
    (re.compile(rf"(?<![\w{re.escape(m)}]){re.escape(m)}(?=\S)([^{re.escape(m)}\n]+?)(?<=\S)"
                rf"{re.escape(m)}(?![\w{re.escape(m)}])"), tag)
    for m, tag in (("*", "b"), ("_", "i"), ("~", "s"))
)  # fmt: skip


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    for pattern, tag in _INLINE:
        out = pattern.sub(rf"<{tag}>\1</{tag}>", out)
    return out


def to_telegram_html(text: str) -> str:
    parts: list[str] = []
    pos = 0
    for m in _CODE.finditer(text):
        parts.append(_inline(text[pos : m.start()]))
        if m.group(1) is not None:
            parts.append(f"<pre>{html.escape(m.group(1).strip(chr(10)), quote=False)}</pre>")
        else:
            parts.append(f"<code>{html.escape(m.group(2), quote=False)}</code>")
        pos = m.end()
    parts.append(_inline(text[pos:]))
    return "".join(parts)


def split_text(text: str, limit: int = SPLIT_AT) -> list[str]:
    """Paragraph, then line, then sentence, then word boundaries; a hard cut only for one
    unbroken run longer than the limit."""
    text = text.strip()
    parts: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = -1
        for sep in ("\n\n", "\n", ". ", "؟ ", "? ", "! ", " "):
            i = window.rfind(sep)
            if i > limit // 3:
                cut = i + len(sep.rstrip())
                break
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts

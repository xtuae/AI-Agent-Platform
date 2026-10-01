"""Phone numbers as people type them into spreadsheets → WhatsApp ids (E.164 digits, no '+').

Handles what real customer sheets contain: Excel floats (501234567.0) and scientific notation
(5.01234567E+08), Arabic-Indic and Persian digits, spaces / dashes / dots / brackets, "+971",
"00971", "9710 5…", a bare "05…" or "5…" local mobile, and two numbers in one cell ("050… / 055…").

A number without a country code is read as a UAE number (the platform's home market). A number
with an explicit country code is accepted only if that market is priced (api.meta.pricing) —
anything else could never be messaged or metered, so it is rejected with a reason instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from api.meta.pricing import PricingNotConfiguredError, market_for

UAE: Final = "971"
UAE_MOBILE_PREFIXES: Final = frozenset({"50", "52", "54", "55", "56", "58"})

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_SCIENTIFIC = re.compile(r"^\d+(?:\.\d+)?[eE]\+?\d+$")
_SPLIT = re.compile(r"[/,;|&\n]|\s(?:or|او|أو)\s", re.I)


@dataclass(frozen=True)
class Phone:
    wa_id: str | None
    reason: str | None = None  # why it was rejected (None when wa_id is set)


def _text(raw: object) -> str:
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        return str(int(raw)) if raw.is_integer() else repr(raw)
    s = str(raw).translate(_DIGITS).strip()
    if _SCIENTIFIC.match(s):
        try:
            return str(int(Decimal(s)))
        except (InvalidOperation, ValueError):
            return s
    return re.sub(r"\.0+$", "", s)


def _one(part: str) -> Phone:
    explicit = part.lstrip().startswith("+")
    digits = re.sub(r"\D", "", part)
    if not digits:
        return Phone(None, "no phone number")
    if digits.startswith("00"):
        digits, explicit = digits[2:], True
    if digits.startswith(UAE + "0"):  # "+971 050…"
        digits = UAE + digits[4:]

    if explicit or (digits.startswith(UAE) and len(digits) == 12):
        e164 = digits
    elif len(digits) == 10 and digits.startswith("05"):
        e164 = UAE + digits[1:]
    elif len(digits) == 9 and digits.startswith("5"):
        e164 = UAE + digits
    elif len(digits) in (8, 9) and digits.startswith("0"):
        return Phone(None, "landline number (not on WhatsApp)")
    else:
        return Phone(None, "unrecognised phone number")

    if e164.startswith(UAE):
        rest = e164[len(UAE) :]
        if len(rest) == 9 and rest[:2] in UAE_MOBILE_PREFIXES:
            return Phone(e164)
        if len(rest) == 8:
            return Phone(None, "landline number (not on WhatsApp)")
        return Phone(None, "invalid UAE mobile number")
    if not 8 <= len(e164) <= 15:
        return Phone(None, "invalid international number")
    try:
        market_for(e164)
    except PricingNotConfiguredError:
        return Phone(None, "country not supported")
    return Phone(e164)


def normalize_phone(raw: object) -> Phone:
    """The first usable WhatsApp id in `raw`, or the reason the first number was rejected."""
    if raw is None:
        return Phone(None, "no phone number")
    text = _text(raw)
    if not text:
        return Phone(None, "no phone number")
    first: Phone | None = None
    for part in _SPLIT.split(text):
        if not part.strip():
            continue
        phone = _one(part)
        if phone.wa_id:
            return phone
        first = first or phone
    return first or Phone(None, "no phone number")

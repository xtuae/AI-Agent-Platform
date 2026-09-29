"""Text utilities shared by the classifier, validator and prompt budget.

No model call anywhere in here — these are the deterministic parts of the agent.
"""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Final

_ARABIC_LETTER = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")
_DIGIT_MAP: Final = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\w])")


# ---------------------------------------------------------------- tokens


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (no tokenizer download, no network).

    Latin text runs ~4 chars/token in Gemini's tokenizer; Arabic ~2. Using 3.2 and 1.8 errs high,
    so a prompt that passes this check passes the real one.
    """
    arabic = len(_ARABIC_LETTER.findall(text))
    other = len(text) - arabic
    return math.ceil(other / 3.2 + arabic / 1.8)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    space = cut.rfind(" ")
    return (cut[:space] if space > lo * 0.8 else cut).rstrip() + " …"


# ---------------------------------------------------------------- script / language


def normalise_digits(text: str) -> str:
    return text.translate(_DIGIT_MAP)


def script_of(text: str) -> str:
    """'ar' if Arabic letters dominate, 'latin' if Latin letters dominate, else 'none'."""
    ar = len(_ARABIC_LETTER.findall(text))
    la = len(_LATIN_LETTER.findall(text))
    if ar == 0 and la == 0:
        return "none"
    return "ar" if ar >= la else "latin"


def mixed_script(text: str) -> bool:
    """True when the customer genuinely mixes Arabic and Latin script."""
    ar, la = len(_ARABIC_LETTER.findall(text)), len(_LATIN_LETTER.findall(text))
    return ar > 0 and la > 0 and min(ar, la) / max(ar, la) > 0.25


def has_arabic(text: str) -> bool:
    return bool(_ARABIC_LETTER.search(text))


# ---------------------------------------------------------------- numbers


def to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "")).normalize()
    except InvalidOperation:
        return None


_DATE_OR_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)


def numbers_in(text: str, *, skip_dates: bool = False) -> set[Decimal]:
    """Every standalone number in `text` (Arabic-Indic digits included). '3andi' is not a number."""
    out: set[Decimal] = set()
    text = normalise_digits(text)
    if skip_dates:  # "2027-01-14" is a date, not the numbers 2027, 1 and 14
        text = _DATE_OR_TIME.sub(" ", text)
    for m in _NUMBER.finditer(text):
        d = to_decimal(m.group(1) + (m.group(2) or ""))
        if d is not None:
            out.add(d)
    return out


def clean_inline(value: str | None, max_len: int = 60) -> str:
    """For customer-controlled strings placed in a prompt (e.g. WhatsApp profile name):
    strip control characters and newlines so they cannot open a new 'section' of the prompt."""
    if not value:
        return ""
    s = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]

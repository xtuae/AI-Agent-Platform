"""WhatsApp templates: Meta's rules checked before submitting, rendering, and the copy drafter.

The drafter (02 §4.1) writes copy for a PERSON to review; nothing it returns is saved or sent by
itself. Its prompt contains literal {{1}} placeholders (Meta's syntax), so it is filled by plain
substitution of the named fields only, not by Jinja.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, ValidationError

from api.llm.router import LLM, LLMResult

DRAFTER_VERSION: Final = "outreach_drafter_v1"
DRAFTER_PATH: Final = (
    Path(__file__).resolve().parents[2] / "agents/prompts/templates" / (f"{DRAFTER_VERSION}.j2")
)
MAX_BODY: Final = 700  # 02 §4.1 (Meta allows 1024; shorter performs better)
NAME: Final = re.compile(r"^[a-z0-9_]{1,512}$")
PLACEHOLDER: Final = re.compile(r"\{\{\s*(\d+)\s*\}\}")
LINK: Final = re.compile(r"https?://|www\.", re.I)
ABSOLUTE: Final = re.compile(r"\b(best in|cheapest|guaranteed|number one|no\.? ?1)\b|#1", re.I)
EMOJI: Final = re.compile("[\U0001f300-\U0001faff☀-➿]")
DRAFT_FIELDS: Final = (
    "business_name",
    "business_description",
    "service_areas",
    "objective",
    "segment_description",
    "offer_details",
    "products",
    "language",
)


class Variable(BaseModel):
    index: int = Field(ge=1, le=20)
    meaning: str = Field(default="", max_length=120)
    example: str = Field(min_length=1, max_length=120)


@dataclass
class Check:
    errors: list[str] = field(default_factory=list)  # Meta would reject it
    warnings: list[str] = field(default_factory=list)  # style (02 §4.1)

    @property
    def ok(self) -> bool:
        return not self.errors


def check(body: str, variables: list[Variable], *, links_allowed: bool = False) -> Check:
    """Meta's template rules (02 §4.1) as far as they can be checked mechanically."""
    out = Check()
    text = body.strip()
    if not text:
        out.errors.append("The message is empty.")
        return out
    if len(text) > MAX_BODY:
        out.errors.append(f"Keep it under {MAX_BODY} characters (it is {len(text)}).")
    found = [int(n) for n in PLACEHOLDER.findall(text)]
    order: list[int] = []
    for n in found:
        if n not in order:
            order.append(n)
    if order != list(range(1, len(order) + 1)):
        out.errors.append("Variables must be {{1}}, {{2}} … in order, with no gaps.")
    defined = {v.index for v in variables}
    missing = [n for n in order if n not in defined]
    if missing:
        out.errors.append(
            f"Give an example value for {', '.join(f'{{{{{n}}}}}' for n in missing)}."
        )
    unused = sorted(defined - set(order))
    if unused:
        out.errors.append(f"Variable {unused[0]} is described but not used in the text.")
    if PLACEHOLDER.match(text) or re.search(r"\{\{\s*\d+\s*\}\}$", text):
        out.errors.append("The message cannot start or end with a variable.")
    if re.search(r"\}\}\s*\{\{", text):
        out.errors.append("Two variables cannot sit next to each other.")
    if not links_allowed and LINK.search(text):
        out.errors.append(
            "No links in a marketing message until the business's domain is verified."
        )
    if ABSOLUTE.search(text):
        out.errors.append('No absolute claims ("best in UAE", "cheapest", "guaranteed").')
    if len(EMOJI.findall(text)) > 1:
        out.warnings.append("At most one emoji.")
    if "!!" in text or text.count("!") > 2:
        out.warnings.append("Go easy on exclamation marks.")
    return out


def render(body: str, values: list[str]) -> str:
    """The message a customer would see: {{n}} → values[n-1]."""

    def value(m: re.Match[str]) -> str:
        n = int(m.group(1))
        return values[n - 1] if 0 < n <= len(values) else m.group(0)

    return PLACEHOLDER.sub(value, body)


def placeholder_count(body: str) -> int:
    return len({int(n) for n in PLACEHOLDER.findall(body)})


# ---------------------------------------------------------------- drafter (02 §4.1)


class Draft(BaseModel):
    name: str = Field(pattern=NAME.pattern)
    category: Literal["MARKETING", "UTILITY"]
    language: Literal["en", "ar"]
    body: str = Field(min_length=1, max_length=1024)
    variables: list[Variable] = Field(default_factory=list, max_length=20)
    rationale: str = Field(default="", max_length=400)


class DraftError(Exception):
    pass


def drafter_prompt(**brief: str) -> str:
    text = DRAFTER_PATH.read_text()
    for key in DRAFT_FIELDS:
        text = text.replace("{{" + key + "}}", brief.get(key, "").strip() or "—")
    return text.strip()


async def draft(
    llm: LLM, *, provider: str, model: str, brief: dict[str, str]
) -> tuple[Draft, LLMResult]:
    result = await llm.chat(
        provider=provider,
        model=model,
        messages=[
            {"role": "system", "content": drafter_prompt(**brief)},
            {"role": "user", "content": "Write the message now. Return only the JSON."},
        ],
        temperature=0.7,
        max_tokens=900,
        json_mode=True,
    )
    raw = (result.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        payload: Any = json.loads(raw)
        return Draft.model_validate(payload), result
    except (ValueError, ValidationError) as exc:
        raise DraftError("the drafter returned something unusable; try again") from exc

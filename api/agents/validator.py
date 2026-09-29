"""Post-generation validator — 02_agent_prompts §3.2. Runs on every drafted reply before Meta.

Cheap, deterministic, no model call. Each failed check carries an action:
  regenerate → one corrective regeneration, then escalate if it fails again
  escalate   → hand over immediately (a holding message is sent instead of the draft)

Checks
 1. numbers            every currency figure, every bottle count and every decimal amount in the
                       reply must appear in a tool result from this turn (or in the DB-rendered
                       customer block). Bottle counts may also echo the customer's own numbers
                       ("5 bottles, got it"). Arithmetic the model did itself does NOT pass.
 2. forbidden_promise  refund / free / discount / guarantee / compensation … not backed by a tool
                       result → escalate. A negated mention ("I can't offer a discount") is a
                       refusal, not a promise, and passes.
 3. leak               prompt / model / provider / tool names / SQL / another tenant's name.
 4. length             > 700 characters.
 5. language           reply script must match the customer's (ar → Arabic, en/ar-latn → Latin).
 6. empty_or_duplicate empty, or byte-identical to the previous outbound → escalate.
 7. ai_disclosure      (added; 01 §10.2) first reply of a new conversation must identify itself
                       as an automated assistant.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal

from api.agents.text import mixed_script, normalise_digits, numbers_in, script_of, to_decimal

Check = Literal[
    "numbers",
    "forbidden_promise",
    "leak",
    "length",
    "language",
    "empty_or_duplicate",
    "ai_disclosure",
]
Action = Literal["regenerate", "escalate"]

MAX_REPLY_CHARS: Final = 700

_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_CURRENCY_WORDS = r"(?:aed|dhs?|dirhams?|درهم|دراهم|د\.إ)"
_BOTTLE_WORDS = r"(?:bottles?|gallons?|coupons?|refills?|زجاج\w*|قنين\w*|عبو\w*|كوبون\w*|جالون\w*)"

_CURRENCY = (
    re.compile(rf"{_CURRENCY_WORDS}\.?\s*{_NUM}", re.I),
    re.compile(rf"{_NUM}\s*{_CURRENCY_WORDS}", re.I),
)
_BOTTLES = (
    re.compile(rf"{_NUM}\s+(?:\w+\s+){{0,2}}?{_BOTTLE_WORDS}", re.I),
    re.compile(rf"{_BOTTLE_WORDS}\s*:?\s*{_NUM}", re.I),
)
_DECIMAL_AMOUNT = re.compile(r"(?<![\w.])(\d+\.\d{1,2})(?![\w.])")

_PROMISE_TERMS: Final = (
    "refund",
    "reimburse",
    "free",
    "discount",
    "% off",
    "compensat",
    "credit",
    "guarantee",
    "replacement",
    "replace it",
    "waive",
    "استرداد",
    "استرجاع",
    "مجان",
    "خصم",
    "تعويض",
    "ضمان",
    "بديل",
)
# A negation within the 40 characters BEFORE the term marks a refusal ("I can't offer a
# discount"). Bare "no" is excluded on purpose: "No problem, it's free" is a promise.
_NEGATION = re.compile(
    r"\b(not|never|can'?t|cannot|won'?t|unable|isn'?t|aren'?t|don'?t|only (?:our|the) team)\b|"
    r"(?:^|\s)(لا|لن|ليس|لسنا|غير)\s",
    re.I,
)
_NEGATION_WINDOW: Final = 40
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟\n])\s+")

_LEAK = re.compile(
    r"\b(system prompt|my prompt|the prompt|llm|large language model|language model|"
    r"ai model|gemini|openai|openrouter|chatgpt|gpt-?\d*|anthropic|api|database|sql|json|"
    r"tool calls?|function calls?|my instructions|these instructions|my rules)\b|"
    r"(قاعدة البيانات|نموذج لغوي|تعليمات النظام)",
    re.I,
)


@dataclass(frozen=True)
class Failure:
    check: Check
    action: Action
    detail: str


@dataclass
class ValidationResult:
    failures: list[Failure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def must_escalate(self) -> bool:
        return any(f.action == "escalate" for f in self.failures)

    def reasons(self) -> str:
        return "; ".join(f"{f.check}: {f.detail}" for f in self.failures)


@dataclass(frozen=True)
class ValidationInput:
    reply: str
    tool_results: list[dict[str, Any]]
    language: str  # en | ar | ar-latn — the customer's
    customer_text: str = ""
    trusted_context: str = ""  # DB-rendered customer block
    previous_outbound: str | None = None
    is_new_conversation: bool = False
    disclosure_markers: tuple[str, ...] = ()
    other_tenant_names: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()


def validate(inp: ValidationInput) -> ValidationResult:
    result = ValidationResult()
    reply = inp.reply.strip()

    # 6. empty / duplicate
    if not reply:
        result.failures.append(Failure("empty_or_duplicate", "escalate", "empty reply"))
        return result
    if inp.previous_outbound is not None and reply == inp.previous_outbound.strip():
        result.failures.append(
            Failure("empty_or_duplicate", "escalate", "identical to previous reply")
        )
        return result

    tool_text = json.dumps(inp.tool_results, ensure_ascii=False, default=str)
    allowed = _allowed_numbers(inp.tool_results) | numbers_in(inp.trusted_context, skip_dates=True)

    # 1. numbers
    unbacked = sorted(
        (str(n) for n in _money_figures(reply) - allowed),
        key=lambda s: (len(s), s),
    )
    customer_numbers = numbers_in(inp.customer_text)
    unbacked += sorted(str(n) for n in _bottle_counts(reply) - allowed - customer_numbers)
    if unbacked:
        result.failures.append(
            Failure(
                "numbers",
                "regenerate",
                "figures not from a tool result: " + ", ".join(dict.fromkeys(unbacked)),
            )
        )

    # 2. forbidden promises
    tool_lower = tool_text.lower()
    for sentence in _SENTENCE_SPLIT.split(reply):
        low = sentence.lower()
        for term in _PROMISE_TERMS:
            idx = low.find(term)
            if idx < 0 or term in tool_lower:
                continue
            if _NEGATION.search(low[max(0, idx - _NEGATION_WINDOW) : idx]):
                continue
            result.failures.append(
                Failure("forbidden_promise", "escalate", f"unbacked '{term.strip()}'")
            )
            break

    # 3. leak
    leaks = {m.group(0).lower() for m in _LEAK.finditer(reply)}
    leaks |= {t for t in inp.tool_names if t in reply}
    leaks |= {
        name
        for name in inp.other_tenant_names
        if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", reply, re.I)
    }
    if leaks:
        result.failures.append(
            Failure("leak", "regenerate", "mentions " + ", ".join(sorted(leaks)))
        )

    # 4. length
    if len(reply) > MAX_REPLY_CHARS:
        result.failures.append(
            Failure("length", "regenerate", f"{len(reply)} characters, max {MAX_REPLY_CHARS}")
        )

    # 5. language
    expected = "ar" if inp.language == "ar" else "latin"
    got = script_of(reply)
    customer_mixed = mixed_script(inp.customer_text)
    if got != "none" and got != expected and not customer_mixed:
        result.failures.append(
            Failure(
                "language", "regenerate", f"reply is {got} script, customer wrote {inp.language}"
            )
        )

    # 7. AI disclosure on the first reply
    if inp.is_new_conversation and inp.disclosure_markers:
        low = reply.lower()
        if not any(m.lower() in low for m in inp.disclosure_markers if m):
            result.failures.append(
                Failure(
                    "ai_disclosure",
                    "regenerate",
                    "first reply must say it is an automated assistant",
                )
            )
    return result


# ---------------------------------------------------------------- helpers


def _allowed_numbers(tool_results: list[dict[str, Any]]) -> set[Decimal]:
    out: set[Decimal] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int | float | Decimal):
            d = to_decimal(str(v))
            if d is not None:
                out.add(d)
        elif isinstance(v, str):
            out.update(numbers_in(v, skip_dates=True))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)

    walk(tool_results)
    return out


def _capture(patterns: tuple[re.Pattern[str], ...], text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for p in patterns:
        for m in p.finditer(text):
            d = to_decimal(m.group(1))
            if d is not None:
                out.add(d)
    return out


def _money_figures(reply: str) -> set[Decimal]:
    text = normalise_digits(reply)
    figures = _capture(_CURRENCY, text)
    for m in _DECIMAL_AMOUNT.finditer(text):
        d = to_decimal(m.group(1))
        if d is not None:
            figures.add(d)
    return figures


def _bottle_counts(reply: str) -> set[Decimal]:
    return _capture(_BOTTLES, normalise_digits(reply))

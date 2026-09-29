"""Intent classification (02_agent_prompts §1).

Two layers:
1. `explicit_optout()` — deterministic phrase match, NO model call. STOP handling is absolute
   (01 §10.2) and scenarios 6/7 require "no LLM call made", so explicit opt-out phrases never
   reach a model. Deliberately narrow: "please stop the delivery tomorrow" is NOT an opt-out.
2. `classify()` — Flash-Lite, JSON mode, temperature 0, prompt verbatim from the spec. Catches
   fuzzier opt-outs and everything else. Any unparseable or out-of-enum output → 'unknown'.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

from api.agents.prompts import compose
from api.agents.text import script_of
from api.llm.router import LLM, LLMResult
from api.modules import registry

# Core intents, handled by the turn runner itself; every other intent comes from a module and goes
# to the support agent.
Intent = str
CORE_INTENTS: Final = frozenset(i.name for i in compose.CORE_INTENTS)
LANGUAGES: Final = frozenset(["en", "ar", "ar-latn"])

# 02 §1 says max output 20 tokens; the required JSON is ~25 tokens for ar-latn answers, so 20
# would truncate it. 40 is the smallest safe value.
CLASSIFIER_MAX_TOKENS: Final = 40

_OPTOUT_EXACT: Final = frozenset(
    {
        "stop",
        "stop all",
        "unsubscribe",
        "opt out",
        "optout",
        "cancel subscription",
        "توقف",
        "ايقاف",
        "إيقاف",
        "الغاء الاشتراك",
        "إلغاء الاشتراك",
        "stop please",
        "please stop",
    }
)
_OPTOUT_PHRASES: Final = (
    re.compile(r"\bunsubscribe\b", re.I),
    re.compile(r"\bopt[\s-]?out\b", re.I),
    re.compile(r"\b(stop|quit)\s+(messaging|texting|sending|contacting|spamming)\b", re.I),
    re.compile(r"\b(don'?t|do not|never)\s+(message|text|contact|send)\s+(me|us)\b", re.I),
    re.compile(r"\bno more (messages|offers|promotions)\b", re.I),
    re.compile(r"لا\s*(ترسل|ترسلوا|تراسلني|تراسلوني)"),
    re.compile(r"(اوقف|أوقف|أوقفوا|اوقفوا)\s*(الرسائل|الإرسال|الارسال)"),
    re.compile(r"(الغاء|إلغاء)\s*الاشتراك"),
)
_PUNCT = re.compile(r"[^\w\s؀-ۿ'-]")


def explicit_optout(text: str) -> bool:
    norm = _PUNCT.sub("", text).strip().lower()
    norm = re.sub(r"\s+", " ", norm)
    if norm in _OPTOUT_EXACT:
        return True
    return any(p.search(text) for p in _OPTOUT_PHRASES)


def guess_language(text: str) -> str:
    """Script-based guess for paths that run without the classifier (e.g. explicit STOP)."""
    return "ar" if script_of(text) == "ar" else "en"


@dataclass(frozen=True)
class Classification:
    intent: Intent
    language: str
    confidence: float
    llm: LLMResult | None


async def classify(
    llm: LLM,
    *,
    provider: str,
    model: str,
    text: str,
    modules: tuple[str, ...] = (),
) -> Classification:
    """`modules`: the tenant's enabled module keys — their intents are added to the prompt."""
    prompt = compose.render_classifier(modules, message_text=text)
    allowed = compose.intents(registry.resolve(modules))
    result = await llm.chat(
        provider=provider,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=CLASSIFIER_MAX_TOKENS,
        json_mode=True,
    )
    intent, language, confidence = _parse(
        result.content or "", fallback_language=guess_language(text), allowed=allowed
    )
    return Classification(intent=intent, language=language, confidence=confidence, llm=result)


def _parse(
    raw: str, *, fallback_language: str, allowed: frozenset[str] = CORE_INTENTS
) -> tuple[Intent, str, float]:
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    intent = data.get("intent")
    language = data.get("language")
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        intent if isinstance(intent, str) and intent in allowed else "unknown",
        language if language in LANGUAGES else fallback_language,
        confidence,
    )

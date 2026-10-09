"""Assemble a tenant's prompts from the core templates and its enabled modules.

Composition happens on template SOURCE, before Jinja renders it: each `<<slot>>` marker in the core
template is replaced with the weighted `Line`s the enabled modules contribute. The result is a
normal template, compiled once per module set and cached.

For the water preset (catalog + orders + coupons) the composed source is exactly
02_agent_prompts.md §2 and §1 — asserted by test_classifier_and_prompts — so splitting the prompt
into modules changed nothing for Aquamena.

Slots in support_core_v1:
  <<capabilities>>    "- …" bullets under WHAT YOU CAN DO
  <<facts>>           the things rule 1 forbids stating without a tool result, "a, b, c or d"
  <<rules>>           extra "#. …" rules, between core rules 2 and 3
  <<sections>>        whole "## …" sections; a section may hold <<steps:NAME>>, filled by every
                      module's steps[NAME]
  <<business_facts>>  lines under BUSINESS FACTS
Lines starting "#. " are numbered 1, 2, 3… within each "## " section.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Final

import jinja2

from api.agents.prompts import env
from api.modules.base import Intent, Module

TEMPLATES: Final = Path(__file__).parent / "templates"
SUPPORT_CORE: Final = "support_core_v1"
CLASSIFIER_CORE: Final = "classifier_core_v1"

# Core intents come after every module intent; their text is the spec's.
CORE_INTENTS: Final[tuple[Intent, ...]] = (
    Intent(
        1000,
        "complaint",
        "Unhappy: late, damaged, wrong item, rude driver, billing dispute, refund request.",
    ),
    Intent(
        1010,
        "optout",
        'Wants to stop receiving messages. "STOP", "unsubscribe", "لا ترسل", "don\'t message me".',
    ),
    Intent(1020, "support", "A question about the business that is none of the above."),
    Intent(1030, "smalltalk", "Greeting, thanks, emoji only, or no actionable content."),
    Intent(1040, "unknown", "Cannot tell."),
)

# Used only when no enabled module fills a slot (a core-only tenant).
DEFAULT_CAPABILITY: Final = "- Answer questions about {{business_name}}"
DEFAULT_FACTS: Final = "a price, a date or any other business detail"
DEFAULT_NEW_CONTACT: Final = (
    "This is a new contact. You do not know their name or history yet.\n"
    "Ask their name once, politely."
)

_STEPS = re.compile(r"<<steps:([a-z_]+)>>")


def _ordered(modules: Iterable[Module], pick: str) -> list[str]:
    lines: list[tuple[int, int, str]] = []
    for i, m in enumerate(modules):
        for line in getattr(m, pick):
            lines.append((line.weight, i, line.text))
    return [text for _, _, text in sorted(lines)]


def _steps(modules: tuple[Module, ...], name: str) -> list[str]:
    lines: list[tuple[int, int, str]] = []
    for i, m in enumerate(modules):
        for line in m.steps.get(name, ()):
            lines.append((line.weight, i, line.text))
    return [text for _, _, text in sorted(lines)]


def join_facts(facts: list[str]) -> str:
    if not facts:
        return DEFAULT_FACTS
    if len(facts) == 1:
        return facts[0]
    return f"{', '.join(facts[:-1])} or {facts[-1]}"


def _fill_line_slot(source: str, marker: str, lines: list[str]) -> str:
    """Replace a marker that sits on its own line; with nothing to insert, the line disappears."""
    token = f"<<{marker}>>\n"
    if token not in source:
        raise ValueError(f"template has no {marker} slot")
    return source.replace(token, "".join(f"{text}\n" for text in lines))


def number(source: str) -> str:
    out: list[str] = []
    n = 0
    for line in source.split("\n"):
        if line.startswith("## "):
            n = 0
        if line.startswith("#. "):
            n += 1
            line = f"{n}. {line[3:]}"
        out.append(line)
    return "\n".join(out)


def _core(name: str) -> str:
    return (TEMPLATES / f"{name}.j2").read_text()


def support_source(modules: tuple[Module, ...]) -> str:
    src = _core(SUPPORT_CORE)
    src = _fill_line_slot(
        src, "capabilities", _ordered(modules, "capabilities") or [DEFAULT_CAPABILITY]
    )
    src = src.replace("<<facts>>", join_facts(_ordered(modules, "facts")))
    src = _fill_line_slot(src, "rules", _ordered(modules, "rules"))
    sections = [
        _STEPS.sub(lambda mt: "\n".join(_steps(modules, mt.group(1))), text)
        for text in _ordered(modules, "sections")
    ]
    src = src.replace("<<sections>>\n", "".join(f"{s}\n\n" for s in sections))
    src = _fill_line_slot(src, "business_facts", _ordered(modules, "business_facts"))
    return number(src)


def classifier_source(modules: tuple[Module, ...]) -> str:
    src = _core(CLASSIFIER_CORE)
    intents = sorted(
        [(it.weight, i, it) for i, m in enumerate(modules) for it in m.intents]
        + [(it.weight, len(modules), it) for it in CORE_INTENTS],
        key=lambda t: (t[0], t[1]),
    )
    lines = [f"{it.name:<14}{it.description}" for _, _, it in intents]
    src = _fill_line_slot(src, "intents", lines)
    return _fill_line_slot(src, "intent_rules", _ordered(modules, "intent_rules"))


def intents(modules: tuple[Module, ...]) -> frozenset[str]:
    return frozenset(it.name for m in modules for it in m.intents) | {i.name for i in CORE_INTENTS}


def new_contact_text(modules: tuple[Module, ...]) -> str:
    lines = [m.new_contact for m in modules if m.new_contact is not None]
    return max(lines, key=lambda line: line.weight).text if lines else DEFAULT_NEW_CONTACT


def context_description(modules: tuple[Module, ...]) -> str:
    """get_customer_context's description, naming what the enabled modules add to it."""
    fetches = ["profile", *_ordered(modules, "context_fetches")]
    topics = _ordered(modules, "context_topics") or ["their account"]
    what = fetches[0] if len(fetches) == 1 else f"{', '.join(fetches[:-1])} and {fetches[-1]}"
    about = topics[0] if len(topics) == 1 else f"{', '.join(topics[:-1])}, or {topics[-1]}"
    return f"Fetch this customer's {what}. Call this before answering any question about {about}."


@lru_cache(maxsize=64)
def _compiled(kind: str, keys: tuple[str, ...], channel_name: str = "WhatsApp") -> jinja2.Template:
    from api.modules import registry

    modules = registry.resolve(keys)
    source = support_source(modules) if kind == "support" else classifier_source(modules)
    if channel_name != "WhatsApp":
        # The released templates name WhatsApp (and stay verbatim for it). On another channel the
        # same text names that channel; only the template text changes, never a variable's value.
        source = source.replace("WhatsApp", channel_name)
    return env.from_string(source)


def render_support(
    keys: tuple[str, ...], *, channel_name: str = "WhatsApp", **variables: object
) -> str:
    return _compiled("support", keys, channel_name).render(**variables).strip()


def render_classifier(
    keys: tuple[str, ...], *, channel_name: str = "WhatsApp", **variables: object
) -> str:
    return _compiled("classifier", keys, channel_name).render(**variables).strip()


def version(keys: tuple[str, ...], channel_kind: str = "whatsapp") -> str:
    """Recorded on every agent message (messages.prompt_version). A non-WhatsApp channel's
    turns carry "@<kind>": the same release, with the channel's name in the text."""
    base = f"{SUPPORT_CORE}[{','.join(keys)}]+{CLASSIFIER_CORE}"
    return base if channel_kind == "whatsapp" else f"{base}@{channel_kind}"

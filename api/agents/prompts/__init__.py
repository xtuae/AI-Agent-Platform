"""Versioned prompt templates (Jinja). A prompt is a deployable artifact: every outbound message
records the version that produced it (messages.prompt_version).

The support and classifier prompts are composed per tenant from core templates plus the enabled
modules' pieces (see compose.py). For the water preset the composed text is 02_agent_prompts.md §2
and §1 VERBATIM — change the spec first, then add a v2 file; never edit a released version in place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import jinja2

SUMMARY: Final = "summary_v1"
CORRECTIVE: Final = "corrective_v1"
CANNED: Final = "canned_v1"

# Plain-text LLM prompts, never rendered as HTML, so HTML autoescaping would corrupt them.
env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    undefined=jinja2.StrictUndefined,
    autoescape=False,  # noqa: S701
    keep_trailing_newline=False,
)


def render(name: str, **variables: Any) -> str:
    return env.get_template(f"{name}.j2").render(**variables).strip()

"""How each template variable is filled for one recipient.

A campaign stores one binding per template variable, in order:
  {"source": "contact.first_name", "fallback": "there"}   — from the contact, with a fallback
  {"source": "text", "value": "10% off snacks"}            — the same for everyone
Meta rejects an empty parameter, so every binding must end up non-empty.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, Field, model_validator

from api.db.models import Customer

Source = Literal["contact.name", "contact.first_name", "contact.area", "text"]
SOURCES: Final[dict[str, str]] = {
    "contact.first_name": "First name",
    "contact.name": "Full name",
    "contact.area": "Area",
    "text": "Fixed text",
}
MAX_VALUE: Final = 60  # a parameter, not a paragraph


class Binding(BaseModel):
    source: Source
    value: str | None = Field(default=None, max_length=MAX_VALUE)
    fallback: str | None = Field(default=None, max_length=MAX_VALUE)

    @model_validator(mode="after")
    def _complete(self) -> Binding:
        if self.source == "text" and not (self.value or "").strip():
            raise ValueError("fixed text needs a value")
        if self.source != "text" and not (self.fallback or "").strip():
            raise ValueError("a contact field needs a fallback for contacts without it")
        return self


def _clean(text: str | None) -> str:
    # Meta rejects newlines, tabs and runs of 4+ spaces inside a parameter
    return " ".join((text or "").split())[:MAX_VALUE]


def value_for(binding: Binding, customer: Customer) -> str:
    if binding.source == "text":
        return _clean(binding.value)
    raw = {
        "contact.name": customer.name,
        "contact.first_name": (customer.name or "").split()[0]
        if (customer.name or "").split()
        else None,
        "contact.area": customer.area,
    }[binding.source]
    return _clean(raw) or _clean(binding.fallback)


def values_for(bindings: list[Binding], customer: Customer) -> list[str]:
    return [value_for(b, customer) for b in bindings]


def components(values: list[str]) -> list[dict[str, Any]] | None:
    """The send-template body component for these values."""
    if not values:
        return None
    return [{"type": "body", "parameters": [{"type": "text", "text": v} for v in values]}]


def parse(raw: Any) -> list[Binding]:
    return [Binding.model_validate(b) for b in (raw or [])]

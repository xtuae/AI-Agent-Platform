"""Declarative segments (02 §4.3): JSON in, SQL out, built by the application — never by a model.

A definition is a flat object of field → value, all ANDed:

    {"opt_in_status": "opted_in", "last_order_before_days": 60, "area_in": ["Al Nahda"]}

Fields come from the core (area, emirate, language) and from the tenant's enabled modules
(orders: last_order_before_days …, coupons: bottles_remaining_lte …). An unknown field is an
error, never ignored — a typo must not silently widen a campaign.

EVERY compiled segment is `opt_in_status = 'opted_in' AND (the fields)`. The opt-in condition is
added here, outside anything a field returns, so no definition can reach a pending or opted-out
customer. `opt_in_status` may appear in a definition (the spec's examples carry it) but only with
the value 'opted_in'.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import ColumnElement, Select, and_, func, select, true

from api.db.models import Customer
from api.modules.base import SegmentField, SegmentScope
from api.modules.registry import Enabled

OPTED_IN: Final = "opted_in"
MAX_FIELDS: Final = 12
MAX_LIST: Final = 50
MAX_TEXT: Final = 120


class SegmentError(ValueError):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def _lower_in(column: Any, values: list[str]) -> ColumnElement[bool]:
    return func.lower(func.trim(column)).in_([v.strip().lower() for v in values])


CORE_FIELDS: Final[tuple[SegmentField, ...]] = (
    SegmentField(
        name="area_in",
        label="Area is one of",
        kind="text_list",
        help="Customers whose area is one of these",
        clause=lambda v, _s: _lower_in(Customer.area, v),
    ),
    SegmentField(
        name="emirate_in",
        label="Emirate is one of",
        kind="text_list",
        help="Customers in these emirates",
        clause=lambda v, _s: _lower_in(Customer.emirate, v),
    ),
    SegmentField(
        name="language_in",
        label="Language is one of",
        kind="text_list",
        help="The language they write to you in",
        choices=("en", "ar", "ar-latn"),
        clause=lambda v, _s: _lower_in(Customer.language, v),
    ),
)


def fields(enabled: Enabled) -> dict[str, SegmentField]:
    """The fields a tenant's segments may use: core plus its enabled modules'."""
    out = {f.name: f for f in CORE_FIELDS}
    for m in enabled.modules:
        for f in m.segment_fields:
            if f.name in out or f.name == "opt_in_status":
                raise ValueError(f"segment field {f.name!r} is defined twice")
            out[f.name] = f
    return out


def _value(f: SegmentField, raw: Any) -> Any:
    if f.kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SegmentError("invalid_value", field=f.name, expected="a whole number")
        if not f.minimum <= raw <= f.maximum:
            raise SegmentError("invalid_value", field=f.name, minimum=f.minimum, maximum=f.maximum)
        return raw
    if f.kind == "text":
        if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_TEXT:
            raise SegmentError("invalid_value", field=f.name, expected="text")
        value = raw.strip()
        if f.choices and value not in f.choices:
            raise SegmentError("invalid_value", field=f.name, choices=list(f.choices))
        return value
    # text_list
    if (
        not isinstance(raw, list)
        or not 1 <= len(raw) <= MAX_LIST
        or not all(isinstance(x, str) and x.strip() and len(x) <= MAX_TEXT for x in raw)
    ):
        raise SegmentError("invalid_value", field=f.name, expected=f"1-{MAX_LIST} texts")
    values = [x.strip() for x in raw]
    if f.choices and any(v not in f.choices for v in values):
        raise SegmentError("invalid_value", field=f.name, choices=list(f.choices))
    return values


def validate(definition: Any, enabled: Enabled) -> dict[str, Any]:
    """A clean copy of the definition, or SegmentError. Always carries opt_in_status."""
    if not isinstance(definition, dict):
        raise SegmentError("not_an_object")
    if len(definition) > MAX_FIELDS:
        raise SegmentError("too_many_fields", maximum=MAX_FIELDS)
    known = fields(enabled)
    clean: dict[str, Any] = {"opt_in_status": OPTED_IN}
    for key, raw in definition.items():
        if key == "opt_in_status":
            if raw != OPTED_IN:
                raise SegmentError("opt_in_only", field=key)
            continue
        f = known.get(key)
        if f is None:
            raise SegmentError("unknown_field", field=key, available=sorted(known))
        clean[key] = _value(f, raw)
    return clean


def condition(definition: Any, enabled: Enabled, scope: SegmentScope) -> ColumnElement[bool]:
    """The WHERE condition over customers for a definition."""
    clean = validate(definition, enabled)
    known = fields(enabled)
    clauses = [known[k].clause(v, scope) for k, v in clean.items() if k != "opt_in_status"]
    # the opt-in filter wraps everything a field produced
    return and_(Customer.opt_in_status == OPTED_IN, and_(true(), *clauses))


def customers(definition: Any, enabled: Enabled, scope: SegmentScope) -> Select[tuple[Customer]]:
    return select(Customer).where(condition(definition, enabled, scope))


def count(definition: Any, enabled: Enabled, scope: SegmentScope) -> Select[tuple[int]]:
    return select(func.count()).select_from(Customer).where(condition(definition, enabled, scope))


def describe(enabled: Enabled) -> list[dict[str, Any]]:
    """For the dashboard's segment builder."""
    return [
        {
            "name": f.name,
            "label": f.label,
            "kind": f.kind,
            "help": f.help,
            "minimum": f.minimum,
            "maximum": f.maximum,
            "choices": list(f.choices),
        }
        for f in fields(enabled).values()
    ]

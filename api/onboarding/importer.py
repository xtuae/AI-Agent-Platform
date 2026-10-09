"""Customer spreadsheet → customers table. Plan first (pure), then apply (one tenant transaction).

Rules — each chosen so that running the same file twice changes nothing:
* One customer per WhatsApp id. Rows with the same number are merged: the first non-empty value
  of each field wins, and a disagreement is reported ("conflicting name …"), never silently lost.
* An existing customer is only ever FILLED IN: a blank field takes the file's value; a field that
  already has a value keeps it (the customer may have corrected it on WhatsApp) and the difference
  is reported as a conflict. A second run finds nothing blank, so it writes nothing.
* Imported customers are `opt_in_status = 'pending'`. A spreadsheet is not consent: opt-in only
  comes with evidence (a consent page, or the customer saying yes on WhatsApp).
* Similar names on DIFFERENT numbers in the same area are reported as possible duplicates and
  left alone — merging two phone numbers would lose one of them.
* Nothing about a person is logged; the audit_log row holds counts and the file's sha256.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import AuditLog, Customer
from api.db.session import TENANT_KEY
from api.onboarding.phones import normalize_phone
from api.onboarding.sheets import Table

Target = Literal[
    "phone",
    "name",
    "first_name",
    "last_name",
    "area",
    "emirate",
    "language",
    "address_note",
    "external_ref",
    "ignore",
]
TARGETS: Final = get_args(Target)
# the customer fields an import may set, in report order
FIELDS: Final = ("name", "area", "emirate", "language", "address_note", "external_ref")
SOURCE: Final = "excel_import"
CHUNK: Final = 1000
SIMILAR_NAME: Final = 0.9

# header text → target; matched on whole words of the casefolded header (en + ar)
_SYNONYMS: Final[tuple[tuple[Target, tuple[str, ...]], ...]] = (
    ("phone", ("mobile", "phone", "whatsapp", "contact no", "contact number", "tel", "cell",
               "number", "جوال", "هاتف", "موبايل", "رقم", "واتساب")),
    ("first_name", ("first name", "firstname", "given name", "الاسم الأول")),
    ("last_name", ("last name", "lastname", "surname", "family name", "اسم العائلة")),
    ("name", ("name", "customer", "client", "الاسم", "اسم", "العميل")),
    ("emirate", ("emirate", "city", "الإمارة", "الامارة", "إمارة", "المدينة")),
    ("area", ("area", "location", "community", "district", "zone", "المنطقة", "منطقة")),
    ("address_note", ("address", "building", "villa", "flat", "apartment", "street",
                      "landmark", "العنوان", "عنوان", "بناية", "فيلا", "شقة")),
    ("language", ("language", "lang", "اللغة")),
    ("external_ref", ("id", "ref", "reference", "customer id", "code", "account", "رقم العميل")),
)  # fmt: skip

EMIRATES: Final[dict[str, str]] = {
    "dubai": "Dubai", "dxb": "Dubai", "دبي": "Dubai",
    "abu dhabi": "Abu Dhabi", "abudhabi": "Abu Dhabi", "auh": "Abu Dhabi", "أبوظبي": "Abu Dhabi",
    "ابوظبي": "Abu Dhabi", "أبو ظبي": "Abu Dhabi", "ابو ظبي": "Abu Dhabi",
    "sharjah": "Sharjah", "shj": "Sharjah", "الشارقة": "Sharjah",
    "ajman": "Ajman", "عجمان": "Ajman",
    "umm al quwain": "Umm Al Quwain", "umm al-quwain": "Umm Al Quwain", "uaq": "Umm Al Quwain",
    "أم القيوين": "Umm Al Quwain", "ام القيوين": "Umm Al Quwain",
    "ras al khaimah": "Ras Al Khaimah", "ras al-khaimah": "Ras Al Khaimah",
    "rak": "Ras Al Khaimah", "رأس الخيمة": "Ras Al Khaimah", "راس الخيمة": "Ras Al Khaimah",
    "fujairah": "Fujairah", "fujeirah": "Fujairah", "fuj": "Fujairah", "الفجيرة": "Fujairah",
}  # fmt: skip
LANGUAGES: Final[dict[str, str]] = {
    "en": "en", "english": "en", "eng": "en", "انجليزي": "en", "الإنجليزية": "en",
    "ar": "ar", "arabic": "ar", "عربي": "ar", "العربية": "ar",
}  # fmt: skip


class Mapping(BaseModel):
    """Which column feeds which field. Saved as JSON and reused for the next file of the same
    layout (`--mapping`). Several columns may feed phone (tried in order) or address_note
    (joined)."""

    model_config = ConfigDict(extra="forbid")

    sheet: str | None = None
    header_row: int | None = Field(default=None, ge=1)
    columns: dict[str, Target]


def suggest(columns: list[str]) -> dict[str, Target]:
    out: dict[str, Target] = {}
    for col in columns:
        words = " " + re.sub(r"[^\w\s]", " ", col.casefold()) + " "
        out[col] = "ignore"
        for target, keys in _SYNONYMS:
            if any(f" {k} " in words for k in keys):
                out[col] = target
                break
    return out


# ---------------------------------------------------------------- cleaning


def _str(v: Any) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = " ".join(unicodedata.normalize("NFKC", str(v)).split()).strip(" -_.,;:/")
    return s or None


def clean_name(v: Any) -> str | None:
    s = _str(v)
    if s is None or not re.search(r"[^\W\d_]", s):  # no letters at all: "-", "0", "N/A"-ish
        return None
    if s.casefold() in ("na", "n/a", "none", "null", "unknown", "customer", "test"):
        return None
    if s.isascii() and (s.isupper() or s.islower()):
        s = s.title()
    return s


def clean_emirate(v: Any) -> str | None:
    s = _str(v)
    return EMIRATES.get(s.casefold(), s) if s else None


def clean_language(v: Any) -> str | None:
    s = _str(v)
    return LANGUAGES.get(s.casefold()) if s else None


_TASHKEEL = re.compile(r"[ً-ٰٟـ]")
_ALEF = str.maketrans("أإآٱ", "اااا")


def name_key(name: str) -> str:
    """Comparable form: case, spacing, Arabic diacritics and letter variants, word order."""
    s = _TASHKEEL.sub("", unicodedata.normalize("NFKC", name).casefold())
    s = s.translate(_ALEF).replace("ى", "ي").replace("ة", "ه")
    return " ".join(sorted(re.findall(r"[^\W\d_]+", s)))


# ---------------------------------------------------------------- plan


@dataclass
class Candidate:
    wa_id: str
    rows: list[int]
    values: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Rejected:
    row: int
    reason: str
    name: str | None


@dataclass
class Plan:
    rows_read: int
    blank_rows: int
    repeated_headers: int
    candidates: list[Candidate]
    rejected: list[Rejected]
    duplicate_rows: int  # rows folded into an earlier row with the same number
    possible_duplicates: list[tuple[str, str, str]]  # (wa_id, wa_id, why)


def plan(table: Table, mapping: Mapping) -> Plan:
    index = {name: i for i, name in enumerate(table.columns)}
    unknown = [c for c in mapping.columns if c not in index]
    if unknown:
        raise ValueError(f"mapping names columns this sheet does not have: {', '.join(unknown)}")
    by_target: dict[str, list[int]] = defaultdict(list)
    for col, target in mapping.columns.items():
        if target != "ignore":
            by_target[target].append(index[col])
    if not by_target.get("phone"):
        raise ValueError("the mapping has no phone column")

    candidates: dict[str, Candidate] = {}
    rejected: list[Rejected] = []
    duplicate_rows = 0
    for row_no, cells in table.rows:

        def cell(target: str, cells: list[Any] = cells) -> list[Any]:
            return [cells[i] for i in by_target.get(target, [])]

        name = next(filter(None, map(clean_name, cell("name"))), None)
        if name is None:
            parts = [clean_name(v) for v in (*cell("first_name"), *cell("last_name"))]
            name = " ".join(p for p in parts if p) or None

        phone = None
        reason = "no phone number"
        for raw in cell("phone"):
            p = normalize_phone(raw)
            if p.wa_id:
                phone = p.wa_id
                break
            if reason == "no phone number" and p.reason:
                reason = p.reason
        if phone is None:
            rejected.append(Rejected(row_no, reason, name))
            continue

        values: dict[str, str] = {}
        if name:
            values["name"] = name
        for target, fn in (
            ("area", _str),
            ("emirate", clean_emirate),
            ("language", clean_language),
            ("external_ref", _str),
        ):
            v = next(filter(None, map(fn, cell(target))), None)
            if v:
                values[target] = v
        address = ", ".join(dict.fromkeys(filter(None, map(_str, cell("address_note")))))
        if address:
            values["address_note"] = address

        c = candidates.get(phone)
        if c is None:
            candidates[phone] = Candidate(phone, [row_no], values)
            continue
        duplicate_rows += 1
        c.rows.append(row_no)
        for k, v in values.items():
            if k not in c.values:
                c.values[k] = v
            elif _differs(k, c.values[k], v):
                c.conflicts.append(f"row {row_no}: different {k.replace('_', ' ')} in the file")

    return Plan(
        rows_read=len(table.rows),
        blank_rows=table.blank_rows,
        repeated_headers=table.repeated_headers,
        candidates=list(candidates.values()),
        rejected=rejected,
        duplicate_rows=duplicate_rows,
        possible_duplicates=_similar(list(candidates.values())),
    )


def _differs(key: str, a: str, b: str) -> bool:
    if key == "name":
        return name_key(a) != name_key(b)
    return a.casefold() != b.casefold()


def _similar(candidates: list[Candidate]) -> list[tuple[str, str, str]]:
    """Different numbers, near-identical names, same area. Blocked by area + name prefix so a
    10,000-row sheet compares thousands of pairs, not fifty million."""
    blocks: dict[tuple[str, str], list[tuple[str, Candidate]]] = defaultdict(list)
    for c in candidates:
        name = c.values.get("name")
        if not name:
            continue
        key = name_key(name)
        if len(key) < 6 or " " not in key:  # "Ali" alone matches half a city
            continue
        blocks[(c.values.get("area", "").casefold(), key[:2])].append((key, c))
    out: list[tuple[str, str, str]] = []
    for members in blocks.values():
        for i, (ka, a) in enumerate(members):
            for kb, b in members[i + 1 :]:
                ratio = difflib.SequenceMatcher(None, ka, kb).ratio()
                if ratio >= SIMILAR_NAME:
                    out.append((a.wa_id, b.wa_id, f"similar names ({ratio:.0%}) in the same area"))
    return out


# ---------------------------------------------------------------- apply


@dataclass
class Outcome:
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # (wa_id, what)
    new_ids: list[uuid.UUID] = field(default_factory=list)


async def apply(s: AsyncSession, p: Plan, *, actor: str, file_sha256: str, commit: bool) -> Outcome:
    """Write the plan inside the caller's tenant transaction. `commit=False` computes the same
    outcome and writes nothing (the caller rolls back)."""
    existing: dict[str, Customer] = {}
    wa_ids = [c.wa_id for c in p.candidates]
    for i in range(0, len(wa_ids), CHUNK):
        found = await s.scalars(select(Customer).where(Customer.wa_id.in_(wa_ids[i : i + CHUNK])))
        existing.update((c.wa_id, c) for c in found if c.wa_id)
    out = Outcome()
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for cand in p.candidates:
        out.conflicts.extend((cand.wa_id, c) for c in cand.conflicts)
        row = existing.get(cand.wa_id)
        if row is None:
            new_id = uuid.uuid4()
            inserts.append(
                {
                    "id": new_id,
                    "wa_id": cand.wa_id,
                    "source": SOURCE,
                    "opt_in_status": "pending",
                    **{k: cand.values.get(k) for k in FIELDS},
                }
            )
            out.new_ids.append(new_id)
            continue
        fill: dict[str, Any] = {}
        for k in FIELDS:
            v = cand.values.get(k)
            if v is None:
                continue
            current = getattr(row, k)
            if current is None or not str(current).strip():
                fill[k] = v
            elif _differs(k, str(current), v):
                out.conflicts.append((cand.wa_id, f"kept existing {k.replace('_', ' ')}"))
        if fill:
            updates.append({"id": row.id, **fill})
        else:
            out.unchanged += 1

    if commit:
        tenant_id = s.info[TENANT_KEY]
        for i in range(0, len(inserts), CHUNK):
            chunk = [{"tenant_id": tenant_id, **r} for r in inserts[i : i + CHUNK]]
            result = await s.execute(
                insert(Customer)
                .values(chunk)
                .on_conflict_do_nothing(index_elements=["tenant_id", "wa_id"])
                .returning(Customer.id)
            )
            out.imported += len(result.all())  # a number that messaged in meanwhile: skipped
        for u in updates:
            await s.execute(
                update(Customer)
                .where(Customer.id == u["id"])
                .values({k: v for k, v in u.items() if k != "id"})
            )
        out.updated = len(updates)
        s.add(
            AuditLog(
                actor=actor,
                action="import_customers",
                entity="customers",
                after={
                    "file_sha256": file_sha256,
                    "rows_read": p.rows_read,
                    "imported": out.imported,
                    "updated": out.updated,
                    "unchanged": out.unchanged,
                    "rejected": len(p.rejected),
                    "duplicate_rows": p.duplicate_rows,
                },
            )
        )
    else:
        out.imported, out.updated = len(inserts), len(updates)
    return out

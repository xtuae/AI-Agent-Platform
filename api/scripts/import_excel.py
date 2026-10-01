"""Import a tenant's customer spreadsheet (HMH Labz staff, during onboarding).

    python -m api.scripts.import_excel --slug acme customers.xlsx               # dry run
    python -m api.scripts.import_excel --slug acme customers.xlsx --commit
    python -m api.scripts.import_excel --slug acme customers.xlsx --save-mapping acme.json
    python -m api.scripts.import_excel --slug acme next-month.xlsx --mapping acme.json --commit

Without --mapping, each column is shown with sample values and a suggested field; press Enter to
accept or type another field (phone, name, first_name, last_name, area, emirate, language,
address_note, external_ref, ignore). Without a terminal the suggestions are used as they are.

Nothing is written without --commit. Every run writes a report CSV (default: next to the file)
listing each rejected row with its reason, each conflict, and each possible duplicate. The report
names customers — keep it where the spreadsheet itself is kept. Running the same file again
changes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import sys
from collections import Counter
from pathlib import Path
from typing import cast

from sqlalchemy import select

from api.config import get_settings
from api.db.models import Tenant
from api.db.session import Database
from api.onboarding.importer import TARGETS, Mapping, Outcome, Plan, Target, apply, plan, suggest
from api.onboarding.sheets import SheetError, Table, read_table


class _RollbackError(Exception):
    """Raised inside the transaction to discard a dry run's work."""


def _ask_mapping(table: Table) -> dict[str, Target]:
    suggested = suggest(table.columns)
    if not sys.stdin.isatty():
        return suggested
    print(
        f"\nSheet {table.sheet!r}, header on row {table.header_row}. Fields: {', '.join(TARGETS)}"
    )
    out: dict[str, Target] = {}
    for i, col in enumerate(table.columns):
        samples = " | ".join(table.samples(i)) or "(empty)"
        while True:
            answer = input(f"  {col:<28} e.g. {samples[:60]:<60} [{suggested[col]}] ").strip()
            if not answer:
                out[col] = suggested[col]
                break
            if answer in TARGETS:
                out[col] = cast(Target, answer)
                break
            print(f"    choose one of: {', '.join(TARGETS)}")
    return out


def _write_report(path: Path, p: Plan, o: Outcome) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel opens Arabic right
        w = csv.writer(f)
        w.writerow(["kind", "rows", "whatsapp_id", "detail", "name"])
        for r in p.rejected:
            w.writerow(["rejected", r.row, "", r.reason, r.name or ""])
        for c in p.candidates:
            if len(c.rows) > 1:
                w.writerow(
                    ["merged_in_file", " ".join(map(str, c.rows)), c.wa_id, "same number",
                     c.values.get("name", "")]
                )  # fmt: skip
        for wa_id, what in o.conflicts:
            w.writerow(["conflict", "", wa_id, what, ""])
        for a, b, why in p.possible_duplicates:
            w.writerow(["possible_duplicate", "", f"{a} / {b}", why, ""])


def _summary(p: Plan, o: Outcome, *, committed: bool) -> str:
    verb = "" if committed else " (dry run — nothing written; add --commit)"
    lines = [
        f"Rows read {p.rows_read}  (blank rows skipped {p.blank_rows}, "
        f"repeated headers skipped {p.repeated_headers}){verb}",
        f"  imported (new)           {o.imported}",
        f"  merged into existing     {o.updated}",
        f"  unchanged                {o.unchanged}",
        f"  duplicate rows folded    {p.duplicate_rows}",
        f"  rejected                 {len(p.rejected)}",
    ]
    for reason, n in Counter(r.reason for r in p.rejected).most_common():
        lines.append(f"      {n:>6}  {reason}")
    lines.append(f"  conflicts (kept first)   {len(o.conflicts)}")
    lines.append(f"  possible duplicates      {len(p.possible_duplicates)}")
    return "\n".join(lines)


async def run(
    db: Database,
    slug: str,
    table: Table,
    mapping: Mapping,
    *,
    file_sha256: str,
    commit: bool,
) -> tuple[Plan, Outcome]:
    async with db.platform_session() as s:
        tenant_id = await s.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tenant_id is None:
        raise ValueError(f"no tenant with slug {slug!r}")
    p = plan(table, mapping)
    outcome: Outcome | None = None
    try:
        async with db.tenant_session(tenant_id) as s:
            outcome = await apply(
                s, p, actor=f"import:{file_sha256[:12]}", file_sha256=file_sha256, commit=commit
            )
            if not commit:
                raise _RollbackError
    except _RollbackError:
        pass
    assert outcome is not None
    return p, outcome


async def _import(
    slug: str, table: Table, mapping: Mapping, file_sha256: str, commit: bool
) -> tuple[Plan, Outcome]:
    db = Database(get_settings())
    try:
        return await run(db, slug, table, mapping, file_sha256=file_sha256, commit=commit)
    finally:
        await db.dispose()


def _main(args: argparse.Namespace) -> int:
    path = Path(args.file)
    mapping: Mapping | None = None
    if args.mapping:
        mapping = Mapping.model_validate_json(Path(args.mapping).read_text(encoding="utf-8"))
    table = read_table(
        path,
        sheet=args.sheet or (mapping.sheet if mapping else None),
        header_row=args.header_row or (mapping.header_row if mapping else None),
    )
    if mapping is None:
        mapping = Mapping(
            sheet=table.sheet, header_row=table.header_row, columns=_ask_mapping(table)
        )
    if args.save_mapping:
        Path(args.save_mapping).write_text(mapping.model_dump_json(indent=2), encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    p, outcome = asyncio.run(_import(args.slug, table, mapping, sha, args.commit))
    report = Path(args.report) if args.report else path.with_suffix(".import-report.csv")
    _write_report(report, p, outcome)
    print(_summary(p, outcome, committed=args.commit))
    print(f"Report: {report}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--sheet")
    ap.add_argument("--header-row", type=int)
    ap.add_argument("--mapping", help="saved mapping JSON to use")
    ap.add_argument("--save-mapping", help="write the mapping used to this JSON file")
    ap.add_argument("--report", help="report CSV path")
    ap.add_argument("--commit", action="store_true", help="write to the database")
    args = ap.parse_args(argv)
    try:
        return _main(args)
    except (SheetError, ValueError, OSError) as exc:  # ValidationError is a ValueError
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

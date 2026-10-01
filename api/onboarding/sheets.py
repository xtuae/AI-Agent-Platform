"""Read a customer spreadsheet (.xlsx or .csv) into a header and rows of raw cell values.

Assumes the file is messy:
* merged cells — every cell of a merged range takes the range's value (a merged "Area" cell
  down the side of a block applies to each row in the block);
* a title or notes above the table — the header is the first row with two or more DISTINCT text
  cells (a merged title repeats one value, so it never qualifies);
* blank rows and repeated header rows (per-page headers in exported reports) are skipped;
* duplicate column names get " (2)", " (3)"; an empty header becomes "Column C";
* CSV in UTF-8 (with or without BOM), Windows-1256 (Arabic Excel exports) or Latin-1.

Values are passed on untouched (int / float / str / datetime) — the phone normaliser needs to see
an Excel float as a float.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

HEADER_SCAN_ROWS = 30
CSV_ENCODINGS = ("utf-8-sig", "cp1256", "latin-1")


class SheetError(ValueError):
    pass


@dataclass
class Table:
    sheet: str
    header_row: int  # 1-based, as Excel shows it
    columns: list[str]
    rows: list[tuple[int, list[Any]]] = field(default_factory=list)  # (Excel row number, cells)
    blank_rows: int = 0
    repeated_headers: int = 0

    def samples(self, column: int, n: int = 3) -> list[str]:
        out: list[str] = []
        for _, cells in self.rows:
            v = cells[column] if column < len(cells) else None
            if _filled(v):
                out.append(str(v).strip()[:40])
            if len(out) == n:
                break
        return out


def _filled(v: Any) -> bool:
    return v is not None and not (isinstance(v, str) and not v.strip())


def _grid_from_xlsx(path: Path, sheet: str | None) -> tuple[str, list[list[Any]]]:
    wb = load_workbook(path, data_only=True)  # data_only: formula results, not formulas
    ws: object
    if sheet is not None:
        if sheet not in wb.sheetnames:
            raise SheetError(f"no sheet named {sheet!r}; sheets: {', '.join(wb.sheetnames)}")
        ws = wb[sheet]
    else:
        visible = [w for w in wb.worksheets if w.sheet_state == "visible" and w.max_row > 1]
        if not visible:
            raise SheetError("the workbook has no visible sheet with data")
        ws = visible[0]
    if not isinstance(ws, Worksheet):
        raise SheetError(f"{sheet!r} is not a worksheet")
    for rng in list(ws.merged_cells.ranges):
        value = ws.cell(rng.min_row, rng.min_col).value
        ws.unmerge_cells(str(rng))
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                ws.cell(r, c).value = value
    return ws.title, [list(row) for row in ws.iter_rows(values_only=True)]


def _grid_from_csv(path: Path) -> tuple[str, list[list[Any]]]:
    raw = path.read_bytes()
    for enc in CSV_ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover — latin-1 decodes anything
        raise SheetError("cannot decode the CSV file")
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return path.stem, [list(r) for r in csv.reader(io.StringIO(text), dialect)]


def _header_index(grid: list[list[Any]]) -> int:
    for i, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        texts = {str(v).strip().casefold() for v in row if isinstance(v, str) and v.strip()}
        if len(texts) >= 2:
            return i
    raise SheetError(f"no header row found in the first {HEADER_SCAN_ROWS} rows")


def _column_names(row: list[Any]) -> list[str]:
    names: list[str] = []
    seen: dict[str, int] = {}
    for i, v in enumerate(row):
        name = " ".join(str(v).split()) if _filled(v) else f"Column {get_column_letter(i + 1)}"
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return names


def read_table(path: Path, *, sheet: str | None = None, header_row: int | None = None) -> Table:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        title, grid = _grid_from_xlsx(path, sheet)
    elif suffix in (".csv", ".txt"):
        title, grid = _grid_from_csv(path)
    elif suffix == ".xls":
        raise SheetError("old .xls format: open it in Excel and save as .xlsx first")
    else:
        raise SheetError(f"unsupported file type {suffix!r} (use .xlsx or .csv)")
    if not grid:
        raise SheetError("the sheet is empty")

    h = header_row - 1 if header_row is not None else _header_index(grid)
    if not 0 <= h < len(grid):
        raise SheetError(f"header row {header_row} is outside the sheet")
    header = grid[h]
    width = max(len(r) for r in grid)
    header = header + [None] * (width - len(header))
    table = Table(sheet=title, header_row=h + 1, columns=_column_names(header))
    header_key = [str(v).strip().casefold() if _filled(v) else "" for v in header]

    for offset, row in enumerate(grid[h + 1 :], start=h + 2):
        cells = list(row) + [None] * (width - len(row))
        if not any(_filled(v) for v in cells):
            table.blank_rows += 1
            continue
        if [str(v).strip().casefold() if _filled(v) else "" for v in cells] == header_key:
            table.repeated_headers += 1
            continue
        table.rows.append((offset, cells))
    return table

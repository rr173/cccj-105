"""Column mapping and detail extraction over a validated logical grid.

The mapping is driven entirely by the configured header aliases — never by
taking "the Nth text of each row":

* a column whose top header matches an institution/project alias is the
  institution/project column (with two header rows its label cell must span
  both rows, so the role is unambiguous);
* a column group whose top header is a year (``2024年度`` / ``2024年`` /
  ``2024``) must contain exactly one sub-column whose header matches an
  amount alias; other sub-columns must match an ignore alias;
* anything else rejects the table (``unmapped_column`` /
  ``column_mapping`` / ``duplicate_year``).

Detail rows inherit institution/project values from merged cells above them
(the grid records exactly which physical cell each value came from).  An
amount cell must occupy exactly one grid position: a merged amount cell
spanning two year columns (``amount_spans_years``) or several data rows
(``amount_spans_rows``) rejects the whole table rather than copying one
amount into several facts.

Totals rows (labelled by the configured total aliases) are never booked as
details; each per-year total is reconciled against the sum of the recognized
details in unified yuan, and any difference or unparseable amount sends the
whole table to the pending area with its grid and candidate details.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

from .fundamount import (
    extract_table_units,
    fold_fullwidth_digits,
    format_yuan,
    parse_amount,
    split_unit_annotation,
    to_yuan,
)
from .fundconfig import FundingTablesConfig
from .fundgrid import Grid, GridCell, TableRejection

YEAR_RE = re.compile(r"^(\d{4})年?(度)?$")


def norm_label(text: str) -> str:
    """Header-label normal form: full-width digits folded, whitespace removed."""
    return re.sub(r"\s+", "", fold_fullwidth_digits(text))


def matches_alias(norm_text: str, aliases) -> bool:
    return any(norm_text == norm_label(alias) for alias in aliases)


@dataclass
class YearColumn:
    col: int
    year: int
    local_unit: str | None  # unit declared in the column header, if any
    top_cell: GridCell
    sub_cell: GridCell | None


@dataclass
class ColumnMap:
    institution_col: int
    project_col: int
    year_columns: list[YearColumn]


def _single_unit(units: list[str], where: str, grid: Grid) -> str | None:
    if len(set(units)) > 1:
        raise TableRejection(
            "conflicting_unit",
            f"{where}: conflicting units {sorted(set(units))}",
            category="data", grid=grid)
    return units[0] if units else None


def classify_columns(grid: Grid, cfg: FundingTablesConfig) -> ColumnMap:
    """Map grid columns onto institution / project / year-amount roles."""
    h = grid.header_rows
    inst_cols: list[int] = []
    proj_cols: list[int] = []
    groups: list[tuple[GridCell, list[int], int, list[str]]] = []
    for c in range(grid.width):
        top = grid.cell_at(0, c)
        sub = grid.cell_at(h - 1, c) if h == 2 else None
        top_core, top_units, top_unknown = split_unit_annotation(top.text)
        if top_unknown:
            raise TableRejection(
                "unknown_currency",
                f"column {c} header {top.text!r}: unrecognized currency "
                f"{sorted(set(top_unknown))}",
                category="data", grid=grid)
        top_norm = norm_label(top_core)
        is_inst = matches_alias(top_norm, cfg.institution_aliases)
        is_proj = matches_alias(top_norm, cfg.project_aliases)
        if is_inst or is_proj:
            if h == 2 and sub is not top and norm_label(sub.text):
                raise TableRejection(
                    "column_mapping",
                    f"column {c}: label header {top.text!r} must span both header "
                    f"rows (found sub-header {sub.text!r} underneath)", grid=grid)
            (inst_cols if is_inst else proj_cols).append(c)
            continue
        year_match = YEAR_RE.match(top_norm)
        if year_match:
            year = int(year_match.group(1))
            for cell, cols, existing_year, _units in groups:
                if cell is top:
                    cols.append(c)
                    break
                if existing_year == year:
                    raise TableRejection(
                        "duplicate_year",
                        f"year {year} appears in two separate header groups",
                        grid=grid)
            else:
                groups.append((top, [c], year, top_units))
            continue
        if matches_alias(top_norm, cfg.ignore_aliases):
            continue
        raise TableRejection(
            "unmapped_column",
            f"column {c} header {top.text!r} matches no configured alias "
            "(institution/project/year/ignore)", grid=grid)

    if len(inst_cols) != 1:
        raise TableRejection(
            "column_mapping",
            f"expected exactly 1 institution column, found {len(inst_cols)}",
            grid=grid)
    if len(proj_cols) != 1:
        raise TableRejection(
            "column_mapping",
            f"expected exactly 1 project column, found {len(proj_cols)}", grid=grid)
    if not groups:
        raise TableRejection("column_mapping", "no year columns found", grid=grid)

    year_columns: list[YearColumn] = []
    for top, cols, year, top_units in groups:
        amount_cols: list[tuple[int, str | None, GridCell | None]] = []
        for c in cols:
            sub = grid.cell_at(h - 1, c) if h == 2 else None
            if h == 1 or sub is top:
                local = _single_unit(top_units, f"column {c} header", grid)
                amount_cols.append((c, local, None))
                continue
            sub_core, sub_units, sub_unknown = split_unit_annotation(sub.text)
            if sub_unknown:
                raise TableRejection(
                    "unknown_currency",
                    f"column {c} sub-header {sub.text!r}: unrecognized currency "
                    f"{sorted(set(sub_unknown))}",
                    category="data", grid=grid)
            if matches_alias(norm_label(sub_core), cfg.amount_aliases):
                if sub_units and top_units and set(sub_units) != set(top_units):
                    raise TableRejection(
                        "conflicting_unit",
                        f"column {c}: header declares both "
                        f"{sorted(set(top_units))} and {sorted(set(sub_units))}",
                        category="data", grid=grid)
                local = _single_unit(sub_units or top_units,
                                     f"column {c} header", grid)
                amount_cols.append((c, local, sub))
            elif matches_alias(norm_label(sub_core), cfg.ignore_aliases):
                continue
            else:
                raise TableRejection(
                    "unmapped_column",
                    f"column {c} sub-header {sub.text!r} matches no configured "
                    "alias (amount/ignore)", grid=grid)
        if len(amount_cols) != 1:
            raise TableRejection(
                "column_mapping",
                f"year {year} group has {len(amount_cols)} amount sub-columns; "
                "exactly 1 required", grid=grid)
        col, local, sub_cell = amount_cols[0]
        year_columns.append(YearColumn(col=col, year=year, local_unit=local,
                                       top_cell=top, sub_cell=sub_cell))

    year_columns.sort(key=lambda yc: yc.col)
    return ColumnMap(institution_col=inst_cols[0], project_col=proj_cols[0],
                     year_columns=year_columns)


@dataclass
class AmountCandidate:
    year: int
    col: int
    cell_row: int
    cell_col: int
    text: str
    unit: str
    amount_yuan: Decimal | None
    error: str | None

    def to_dict(self) -> dict:
        return {"year": self.year, "column": self.col,
                "cell": [self.cell_row, self.cell_col], "text": self.text,
                "unit": self.unit,
                "amount_yuan": (format_yuan(self.amount_yuan)
                                if self.amount_yuan is not None else None),
                "error": self.error}


@dataclass
class Candidate:
    row_index: int
    institution: str
    project: str
    inst_cell: GridCell
    proj_cell: GridCell
    amounts: list[AmountCandidate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"row_index": self.row_index, "institution": self.institution,
                "project": self.project,
                "institution_cell": self.inst_cell.to_dict(),
                "project_cell": self.proj_cell.to_dict(),
                "amounts": [a.to_dict() for a in self.amounts]}


@dataclass
class Interpretation:
    candidates: list[Candidate]
    totals: dict[int, Decimal]
    year_units: dict[int, str]
    unit_sources: dict[int, str]  # year -> "column" / "table"


def _check_amount_cell(cell: GridCell, row: int, grid: Grid,
                       candidates: list[Candidate]) -> None:
    if cell.row != row or cell.rowspan != 1:
        raise TableRejection(
            "amount_spans_rows",
            f"amount cell r{cell.row}c{cell.col} (rowspan {cell.rowspan}) covers "
            f"data row {row}; a merged amount cannot be attributed to each row",
            grid=grid, candidates=candidates)
    if cell.colspan != 1:
        raise TableRejection(
            "amount_spans_years",
            f"amount cell r{cell.row}c{cell.col} spans {cell.colspan} columns; "
            "one merged amount cannot be copied into several year columns",
            grid=grid, candidates=candidates)


def interpret(grid: Grid, colmap: ColumnMap, caption: str,
              cfg: FundingTablesConfig) -> Interpretation:
    """Extract candidate details, resolve units, reconcile totals.

    Raises :class:`TableRejection` (category ``"data"``) for unknown or
    conflicting units, missing units, unparseable amounts and totals that
    disagree with the details; the candidates gathered so far ride along on
    the exception so the rejection stays auditable.
    """
    units, unknowns = extract_table_units(caption)
    if unknowns:
        raise TableRejection(
            "unknown_currency",
            f"table caption declares unrecognized currency/unit: "
            f"{sorted(set(unknowns))}",
            category="data", grid=grid)
    table_unit = _single_unit(units, "table caption", grid)

    year_units: dict[int, str] = {}
    unit_sources: dict[int, str] = {}
    for yc in colmap.year_columns:
        if yc.local_unit:  # a column-header unit overrides the table unit
            year_units[yc.year] = yc.local_unit
            unit_sources[yc.year] = "column"
        elif table_unit:
            year_units[yc.year] = table_unit
            unit_sources[yc.year] = "table"
        else:
            raise TableRejection(
                "missing_unit",
                f"year column {yc.col} ({yc.year}) has no column-level unit and "
                "the table caption declares none",
                category="data", grid=grid)

    candidates: list[Candidate] = []
    totals: dict[int, Decimal] = {}
    problems: list[dict] = []
    total_rows = 0
    for r in range(grid.header_rows, grid.n_rows):
        first = grid.cell_at(r, 0)
        first_core = norm_label(split_unit_annotation(first.text)[0])
        if first.row == r and matches_alias(first_core, cfg.total_aliases):
            total_rows += 1
            for yc in colmap.year_columns:
                cell = grid.cell_at(r, yc.col)
                _check_amount_cell(cell, r, grid, candidates)
                amount = parse_amount(cell.text)
                if amount is None:
                    problems.append({"where": "total", "row": r, "year": yc.year,
                                     "cell": [cell.row, cell.col],
                                     "text": cell.text})
                else:
                    totals[yc.year] = to_yuan(amount, year_units[yc.year])
            continue
        row_cells = grid.row_cells(r)
        if all(cell.is_th for cell in row_cells):
            raise TableRejection(
                "header_row_in_body",
                f"row {r} consists entirely of <th> but is not a leading "
                "header row", grid=grid, candidates=candidates)
        inst = grid.cell_at(r, colmap.institution_col)
        proj = grid.cell_at(r, colmap.project_col)
        cand = Candidate(row_index=r, institution=inst.text, project=proj.text,
                         inst_cell=inst, proj_cell=proj)
        if not cand.institution or not cand.project:
            raise TableRejection(
                "missing_field",
                f"row {r}: empty institution or project after merged-cell "
                "inheritance", category="data", grid=grid, candidates=candidates)
        for yc in colmap.year_columns:
            cell = grid.cell_at(r, yc.col)
            _check_amount_cell(cell, r, grid, candidates)
            amount = parse_amount(cell.text)
            cand.amounts.append(AmountCandidate(
                year=yc.year, col=yc.col, cell_row=cell.row, cell_col=cell.col,
                text=cell.text, unit=year_units[yc.year],
                amount_yuan=(to_yuan(amount, year_units[yc.year])
                             if amount is not None else None),
                error=None if amount is not None else "unparseable_amount"))
            if amount is None:
                problems.append({"where": "detail", "row": r, "year": yc.year,
                                 "cell": [cell.row, cell.col], "text": cell.text})
        candidates.append(cand)

    if total_rows > 1:
        raise TableRejection(
            "multiple_total_rows", f"{total_rows} totals rows found",
            grid=grid, candidates=candidates)
    if not candidates:
        raise TableRejection("no_details", "no detail rows found", grid=grid)
    if problems:
        raise TableRejection(
            "unparseable_amount", json.dumps(problems, ensure_ascii=False),
            category="data", grid=grid, candidates=candidates)

    mismatches: list[dict] = []
    for yc in colmap.year_columns:
        if yc.year not in totals:
            continue
        detail_sum = sum(
            (a.amount_yuan for cand in candidates for a in cand.amounts
             if a.year == yc.year), Decimal(0))
        total = totals[yc.year]
        if detail_sum != total:
            mismatches.append({
                "year": yc.year,
                "details_sum_yuan": format_yuan(detail_sum),
                "total_yuan": format_yuan(total),
                "difference_yuan": format_yuan(total - detail_sum)})
    if mismatches:
        raise TableRejection(
            "total_mismatch", json.dumps(mismatches, ensure_ascii=False),
            category="data", grid=grid, candidates=candidates)
    return Interpretation(candidates=candidates, totals=totals,
                          year_units=year_units, unit_sources=unit_sources)

"""HTML tables → logical grids, with strict structural validation.

Supported HTML subset (anything outside it is a table-level rejection with a
machine-readable reason code, never a silent guess):

* ``<table>`` with an optional ``<caption>``; ``thead``/``tbody``/``tfoot``
  row groups are accepted but carry no semantics — rows are processed in
  document order.
* ``<tr>`` rows that are direct children of the table or a row group;
  ``<th>``/``<td>`` cells that are direct children of a row.
* ``colspan``/``rowspan`` must be explicit positive integers.  The HTML
  value ``0`` ("span to the end of the table") is NOT part of the subset.
* No nested ``<table>`` inside a cell; a table nested inside another
  table's cell is itself rejected as well.
* After expanding spans the grid must be a complete rectangle: merged
  regions may not overlap, a rowspan may not run past the last row, and no
  position may be left unfilled.
* Header rows are the 1 or 2 leading rows consisting entirely of ``<th>``.

Structural reason codes: ``nested_table``, ``misplaced_row``,
``misplaced_cell``, ``invalid_span``, ``rowspan_out_of_bounds``,
``overlap_merge``, ``grid_hole``, ``empty_table``, ``no_header``,
``unsupported_header_depth``, ``no_details``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .htmlparse import Node

CELL_TAGS = {"td", "th"}
ROW_GROUP_TAGS = {"table", "thead", "tbody", "tfoot"}
TEXT_ONLY = {"script", "style", "textarea", "title"}
MAX_SPAN = 1000

_WS = re.compile(r"[ \t\f\v]+")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"), None)


class TableRejection(Exception):
    """A table-level rejection with a machine-readable reason code.

    ``category`` is ``"structural"`` (grid/mapping problems) or ``"data"``
    (amounts, units, totals).  ``grid``/``candidates`` carry whatever had
    been established before the failure so the rejection stays auditable.
    """

    def __init__(self, code: str, detail: str, *, category: str = "structural",
                 grid: "Grid | None" = None, candidates: list | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.category = category
        self.grid = grid
        self.candidates = candidates or []


def cell_text(node: Node) -> str:
    """Visible cell text: whitespace collapsed, <br> → space, zero-width stripped."""
    pieces: list[str] = []

    def rec(el: Node) -> None:
        for child in el.children:
            if isinstance(child, str):
                pieces.append(child)
            elif child.tag in TEXT_ONLY:
                continue
            elif child.tag == "br":
                pieces.append(" ")
            else:
                rec(child)

    rec(node)
    text = "".join(pieces).translate(_ZERO_WIDTH)
    text = text.replace("\r", " ").replace("\n", " ")
    return _WS.sub(" ", text).strip()


def table_caption(table: Node) -> str:
    for child in table.children:
        if isinstance(child, Node) and child.tag == "caption":
            return cell_text(child)
    return ""


def is_nested_table(table: Node) -> bool:
    parent = table.parent
    while parent is not None:
        if parent.tag == "table":
            return True
        parent = parent.parent
    return False


def contains_nested_table(table: Node) -> bool:
    return any(el.tag == "table" for el in table.iter_elements())


@dataclass(eq=False)
class GridCell:
    """One physical cell and the rectangle it covers in the logical grid."""

    row: int
    col: int
    rowspan: int
    colspan: int
    text: str
    is_th: bool

    def to_dict(self) -> dict:
        return {"row": self.row, "col": self.col, "rowspan": self.rowspan,
                "colspan": self.colspan, "text": self.text,
                "kind": "th" if self.is_th else "td"}


@dataclass
class Grid:
    """The expanded logical grid: every position maps to its covering cell."""

    cells: list[GridCell]
    n_rows: int
    width: int
    header_rows: int
    pos: dict[tuple[int, int], GridCell] = field(default_factory=dict)

    def cell_at(self, row: int, col: int) -> GridCell:
        return self.pos[(row, col)]

    def row_cells(self, row: int) -> list[GridCell]:
        """Distinct cells covering the row, left to right."""
        seen: set[int] = set()
        out: list[GridCell] = []
        for c in range(self.width):
            cell = self.pos[(row, c)]
            if id(cell) not in seen:
                seen.add(id(cell))
                out.append(cell)
        return out

    def to_dict(self) -> dict:
        return {"n_rows": self.n_rows, "width": self.width,
                "header_rows": self.header_rows,
                "cells": [cell.to_dict() for cell in self.cells]}


def _parse_span(value: str | None, attr: str, where: str) -> int:
    if value is None or value == "":
        return 1
    if not re.fullmatch(r"\d+", value.strip()):
        raise TableRejection(
            "invalid_span", f"{where}: {attr}={value!r} is not a positive integer")
    span = int(value.strip())
    if span < 1 or span > MAX_SPAN:
        raise TableRejection(
            "invalid_span", f"{where}: {attr}={value!r} outside 1..{MAX_SPAN}")
    return span


def build_grid(table: Node) -> Grid:
    """Expand a ``<table>`` element into a validated logical grid.

    Raises :class:`TableRejection` for anything outside the supported
    subset; the exception message names the physical cells involved.
    """
    if is_nested_table(table):
        raise TableRejection(
            "nested_table", "table is nested inside another table's cell")
    if contains_nested_table(table):
        raise TableRejection(
            "nested_table", "table contains a nested <table> inside a cell")

    rows: list[Node] = []
    for el in table.iter_elements():
        if el.tag == "tr":
            if el.parent is None or el.parent.tag not in ROW_GROUP_TAGS:
                raise TableRejection(
                    "misplaced_row",
                    "<tr> is not directly inside table/thead/tbody/tfoot")
            rows.append(el)
        elif el.tag in CELL_TAGS:
            if el.parent is None or el.parent.tag != "tr":
                raise TableRejection(
                    "misplaced_cell", f"<{el.tag}> is not a direct child of <tr>")
    if not rows:
        raise TableRejection("empty_table", "table has no <tr> rows")

    pos: dict[tuple[int, int], GridCell] = {}
    cells: list[GridCell] = []
    n_rows = len(rows)
    for r, tr in enumerate(rows):
        c = 0
        for node in tr.children:
            if not (isinstance(node, Node) and node.tag in CELL_TAGS):
                continue
            while (r, c) in pos:
                c += 1
            where = f"cell r{r}c{c}"
            rowspan = _parse_span(node.attrs.get("rowspan"), "rowspan", where)
            colspan = _parse_span(node.attrs.get("colspan"), "colspan", where)
            if r + rowspan > n_rows:
                raise TableRejection(
                    "rowspan_out_of_bounds",
                    f"cell r{r}c{c} has rowspan {rowspan} but the table has "
                    f"{n_rows} rows")
            cell = GridCell(r, c, rowspan, colspan, cell_text(node),
                            node.tag == "th")
            for dr in range(rowspan):
                for dc in range(colspan):
                    target = (r + dr, c + dc)
                    if target in pos:
                        other = pos[target]
                        raise TableRejection(
                            "overlap_merge",
                            f"cell r{r}c{c} (rowspan {rowspan}, colspan {colspan}) "
                            f"overlaps cell r{other.row}c{other.col} "
                            f"(rowspan {other.rowspan}, colspan {other.colspan}) "
                            f"at r{target[0]}c{target[1]}")
                    pos[target] = cell
            cells.append(cell)
            c += colspan

    width = max((c for (_r, c) in pos), default=-1) + 1
    for r in range(n_rows):
        for c in range(width):
            if (r, c) not in pos:
                raise TableRejection(
                    "grid_hole",
                    f"no cell covers r{r}c{c}; the expanded grid is not rectangular")

    header_rows = 0
    for r in range(n_rows):
        row_cells = {id(pos[(r, c)]): pos[(r, c)] for c in range(width)}
        if all(cell.is_th for cell in row_cells.values()):
            header_rows += 1
        else:
            break
    if header_rows == 0:
        raise TableRejection(
            "no_header",
            "no leading all-<th> header row; column roles cannot be determined")
    if header_rows > 2:
        raise TableRejection(
            "unsupported_header_depth",
            f"{header_rows} header rows; only 1 or 2 are supported")
    grid = Grid(cells=cells, n_rows=n_rows, width=width,
                header_rows=header_rows, pos=pos)
    if header_rows == n_rows:
        raise TableRejection(
            "no_details", "table has header rows but no data rows", grid=grid)
    return grid

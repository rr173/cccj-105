"""Funding-table structuring job: archived raw HTML → SQLite fact records.

This job is an independent entry point from web scanning.  It never contacts
a site and never calls a language model; it consumes only

* the *identity-schema* pipeline database (``work/pipeline.sqlite3``, opened
  read-only — a pre-identity legacy database is refused, never migrated), and
* the local content-addressed raw archive (``work/archive/``).

For every archived version it loads the raw HTML bytes by ``raw_digest``,
verifies the SHA-256 (a missing blob or a digest mismatch is an explicit
per-version failure — the normalized body is never substituted), expands the
selected funding tables into logical grids (:mod:`fundgrid`), maps columns
by configured aliases (:mod:`fundtable`), converts amounts to exact
:class:`~decimal.Decimal` yuan (:mod:`fundamount`), reconciles per-year
totals, and books fully-valid tables into ``work/funding.sqlite3``.

Guarantees
----------
* facts are keyed by (source, archive identity, version, table, logical
  data row, year): same-number public/full editions and every historical
  version coexist without overwriting each other;
* each table is processed inside one transaction — a crash mid-table leaves
  no partial facts, and reprocessing is always safe;
* reprocessing the same input books no duplicates (booked tables with an
  unchanged config digest and raw digest are skipped; anything else is
  deleted and re-booked atomically);
* rejected tables keep their expanded grid, candidate details and reason in
  ``table_outcomes`` (current state) and ``outcome_history`` (every
  diagnostic ever produced, so a config fix can be audited);
* only fully-validated tables ever enter the fact query.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .archive import RawArchive
from .fundamount import format_yuan
from .fundconfig import FundingTablesConfig, load_funding_tables_config
from .fundgrid import TableRejection, build_grid, table_caption
from .fundtable import Candidate, classify_columns, interpret
from .htmlparse import parse_html

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TABLES_CONFIG = ROOT / "config" / "funding_tables.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FundingError(RuntimeError):
    """Operational failure: missing/legacy pipeline state, bad config…"""


class FundingInterrupted(RuntimeError):
    """Controlled mid-table interruption (tests/demo).

    Raised inside the per-table transaction, which is therefore rolled back:
    no partial facts for the interrupted table survive.
    """


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    config_digest  TEXT NOT NULL,
    config_json    TEXT NOT NULL DEFAULT '{}',
    summary_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS table_outcomes (
    source_id      TEXT NOT NULL,
    archive_id     TEXT NOT NULL,
    version_no     INTEGER NOT NULL,
    table_index    INTEGER NOT NULL,
    notice_key     TEXT,
    raw_digest     TEXT NOT NULL,
    status         TEXT NOT NULL,           -- booked / rejected
    category       TEXT,                    -- structural / data
    reason_code    TEXT,
    reason_detail  TEXT,
    grid_json      TEXT,
    candidates_json TEXT,
    totals_json    TEXT,
    config_digest  TEXT NOT NULL,
    run_id         INTEGER NOT NULL,
    processed_at   TEXT NOT NULL,
    PRIMARY KEY (source_id, archive_id, version_no, table_index)
);

CREATE TABLE IF NOT EXISTS outcome_history (
    hist_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT NOT NULL,
    archive_id     TEXT NOT NULL,
    version_no     INTEGER NOT NULL,
    table_index    INTEGER NOT NULL,        -- -1 marks a version-level error
    raw_digest     TEXT,
    status         TEXT NOT NULL,
    category       TEXT,
    reason_code    TEXT,
    reason_detail  TEXT,
    grid_json      TEXT,
    candidates_json TEXT,
    totals_json    TEXT,
    config_digest  TEXT NOT NULL,
    run_id         INTEGER NOT NULL,
    processed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_errors (
    source_id      TEXT NOT NULL,
    archive_id     TEXT NOT NULL,
    version_no     INTEGER NOT NULL,
    notice_key     TEXT,
    raw_digest     TEXT,
    error_code     TEXT NOT NULL,           -- missing_raw / digest_mismatch
    error_detail   TEXT,
    config_digest  TEXT NOT NULL,
    run_id         INTEGER NOT NULL,
    processed_at   TEXT NOT NULL,
    PRIMARY KEY (source_id, archive_id, version_no)
);

CREATE TABLE IF NOT EXISTS facts (
    source_id      TEXT NOT NULL,
    archive_id     TEXT NOT NULL,
    version_no     INTEGER NOT NULL,
    table_index    INTEGER NOT NULL,
    row_index      INTEGER NOT NULL,        -- expanded-grid row of the detail
    year           INTEGER NOT NULL,
    notice_key     TEXT,
    institution    TEXT NOT NULL,
    project        TEXT NOT NULL,
    amount_yuan    TEXT NOT NULL,           -- exact Decimal yuan, plain string
    amount_text    TEXT NOT NULL,           -- original cell text
    unit           TEXT NOT NULL,           -- yuan / thousand / wan
    unit_source    TEXT NOT NULL,           -- column / table
    amount_cell_row INTEGER NOT NULL,
    amount_cell_col INTEGER NOT NULL,
    institution_cell_row INTEGER NOT NULL,
    institution_cell_col INTEGER NOT NULL,
    project_cell_row INTEGER NOT NULL,
    project_cell_col INTEGER NOT NULL,
    header_path_json TEXT NOT NULL,         -- merged column headers inherited
    row_header_path_json TEXT NOT NULL,     -- merged row headers inherited
    raw_digest     TEXT NOT NULL,
    config_digest  TEXT NOT NULL,
    run_id         INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (source_id, archive_id, version_no, table_index, row_index, year)
);
"""

_FACT_COLUMNS = (
    "source_id", "archive_id", "version_no", "table_index", "row_index", "year",
    "notice_key", "institution", "project", "amount_yuan", "amount_text",
    "unit", "unit_source", "amount_cell_row", "amount_cell_col",
    "institution_cell_row", "institution_cell_col",
    "project_cell_row", "project_cell_col",
    "header_path_json", "row_header_path_json",
    "raw_digest", "config_digest", "run_id", "created_at",
)


class FundingStore:
    """The job's own database; the pipeline database is never written."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------
    def mark_interrupted_runs(self, at: str) -> int:
        """Runs still 'running' at startup belong to a dead process."""
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE runs SET status='interrupted', finished_at=? "
                "WHERE status='running'", (at,))
            return cur.rowcount

    def create_run(self, started_at: str, config_digest: str, config_json: str) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO runs(started_at, status, config_digest, config_json) "
                "VALUES (?, 'running', ?, ?)",
                (started_at, config_digest, config_json))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished_at: str, status: str,
                   summary: dict) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, summary_json=? "
                "WHERE run_id=?",
                (finished_at, status,
                 json.dumps(summary, ensure_ascii=False, default=str), run_id))

    def runs(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM runs ORDER BY run_id")]

    # ------------------------------------------------------------------
    def outcome(self, source_id: str, archive_id: str, version_no: int,
                table_index: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM table_outcomes WHERE source_id=? AND archive_id=? "
            "AND version_no=? AND table_index=?",
            (source_id, archive_id, version_no, table_index)).fetchone()
        return dict(row) if row else None

    def outcomes(self, source_id: str | None = None, archive_id: str | None = None,
                 status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM table_outcomes"
        clauses, params = [], []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if archive_id:
            clauses.append("archive_id=?")
            params.append(archive_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_id, archive_id, version_no, table_index"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def history(self, source_id: str | None = None,
                archive_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM outcome_history"
        clauses, params = [], []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if archive_id:
            clauses.append("archive_id=?")
            params.append(archive_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY hist_id"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def version_errors(self, source_id: str | None = None,
                       archive_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM version_errors"
        clauses, params = [], []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if archive_id:
            clauses.append("archive_id=?")
            params.append(archive_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_id, archive_id, version_no"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def facts(self, source_id: str | None = None, archive_id: str | None = None,
              version_no: int | None = None, table_index: int | None = None,
              row_index: int | None = None, year: int | None = None) -> list[dict]:
        sql = "SELECT * FROM facts"
        clauses, params = [], []
        for column, value in (("source_id", source_id), ("archive_id", archive_id),
                              ("version_no", version_no), ("table_index", table_index),
                              ("row_index", row_index), ("year", year)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (" ORDER BY source_id, archive_id, version_no, table_index, "
                "row_index, year")
        return [dict(row) for row in self.conn.execute(sql, params)]

    def fact_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])


class PipelineReader:
    """Read-only view of the identity-schema pipeline database."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        if not self.path.exists():
            raise FundingError(
                f"pipeline database not found: {self.path} "
                "(run a scan first, or build the sample workdir)")
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        user_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version < 2:
            raise FundingError(
                "pipeline database uses the pre-identity schema; the funding job "
                "consumes identity-schema data only and never migrates the old "
                "database. Run 'python -m archive_pipeline.cli repair --preview' "
                "then 'repair --apply' first.")
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='versions'"
        ).fetchone()
        if not exists:
            raise FundingError(
                f"pipeline database {self.path} has no versions table")

    def versions(self, source_id: str | None = None,
                 archive_id: str | None = None) -> list[dict]:
        sql = ("SELECT source_id, archive_id, notice_key, version_no, url_key, "
               "raw_digest, title, issued_on FROM versions")
        clauses, params = [], []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if archive_id:
            clauses.append("archive_id=?")
            params.append(archive_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_id, archive_id, version_no"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def close(self) -> None:
        self.conn.close()


class FundingJob:
    """Processes archived versions into funding facts."""

    def __init__(self, workdir: str | Path,
                 tables_config_path: str | Path | None = None,
                 progress=None):
        self.workdir = Path(workdir)
        self.config: FundingTablesConfig = load_funding_tables_config(
            tables_config_path or DEFAULT_TABLES_CONFIG)
        self.pipeline = PipelineReader(self.workdir / "pipeline.sqlite3")
        self.archive = RawArchive(self.workdir / "archive")
        self.store = FundingStore(self.workdir / "funding.sqlite3")
        self.progress = progress or (lambda _msg: None)
        self._fail_point: tuple[str, int] | None = None

    def close(self) -> None:
        self.pipeline.close()
        self.store.close()

    # ------------------------------------------------------------------
    def process(self, source_id: str | None = None, archive_id: str | None = None,
                fail_point: tuple[str, int] | None = None) -> dict:
        """Process (or reprocess) archived versions.

        ``fail_point`` is a test/demo hook: ``(archive_id, table_index)``
        raises :class:`FundingInterrupted` inside that table's transaction.
        """
        self._fail_point = fail_point
        started = now_iso()
        stale = self.store.mark_interrupted_runs(started)
        if stale:
            self.progress(f"marked {stale} stale running run(s) as interrupted")
        run_id = self.store.create_run(started, self.config.digest,
                                       self.config.canonical_json)
        summary = {"run_id": run_id, "config_digest": self.config.digest,
                   "versions_seen": 0, "tables_selected": 0, "booked": 0,
                   "rejected_structural": 0, "rejected_data": 0,
                   "skipped_booked": 0, "version_errors": 0, "facts_written": 0}
        try:
            versions = self.pipeline.versions(source_id, archive_id)
            summary["versions_seen"] = len(versions)
            for version in versions:
                self._process_version(version, run_id, summary)
        except FundingInterrupted:
            self.store.finish_run(run_id, now_iso(), "interrupted", summary)
            raise
        except Exception:
            self.store.finish_run(run_id, now_iso(), "failed", summary)
            raise
        self.store.finish_run(run_id, now_iso(), "completed", summary)
        return summary

    # ------------------------------------------------------------------
    def _process_version(self, version: dict, run_id: int, summary: dict) -> None:
        key3 = (version["source_id"], version["archive_id"], version["version_no"])
        with self.store.tx() as conn:
            conn.execute(
                "DELETE FROM version_errors WHERE source_id=? AND archive_id=? "
                "AND version_no=?", key3)
        try:
            raw = self.archive.load(version["raw_digest"])
        except FileNotFoundError:
            self._record_version_error(
                version, run_id, "missing_raw",
                f"no archived blob for raw_digest {version['raw_digest']}")
            summary["version_errors"] += 1
            return
        actual = hashlib.sha256(raw).hexdigest()
        if actual != version["raw_digest"]:
            self._record_version_error(
                version, run_id, "digest_mismatch",
                f"archived blob hashes to {actual}, expected "
                f"{version['raw_digest']}")
            summary["version_errors"] += 1
            return
        text = raw.decode("utf-8", errors="replace")
        root, _notes = parse_html(text)
        tables = [el for el in root.iter_elements() if el.tag == "table"]
        for table_index, node in enumerate(tables):
            caption = table_caption(node)
            classes = (node.attrs.get("class") or "").split()
            if not self.config.selects(node.attrs.get("id", ""), classes, caption):
                continue
            summary["tables_selected"] += 1
            existing = self.store.outcome(*key3, table_index)
            if (existing and existing["status"] == "booked"
                    and existing["config_digest"] == self.config.digest
                    and existing["raw_digest"] == version["raw_digest"]):
                summary["skipped_booked"] += 1
                continue
            self._process_table(version, table_index, node, caption,
                                run_id, summary)

    def _record_version_error(self, version: dict, run_id: int, code: str,
                              detail: str) -> None:
        at = now_iso()
        key3 = (version["source_id"], version["archive_id"], version["version_no"])
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO version_errors(source_id, archive_id, version_no, "
                "notice_key, raw_digest, error_code, error_detail, config_digest, "
                "run_id, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id, archive_id, version_no) DO UPDATE SET "
                "notice_key=excluded.notice_key, raw_digest=excluded.raw_digest, "
                "error_code=excluded.error_code, error_detail=excluded.error_detail, "
                "config_digest=excluded.config_digest, run_id=excluded.run_id, "
                "processed_at=excluded.processed_at",
                (*key3, version["notice_key"], version["raw_digest"], code, detail,
                 self.config.digest, run_id, at))
            conn.execute(
                "INSERT INTO outcome_history(source_id, archive_id, version_no, "
                "table_index, raw_digest, status, category, reason_code, "
                "reason_detail, grid_json, candidates_json, totals_json, "
                "config_digest, run_id, processed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*key3, -1, version["raw_digest"], "rejected", "source", code,
                 detail, None, None, None, self.config.digest, run_id, at))
        self.progress(f"  {version['notice_key']} v{version['version_no']}: {code}")

    # ------------------------------------------------------------------
    def _process_table(self, version: dict, table_index: int, node, caption: str,
                       run_id: int, summary: dict) -> None:
        grid = None
        candidates: list[Candidate] = []
        facts: list[dict] = []
        totals_info: dict[str, str] = {}
        try:
            grid = build_grid(node)
            colmap = classify_columns(grid, self.config)
            result = interpret(grid, colmap, caption, self.config)
            candidates = result.candidates
            facts = self._build_facts(version, table_index, grid, result, run_id)
            totals_info = {str(year): format_yuan(total)
                           for year, total in result.totals.items()}
            status, category, reason_code, reason_detail = "booked", None, None, None
        except TableRejection as rej:
            grid = rej.grid or grid
            candidates = rej.candidates or candidates
            status, category = "rejected", rej.category
            reason_code, reason_detail = rej.code, rej.detail

        at = now_iso()
        key4 = (version["source_id"], version["archive_id"],
                version["version_no"], table_index)
        grid_json = json.dumps(grid.to_dict(), ensure_ascii=False) if grid else None
        candidates_json = json.dumps([c.to_dict() for c in candidates],
                                     ensure_ascii=False)
        totals_json = json.dumps(totals_info, ensure_ascii=False)
        with self.store.tx() as conn:
            conn.execute(
                "DELETE FROM facts WHERE source_id=? AND archive_id=? AND "
                "version_no=? AND table_index=?", key4)
            if status == "booked":
                for fact in facts:
                    conn.execute(
                        f"INSERT INTO facts({', '.join(_FACT_COLUMNS)}) "
                        f"VALUES ({', '.join('?' for _ in _FACT_COLUMNS)})",
                        tuple(fact[column] for column in _FACT_COLUMNS))
            conn.execute(
                "INSERT INTO table_outcomes(source_id, archive_id, version_no, "
                "table_index, notice_key, raw_digest, status, category, "
                "reason_code, reason_detail, grid_json, candidates_json, "
                "totals_json, config_digest, run_id, processed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id, archive_id, version_no, table_index) "
                "DO UPDATE SET notice_key=excluded.notice_key, "
                "raw_digest=excluded.raw_digest, status=excluded.status, "
                "category=excluded.category, reason_code=excluded.reason_code, "
                "reason_detail=excluded.reason_detail, grid_json=excluded.grid_json, "
                "candidates_json=excluded.candidates_json, "
                "totals_json=excluded.totals_json, "
                "config_digest=excluded.config_digest, run_id=excluded.run_id, "
                "processed_at=excluded.processed_at",
                (*key4, version["notice_key"], version["raw_digest"], status,
                 category, reason_code, reason_detail, grid_json, candidates_json,
                 totals_json, self.config.digest, run_id, at))
            conn.execute(
                "INSERT INTO outcome_history(source_id, archive_id, version_no, "
                "table_index, raw_digest, status, category, reason_code, "
                "reason_detail, grid_json, candidates_json, totals_json, "
                "config_digest, run_id, processed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*key4, version["raw_digest"], status, category, reason_code,
                 reason_detail, grid_json, candidates_json, totals_json,
                 self.config.digest, run_id, at))
            if self._fail_point and self._fail_point == (version["archive_id"],
                                                         table_index):
                raise FundingInterrupted(
                    f"interrupted inside table transaction {key4}")

        if status == "booked":
            summary["booked"] += 1
            summary["facts_written"] += len(facts)
            self.progress(
                f"  {version['notice_key']} v{version['version_no']} "
                f"table {table_index}: booked {len(facts)} fact(s)")
        else:
            summary["rejected_data" if category == "data"
                    else "rejected_structural"] += 1
            self.progress(
                f"  {version['notice_key']} v{version['version_no']} "
                f"table {table_index}: rejected ({reason_code})")

    # ------------------------------------------------------------------
    def _build_facts(self, version: dict, table_index: int, grid,
                     result, run_id: int) -> list[dict]:
        at = now_iso()
        facts: list[dict] = []
        for cand in result.candidates:
            row_header_path = [
                {"role": "institution", **cand.inst_cell.to_dict()},
                {"role": "project", **cand.proj_cell.to_dict()},
            ]
            for amount in cand.amounts:
                header_path: list[dict] = []
                last_cell = None
                for hr in range(grid.header_rows):
                    cell = grid.cell_at(hr, amount.col)
                    if cell is not last_cell:
                        header_path.append(cell.to_dict())
                        last_cell = cell
                facts.append({
                    "source_id": version["source_id"],
                    "archive_id": version["archive_id"],
                    "version_no": version["version_no"],
                    "table_index": table_index,
                    "row_index": cand.row_index,
                    "year": amount.year,
                    "notice_key": version["notice_key"],
                    "institution": cand.institution,
                    "project": cand.project,
                    "amount_yuan": format_yuan(amount.amount_yuan),
                    "amount_text": amount.text,
                    "unit": amount.unit,
                    "unit_source": result.unit_sources[amount.year],
                    "amount_cell_row": amount.cell_row,
                    "amount_cell_col": amount.cell_col,
                    "institution_cell_row": cand.inst_cell.row,
                    "institution_cell_col": cand.inst_cell.col,
                    "project_cell_row": cand.proj_cell.row,
                    "project_cell_col": cand.proj_cell.col,
                    "header_path_json": json.dumps(header_path, ensure_ascii=False),
                    "row_header_path_json": json.dumps(row_header_path,
                                                       ensure_ascii=False),
                    "raw_digest": version["raw_digest"],
                    "config_digest": self.config.digest,
                    "run_id": run_id,
                    "created_at": at,
                })
        return facts

"""XLIFF 1.2 review intake: bind approved translations to archived notice text.

This job sits between a translation team and the archive corpus.  A review
file is delivered together with a JSON binding map; for every ``file`` element
the map names the exact ``(source_id, archive_id, version_no)`` archive
version, and for every ``trans-unit`` inside it the map gives a half-open
character span ``[start, end)`` on that version's archived ``body``.

The job then, for each unit:

1. expands ``source`` (text + paired ``g`` + empty ``x``, ``x`` replaced by its
   ``equiv-text``) and requires it to equal ``body[start:end]`` exactly — no
   whitespace folding, no byte offsets, no fallback to a sibling variant or a
   newer version of the same notice;
2. expands ``target`` and requires the same code id set, element types and
   direct code parents as the source (sibling order may differ), and the same
   ``equiv-text`` for every ``x``;
3. treats the unit as an *approved candidate* only when
   ``approved="yes"`` and the target ``state="final"``; anything else that is
   otherwise well-formed is kept as a **draft** and can never enter the
   usable corpus;
4. keeps every rejected unit with its concrete reason, the source/target
   texts, the code trees and its XLIFF position for human review.

Candidate grouping
------------------
A group is ``(source_id, archive_id, version_no, start, end, source_language,
target_language)``.  Two approved translations are *the same translation* when
their text/code interleaving event sequences are equal (text runs, ``g`` open/
close, ``x`` id and ``equiv-text``; XML attribute order and namespace prefixes
are irrelevant).  Therefore a re-delivery with different packaging but the
same translation attaches another **source** to one candidate instead of
creating a second corpus entry.  A genuinely different approved translation in
the same group turns the group into ``conflict``: every candidate and every
delivery source remains queryable, and no candidate is offered as the single
usable answer.  Drafts never take part in conflict detection.

Transaction and idempotency
---------------------------
One delivery is written in a single SQLite transaction: delivery, files,
units, diagnostics, candidates, candidate sources and the recomputed
availability of every affected group commit together.  A controlled
interruption (or process death) before commit leaves the corpus exactly as it
was — never "candidate added but the old translation still shown as unique".
Re-delivering the same review file and mapping is detected by their SHA-256
digests and adds no rows at all.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .xliffparse import (
    XLIF_NAMESPACE,
    XML_NAMESPACE,
    Element,
    Expanded,
    InlineError,
    XliffSyntaxError,
    build_branches,
    compare_structure,
    events_for_comparison,
    expand,
    normalize_language,
    parse_xliff,
)

ROOT = Path(__file__).resolve().parent.parent
BINDING_FORMAT = "xliff-binding/1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class XliffError(RuntimeError):
    """Operational failure (missing file, unavailable pipeline database…)."""


class IntakeRejected(XliffError):
    """The document or mapping is refused before any corpus write."""

    def __init__(self, code: str, detail: str, exit_code: int = 3,
                 location: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.exit_code = exit_code
        self.location = location or {}


class MappingError(IntakeRejected):
    def __init__(self, detail: str, location: str = "mapping"):
        super().__init__("mapping_invalid", detail, exit_code=2,
                         location={"where": location})


class IntakeInterrupted(RuntimeError):
    """Controlled interruption inside the delivery transaction (rolled back)."""


# ---------------------------------------------------------------------------
# Binding map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitSpan:
    start: int
    end: int


@dataclass(frozen=True)
class FileBinding:
    original: str
    source_id: str
    archive_id: str
    version_no: int
    units: dict[str, UnitSpan]
    source_language: str | None = None
    target_language: str | None = None


@dataclass(frozen=True)
class Mapping:
    path: Path
    file_sha256: str
    description: str
    files: dict[str, FileBinding]
    canonical_json: str
    digest: str

    def entry(self, original: str) -> FileBinding | None:
        return self.files.get(original)


def _reject_duplicate_keys(pairs):
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def _nonempty_str(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MappingError(f"{where} must be a non-empty string", where)
    return value


def _is_int(value) -> bool:
    return type(value) is int and not isinstance(value, bool)


def load_mapping(path: str | Path) -> Mapping:
    """Read and fully validate the binding map; never touches the database."""
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise MappingError(f"cannot read mapping file {path}: {exc}") from exc
    try:
        raw = json.loads(raw_bytes.decode("utf-8"),
                         object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MappingError(f"mapping file is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise MappingError("mapping must be a JSON object")
    allowed = {"format", "description", "files", "units"}
    unknown = set(raw) - allowed
    if unknown:
        raise MappingError(f"unknown top-level keys {sorted(unknown)}")
    top_units = raw.get("units")
    if top_units is not None and not isinstance(top_units, dict):
        raise MappingError("top-level units must be an object")
    if raw.get("format") != BINDING_FORMAT:
        raise MappingError(
            f"format must be {BINDING_FORMAT!r}, got {raw.get('format')!r}")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise MappingError("description must be a string")
    files_raw = raw.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise MappingError("files must be a non-empty list")

    files: dict[str, FileBinding] = {}
    for index, item in enumerate(files_raw):
        where = f"files[{index}]"
        if not isinstance(item, dict):
            raise MappingError(f"{where} must be an object", where)
        unknown = set(item) - {"original", "source_id", "archive_id",
                               "version_no", "units", "source_language",
                               "target_language", "note"}
        if unknown:
            raise MappingError(f"{where}: unknown keys {sorted(unknown)}", where)
        original = _nonempty_str(item.get("original"), f"{where}.original")
        source_id = _nonempty_str(item.get("source_id"), f"{where}.source_id")
        archive_id = _nonempty_str(item.get("archive_id"), f"{where}.archive_id")
        version_no = item.get("version_no")
        if not _is_int(version_no) or version_no < 1:
            raise MappingError(
                f"{where}.version_no must be a positive JSON integer", where)
        units_raw = item.get("units")
        if units_raw is None:
            units_raw = {}
        elif not isinstance(units_raw, dict):
            raise MappingError(f"{where}.units must be an object", where)
        units: dict[str, UnitSpan] = {}
        for unit_id, span in units_raw.items():
            if not isinstance(unit_id, str) or not unit_id:
                raise MappingError(f"{where}.units has an empty unit id", where)
            if not isinstance(span, dict):
                raise MappingError(
                    f"{where}.units[{unit_id!r}] must be an object")
            unknown = set(span) - {"start", "end", "note"}
            if unknown:
                raise MappingError(
                    f"{where}.units[{unit_id!r}]: unknown keys {sorted(unknown)}")
            start, end = span.get("start"), span.get("end")
            if not _is_int(start) or not _is_int(end):
                raise MappingError(
                    f"{where}.units[{unit_id!r}]: start/end must be JSON integers")
            if start < 0 or end <= start:
                raise MappingError(
                    f"{where}.units[{unit_id!r}]: need 0 <= start < end "
                    f"(got [{start},{end}))")
            units[unit_id] = UnitSpan(start, end)
        if original in files:
            raise MappingError(
                f"{where}.original {original!r} is bound twice; file originals "
                "must be unique so a binding is unambiguous", where)
        sl = item.get("source_language")
        tl = item.get("target_language")
        if sl is not None and normalize_language(sl) is None:
            raise MappingError(f"{where}.source_language is not a valid tag", where)
        if tl is not None and normalize_language(tl) is None:
            raise MappingError(f"{where}.target_language is not a valid tag", where)
        if sl is not None and tl is not None and \
                normalize_language(sl) == normalize_language(tl):
            raise MappingError(
                f"{where}: source_language and target_language are the same tag",
                where)
        files[original] = FileBinding(
            original=original, source_id=source_id, archive_id=archive_id,
            version_no=version_no, units=units,
            source_language=normalize_language(sl),
            target_language=normalize_language(tl))

    # Top-level ``units`` is the shared span table: every file element uses it
    # unless its own ``units`` object overrides an id (file-level wins, so a
    # document can split the same original into several bound parts).
    if top_units:
        shared: dict[str, UnitSpan] = {}
        for unit_id, span in top_units.items():
            if not isinstance(unit_id, str) or not unit_id:
                raise MappingError("top-level units has an empty unit id")
            if not isinstance(span, dict):
                raise MappingError(
                    f"top-level units[{unit_id!r}] must be an object")
            unknown = set(span) - {"start", "end", "note"}
            if unknown:
                raise MappingError(
                    f"top-level units[{unit_id!r}]: unknown keys "
                    f"{sorted(unknown)}")
            start, end = span.get("start"), span.get("end")
            if not _is_int(start) or not _is_int(end):
                raise MappingError(
                    f"top-level units[{unit_id!r}]: start/end must be JSON "
                    "integers")
            if start < 0 or end <= start:
                raise MappingError(
                    f"top-level units[{unit_id!r}]: need 0 <= start < end "
                    f"(got [{start},{end}))")
            shared[unit_id] = UnitSpan(start, end)
        files = {
            original: FileBinding(
                original=binding.original, source_id=binding.source_id,
                archive_id=binding.archive_id, version_no=binding.version_no,
                units={**shared, **binding.units},
                source_language=binding.source_language,
                target_language=binding.target_language)
            for original, binding in files.items()}

    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return Mapping(
        path=path,
        file_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        description=description,
        files=files,
        canonical_json=canonical,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Read-only pipeline access
# ---------------------------------------------------------------------------


class PipelineReader:
    """Read-only view of the identity-schema pipeline database."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        if not self.path.exists():
            raise XliffError(
                f"pipeline database not found: {self.path} "
                "(run a scan first, or build the sample workdir)")
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        user_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version < 2:
            raise XliffError(
                "pipeline database uses the pre-identity schema; the XLIFF job "
                "consumes identity-schema data only and never migrates the old "
                "database. Run 'python -m archive_pipeline.cli repair --preview' "
                "then 'repair --apply' first.")
        if not self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='versions'"
        ).fetchone():
            raise XliffError(f"pipeline database {self.path} has no versions table")

    def version(self, source_id: str, archive_id: str,
                version_no: int) -> dict | None:
        row = self.conn.execute(
            "SELECT source_id, archive_id, notice_key, version_no, title, "
            "issued_on, body, raw_digest FROM versions "
            "WHERE source_id=? AND archive_id=? AND version_no=?",
            (source_id, archive_id, version_no)).fetchone()
        return dict(row) if row else None

    def source_versions(self, source_id: str, archive_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT version_no, notice_key, body FROM versions "
            "WHERE source_id=? AND archive_id=? ORDER BY version_no",
            (source_id, archive_id))]

    def source_identities(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT archive_id, notice_key FROM document_identities "
            "WHERE source_id=? ORDER BY archive_id", (source_id,))]

    def sibling_versions(self, source_id: str, archive_id: str,
                         notice_key: str | None) -> list[dict]:
        if not notice_key:
            return []
        return [dict(row) for row in self.conn.execute(
            "SELECT archive_id, version_no, body FROM versions "
            "WHERE source_id=? AND notice_key=? AND archive_id<>? "
            "ORDER BY archive_id, version_no",
            (source_id, notice_key, archive_id))]

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'running',
    review_path      TEXT NOT NULL,
    review_sha256    TEXT NOT NULL,
    review_bytes     INTEGER NOT NULL,
    mapping_path     TEXT NOT NULL,
    mapping_sha256   TEXT NOT NULL,
    mapping_digest   TEXT NOT NULL,
    delivery_id      INTEGER,
    units_total      INTEGER NOT NULL DEFAULT 0,
    summary_json     TEXT,
    error_json       TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL,
    review_path      TEXT NOT NULL,
    review_sha256    TEXT NOT NULL,
    review_bytes     INTEGER NOT NULL,
    mapping_path     TEXT NOT NULL,
    mapping_sha256   TEXT NOT NULL,
    mapping_digest   TEXT NOT NULL,
    mapping_description TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (review_sha256, mapping_sha256)
);

CREATE TABLE IF NOT EXISTS files (
    file_pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id      INTEGER NOT NULL,
    file_index       INTEGER NOT NULL,
    original         TEXT,
    source_language  TEXT,
    target_language  TEXT,
    bound_source_id  TEXT,
    bound_archive_id TEXT,
    bound_version_no INTEGER,
    status           TEXT NOT NULL,   -- accepted / rejected
    reason_code      TEXT,
    reason_detail    TEXT,
    line             INTEGER,
    column           INTEGER,
    unit_count       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (delivery_id, file_index)
);

CREATE TABLE IF NOT EXISTS units (
    unit_pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id      INTEGER NOT NULL,
    file_pk          INTEGER NOT NULL,
    unit_index       INTEGER NOT NULL,
    unit_id          TEXT,
    group_path_json  TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL,   -- candidate / draft / rejected
    reason_code      TEXT,
    reason_detail    TEXT,
    approved         TEXT,
    target_state     TEXT,
    source_language  TEXT,
    target_language  TEXT,
    declared_source_lang TEXT,
    declared_target_lang TEXT,
    span_start       INTEGER,
    span_end         INTEGER,
    source_text      TEXT,
    target_text      TEXT,
    source_codes_json TEXT,
    target_codes_json TEXT,
    source_xml       TEXT,
    target_xml       TEXT,
    event_digest     TEXT,
    candidate_id     INTEGER,
    line             INTEGER,
    column           INTEGER,
    source_line      INTEGER,
    target_line      INTEGER,
    UNIQUE (delivery_id, file_pk, unit_index)
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        TEXT NOT NULL,
    archive_id       TEXT NOT NULL,
    version_no       INTEGER NOT NULL,
    span_start       INTEGER NOT NULL,
    span_end         INTEGER NOT NULL,
    source_language  TEXT NOT NULL,
    target_language  TEXT NOT NULL,
    source_text      TEXT NOT NULL,
    target_text      TEXT NOT NULL,
    target_codes_json TEXT NOT NULL,
    event_digest     TEXT NOT NULL,
    event_json       TEXT NOT NULL,
    first_delivery_id INTEGER NOT NULL,
    first_unit_pk    INTEGER,
    created_at       TEXT NOT NULL,
    UNIQUE (source_id, archive_id, version_no, span_start, span_end,
            source_language, target_language, event_digest)
);

CREATE TABLE IF NOT EXISTS candidate_sources (
    candidate_source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id     INTEGER NOT NULL,
    delivery_id      INTEGER NOT NULL,
    unit_pk          INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (delivery_id, unit_pk)
);

CREATE TABLE IF NOT EXISTS group_state (
    source_id        TEXT NOT NULL,
    archive_id       TEXT NOT NULL,
    version_no       INTEGER NOT NULL,
    span_start       INTEGER NOT NULL,
    span_end         INTEGER NOT NULL,
    source_language  TEXT NOT NULL,
    target_language  TEXT NOT NULL,
    state            TEXT NOT NULL,   -- available / conflict
    available_candidate_id INTEGER,
    candidate_count  INTEGER NOT NULL,
    reason           TEXT,
    recomputed_at    TEXT NOT NULL,
    PRIMARY KEY (source_id, archive_id, version_no, span_start, span_end,
                 source_language, target_language)
);

CREATE TABLE IF NOT EXISTS diagnostics (
    diagnostic_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL,
    delivery_id      INTEGER,
    file_pk          INTEGER,
    unit_pk          INTEGER,
    severity         TEXT NOT NULL,   -- error / warning / info
    code             TEXT NOT NULL,
    detail           TEXT NOT NULL,
    original         TEXT,
    group_path       TEXT,
    unit_id          TEXT,
    location_json    TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_units_delivery ON units(delivery_id, file_pk);
CREATE INDEX IF NOT EXISTS idx_units_candidate ON units(candidate_id);
CREATE INDEX IF NOT EXISTS idx_diag_run ON diagnostics(run_id);
CREATE INDEX IF NOT EXISTS idx_sources_candidate ON candidate_sources(candidate_id);
"""

STATUS_CANDIDATE = "candidate"
STATUS_DRAFT = "draft"
STATUS_REJECTED = "rejected"


@dataclass
class Outcome:
    run_id: int
    status: str
    delivery_id: int | None
    summary: dict
    error: dict | None
    exit_code: int


class XliffStore:
    """The intake job's own database; the pipeline database is never written."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute("PRAGMA synchronous = NORMAL")

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        Only additive changes are needed: existing candidates keep their
        identity; the source text is backfilled from the first unit that
        produced them (the same text the binding check already verified).
        """
        columns = {row["name"] for row in self.conn.execute(
            "PRAGMA table_info(candidates)")}
        if "source_text" not in columns:
            with self.tx() as conn:
                conn.execute("ALTER TABLE candidates ADD COLUMN source_text "
                             "TEXT NOT NULL DEFAULT ''")
                conn.execute(
                    "UPDATE candidates SET source_text = COALESCE(("
                    "  SELECT u.source_text FROM units u "
                    "  WHERE u.unit_pk = candidates.first_unit_pk), '')")

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

    # -- runs ------------------------------------------------------------
    def mark_interrupted_runs(self, at: str) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE runs SET status='interrupted', finished_at=?, "
                "error_json=COALESCE(error_json, ?) WHERE status='running'",
                (at, json.dumps({"code": "process_interrupted",
                                 "detail": "the process died before the delivery "
                                           "transaction committed"},
                                ensure_ascii=False)))
            return cur.rowcount

    def create_run(self, started_at: str, review_path: Path, review_sha: str,
                   review_bytes: int, mapping_path: Path, mapping_sha: str,
                   mapping_digest: str) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO runs(started_at, status, review_path, review_sha256, "
                "review_bytes, mapping_path, mapping_sha256, mapping_digest) "
                "VALUES (?,'running',?,?,?,?,?,?)",
                (started_at, str(review_path), review_sha, review_bytes,
                 str(mapping_path), mapping_sha, mapping_digest))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict,
                   delivery_id: int | None = None,
                   error: dict | None = None, units_total: int = 0) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, delivery_id=?, "
                "units_total=?, summary_json=?, error_json=? WHERE run_id=?",
                (now_iso(), status, delivery_id, units_total,
                 json.dumps(summary, ensure_ascii=False),
                 json.dumps(error, ensure_ascii=False) if error else None,
                 run_id))

    def run(self, run_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def runs(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM runs ORDER BY run_id")]

    def delivery_by_digests(self, review_sha: str, mapping_sha: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM deliveries WHERE review_sha256=? AND mapping_sha256=?",
            (review_sha, mapping_sha)).fetchone()
        return dict(row) if row else None

    # -- read-back -------------------------------------------------------
    def deliveries(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM deliveries ORDER BY delivery_id")]

    def files(self, delivery_id: int | None = None) -> list[dict]:
        if delivery_id is None:
            rows = self.conn.execute(
                "SELECT * FROM files ORDER BY delivery_id, file_index")
        else:
            rows = self.conn.execute(
                "SELECT * FROM files WHERE delivery_id=? ORDER BY file_index",
                (delivery_id,))
        return [dict(row) for row in rows]

    def units(self, delivery_id: int | None = None,
              status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM units"
        clauses, params = [], []
        if delivery_id is not None:
            clauses.append("delivery_id=?")
            params.append(delivery_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY delivery_id, file_pk, unit_index"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def unit(self, unit_pk: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM units WHERE unit_pk=?", (unit_pk,)).fetchone()
        return dict(row) if row else None

    def unit_by_id(self, unit_id: str, delivery_id: int | None = None) -> list[dict]:
        if delivery_id is None:
            rows = self.conn.execute(
                "SELECT * FROM units WHERE unit_id=? ORDER BY unit_pk", (unit_id,))
        else:
            rows = self.conn.execute(
                "SELECT * FROM units WHERE unit_id=? AND delivery_id=? "
                "ORDER BY unit_pk", (unit_id, delivery_id))
        return [dict(row) for row in rows]

    def candidates(self, source_id: str | None = None,
                   archive_id: str | None = None,
                   version_no: int | None = None) -> list[dict]:
        sql = "SELECT * FROM candidates"
        clauses, params = [], []
        for column, value in (("source_id", source_id),
                              ("archive_id", archive_id),
                              ("version_no", version_no)):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_id, archive_id, version_no, span_start, " \
               "source_language, target_language, candidate_id"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def candidate(self, candidate_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE candidate_id=?",
            (candidate_id,)).fetchone()
        return dict(row) if row else None

    def candidate_sources(self, candidate_id: int) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT cs.*, d.review_path, d.review_sha256, d.mapping_path, "
            "d.mapping_sha256, d.created_at AS delivery_created_at, "
            "u.unit_id, u.group_path_json, u.file_pk "
            "FROM candidate_sources cs "
            "JOIN deliveries d ON d.delivery_id=cs.delivery_id "
            "JOIN units u ON u.unit_pk=cs.unit_pk "
            "WHERE cs.candidate_id=? ORDER BY cs.candidate_source_id",
            (candidate_id,))]

    def group_state(self, source_id: str | None = None,
                    archive_id: str | None = None,
                    state: str | None = None) -> list[dict]:
        sql = "SELECT * FROM group_state"
        clauses, params = [], []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if archive_id:
            clauses.append("archive_id=?")
            params.append(archive_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (" ORDER BY source_id, archive_id, version_no, span_start, "
                "source_language, target_language")
        return [dict(row) for row in self.conn.execute(sql, params)]

    def diagnostics(self, run_id: int | None = None,
                    delivery_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM diagnostics"
        clauses, params = [], []
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if delivery_id is not None:
            clauses.append("delivery_id=?")
            params.append(delivery_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY diagnostic_id"
        return [dict(row) for row in self.conn.execute(sql, params)]


# ---------------------------------------------------------------------------
# XML fragments for human inspection
# ---------------------------------------------------------------------------


def _esc_text(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _esc_attr(text: str) -> str:
    return (_esc_text(text).replace('"', "&quot;"))


def serialize_fragment(element: Element) -> str:
    """Serialize a source/target element for storage (prefix-free XLIFF form)."""

    def attrs(el: Element) -> str:
        parts = []
        for name, value in el.attrs.items():
            if name == f"{XML_NAMESPACE} lang":
                parts.append(f' xml:lang="{_esc_attr(value)}"')
            elif name.startswith(f"{XLIF_NAMESPACE} "):
                parts.append(
                    f' {name.split(" ", 1)[1]}="{_esc_attr(value)}"')
            elif " " in name:
                continue  # other namespaced attributes are not part of the subset
            else:
                parts.append(f' {name}="{_esc_attr(value)}"')
        return "".join(parts)

    def render(el: Element, root: bool = False) -> str:
        attr = attrs(el)
        if root:
            attr = f' xmlns="{XLIF_NAMESPACE}"' + attr
        if not el.children:
            return f"<{el.name}{attr}/>"
        inner = []
        for child in el.children:
            inner.append(_esc_text(child) if isinstance(child, str)
                         else render(child))
        return f"<{el.name}{attr}>{''.join(inner)}</{el.name}>"

    return render(element, root=True)


# ---------------------------------------------------------------------------
# Intake job
# ---------------------------------------------------------------------------


@dataclass
class FileResult:
    index: int
    element: Element
    original: str | None
    status: str
    reason_code: str | None
    reason_detail: str | None
    source_language: str | None
    target_language: str | None
    binding: FileBinding | None
    version: dict | None
    units: list[dict] = field(default_factory=list)


class IntakeJob:
    """Consume one review file + binding map into ``xliff.sqlite3``."""

    def __init__(self, workdir: str | Path,
                 pipeline_db: str | Path | None = None, progress=None):
        self.workdir = Path(workdir)
        self.pipeline_db = (Path(pipeline_db) if pipeline_db
                            else self.workdir / "pipeline.sqlite3")
        self.store: XliffStore | None = None
        self.pipeline: PipelineReader | None = None
        self.progress = progress or (lambda _msg: None)

    def close(self) -> None:
        if self.pipeline is not None:
            self.pipeline.close()
            self.pipeline = None
        if self.store is not None:
            self.store.close()
            self.store = None

    # -- pre-flight directly in intake -----------------------------------

    # -- main ------------------------------------------------------------
    def intake(self, review_path: str | Path, mapping_path: str | Path,
               fail_point: str | None = None) -> Outcome:
        review_path = Path(review_path)
        mapping_path = Path(mapping_path)
        mapping = load_mapping(mapping_path)
        try:
            raw = review_path.read_bytes()
        except OSError as exc:
            raise XliffError(
                f"cannot read review file {review_path}: {exc}") from exc
        review_sha = hashlib.sha256(raw).hexdigest()
        try:
            root = parse_xliff(raw)
        except XliffSyntaxError as exc:
            # No database is opened or modified: the corpus is untouched.
            raise IntakeRejected(exc.code, exc.detail)
        self.pipeline = PipelineReader(self.pipeline_db)
        # The intake database is created only after the document parsed: a
        # syntax error must leave the corpus (and the workdir) untouched.
        self.store = XliffStore(self.workdir / "xliff.sqlite3")

        started = now_iso()
        stale = self.store.mark_interrupted_runs(started)
        if stale:
            self.progress(f"marked {stale} stale running run(s) as interrupted")
        run_id = self.store.create_run(
            started, review_path, review_sha, len(raw), mapping_path,
            mapping.file_sha256, mapping.digest)

        existing = self.store.delivery_by_digests(review_sha, mapping.file_sha256)
        if existing is not None:
            summary = {
                "status": "duplicate",
                "duplicate_of_delivery_id": existing["delivery_id"],
                "review_sha256": review_sha,
                "mapping_sha256": mapping.file_sha256,
                "message": "identical review file and mapping were already "
                           "intaken; no new records were written",
                "new_deliveries": 0, "new_units": 0, "new_candidates": 0,
                "new_candidate_sources": 0,
            }
            self.store.finish_run(run_id, "duplicate", summary,
                                  delivery_id=existing["delivery_id"])
            self.progress(f"run {run_id}: duplicate delivery, no rows added")
            return Outcome(run_id=run_id, status="duplicate",
                           delivery_id=existing["delivery_id"],
                           summary=summary, error=None, exit_code=0)

        try:
            summary, delivery_id, units_total = self._ingest(
                run_id, review_path, review_sha, len(raw), mapping_path,
                mapping, raw, root, fail_point)
        except IntakeInterrupted:
            self.store.finish_run(
                run_id, "interrupted",
                {"status": "interrupted",
                 "message": "controlled interruption before commit; the delivery "
                            "transaction was rolled back"},
                error={"code": "interrupted",
                       "detail": "fail point reached before commit"})
            raise
        except Exception as exc:
            self.store.finish_run(
                run_id, "failed",
                {"status": "failed"},
                error={"code": "internal_error", "detail": repr(exc)})
            raise
        status = "partial" if summary["errors"] else "completed"
        if status == "partial":
            # A partial delivery is applied on purpose, but the run status still
            # tells an operator that a contract error exists.
            summary["message"] = (
                "XML was well-formed but some files/units broke the contract; "
                "the other units were intaken and every problem is listed per "
                "unit")
        self.store.finish_run(run_id, status, summary,
                              delivery_id=delivery_id, units_total=units_total)
        self.progress(
            f"run {run_id}: {status}; units={summary['units_total']} "
            f"candidate={summary['units_candidate']} "
            f"draft={summary['units_draft']} rejected={summary['units_rejected']} "
            f"new_candidates={summary['new_candidates']} "
            f"conflicts={summary['conflict_groups']}")
        return Outcome(run_id=run_id, status=status, delivery_id=delivery_id,
                       summary=summary, error=None,
                       exit_code=4 if summary["errors"] else 0)

    # -- the delivery transaction ----------------------------------------
    def _ingest(self, run_id: int, review_path: Path, review_sha: str,
                review_bytes: int, mapping_path: Path, mapping: Mapping,
                raw: bytes, root: Element, fail_point: str | None):
        stamp = now_iso()
        if root.attrs.get("version") != "1.2":
            raise IntakeRejected(
                "unsupported_version",
                f"xliff/@version is {root.attrs.get('version')!r}; this job "
                "accepts XLIFF 1.2 only")
        stray = [el for el in root.children
                 if isinstance(el, Element)
                 and not (el.ns == XLIF_NAMESPACE and el.name == "file")]
        if stray:
            raise IntakeRejected(
                "unsupported_root_child",
                "unsupported element(s) directly under xliff: "
                + ", ".join(sorted({el.qname for el in stray})))
        file_elements = [el for el in root.children if isinstance(el, Element)]
        if not file_elements:
            raise IntakeRejected(
                "no_file", "the XLIFF document contains no file element")
        originals = [el.attrs.get("original") for el in file_elements]
        duplicate_originals = {
            original for original in originals
            if original is not None and originals.count(original) > 1}
        mapped_originals = set(mapping.files)
        seen_originals = {o for o in originals if o is not None}
        missing_files = sorted(mapped_originals - seen_originals)

        diagnostics: list[dict] = []
        for original in missing_files:
            diagnostics.append({
                "severity": "error", "code": "mapping_file_missing",
                "detail": f"binding map lists original {original!r} but the "
                          "XLIFF document has no such file element",
                "original": original, "group_path": None, "unit_id": None,
                "file_index": None, "unit_index": None, "file_pk": None,
                "unit_pk": None,
                "location": {"mapping": str(mapping_path)}})

        # Everything is validated before the transaction opens: the plan below
        # is the complete set of rows that will be written, and no database
        # write happens while a unit can still add an error.
        file_plans: list[FileResult] = []
        for index, element in enumerate(file_elements):
            file_plans.append(self._plan_file(index, element, mapping,
                                              duplicate_originals))
        unit_records = [unit for result in file_plans for unit in result.units]
        for result in file_plans:
            if result.status != "accepted":
                diagnostics.append({
                    "severity": "error",
                    "code": result.reason_code or "file_rejected",
                    "detail": result.reason_detail or "",
                    "original": result.original, "group_path": None,
                    "unit_id": None, "file_index": result.index,
                    "unit_index": None, "file_pk": None, "unit_pk": None,
                    "location": {"file_index": result.index,
                                 "line": result.element.line}})

        group_candidates: dict[tuple, set[int]] = {}
        new_candidate_ids: list[int] = []
        groups_touched: set[tuple] = set()
        with self.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO deliveries(run_id, review_path, review_sha256, "
                "review_bytes, mapping_path, mapping_sha256, mapping_digest, "
                "mapping_description, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, str(review_path), review_sha, review_bytes,
                 str(mapping_path), mapping.file_sha256, mapping.digest,
                 mapping.description, stamp))
            delivery_id = int(cur.lastrowid)

            file_pks: list[int] = []
            for result in file_plans:
                cur = conn.execute(
                    "INSERT INTO files(delivery_id, file_index, original, "
                    "source_language, target_language, bound_source_id, "
                    "bound_archive_id, bound_version_no, status, reason_code, "
                    "reason_detail, line, column, unit_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (delivery_id, result.index, result.original,
                     result.source_language, result.target_language,
                     result.binding.source_id if result.binding else None,
                     result.binding.archive_id if result.binding else None,
                     result.binding.version_no if result.binding else None,
                     result.status, result.reason_code, result.reason_detail,
                     result.element.line, result.element.column,
                     len(result.units)))
                file_pks.append(int(cur.lastrowid))

            for result, file_pk in zip(file_plans, file_pks):
                for unit in result.units:
                    cur = conn.execute(
                        "INSERT INTO units(delivery_id, file_pk, unit_index, "
                        "unit_id, group_path_json, status, reason_code, "
                        "reason_detail, approved, target_state, source_language, "
                        "target_language, declared_source_lang, "
                        "declared_target_lang, span_start, span_end, source_text, "
                        "target_text, source_codes_json, target_codes_json, "
                        "source_xml, target_xml, event_digest, line, column, "
                        "source_line, target_line) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "?,?,?,?)",
                        (delivery_id, file_pk, unit["unit_index"],
                         unit.get("unit_id"),
                         json.dumps(unit.get("group_path", []),
                                    ensure_ascii=False),
                         unit["status"], unit.get("reason_code"),
                         unit.get("reason_detail"), unit.get("approved"),
                         unit.get("target_state"), unit.get("source_language"),
                         unit.get("target_language"),
                         unit.get("declared_source_lang"),
                         unit.get("declared_target_lang"),
                         unit.get("span_start"), unit.get("span_end"),
                         unit.get("source_text"), unit.get("target_text"),
                         _json_or_none(unit.get("source_codes")),
                         _json_or_none(unit.get("target_codes")),
                         unit.get("source_xml"), unit.get("target_xml"),
                         unit.get("event_digest"), unit.get("line"),
                         unit.get("column"), unit.get("source_line"),
                         unit.get("target_line")))
                    unit_pk = int(cur.lastrowid)
                    unit["unit_pk"] = unit_pk
                    for diag in unit.get("diagnostics", []):
                        diagnostics.append(dict(
                            diag, file_index=result.index,
                            unit_index=unit["unit_index"],
                            unit_id=unit.get("unit_id"),
                            group_path=unit.get("group_path"),
                            file_pk=file_pk, unit_pk=unit_pk))

            for result, file_pk in zip(file_plans, file_pks):
                for unit in result.units:
                    if unit["status"] != STATUS_CANDIDATE:
                        continue
                    binding = result.binding
                    assert binding is not None
                    key = (binding.source_id, binding.archive_id,
                           binding.version_no, unit["span_start"],
                           unit["span_end"], unit["source_language"],
                           unit["target_language"])
                    groups_touched.add(key)
                    event_json = json.dumps(unit["events"], ensure_ascii=False,
                                            separators=(",", ":"))
                    event_digest = hashlib.sha256(
                        event_json.encode("utf-8")).hexdigest()
                    existing = conn.execute(
                        "SELECT candidate_id FROM candidates WHERE "
                        "source_id=? AND archive_id=? AND version_no=? AND "
                        "span_start=? AND span_end=? AND source_language=? AND "
                        "target_language=? AND event_digest=?",
                        (*key, event_digest)).fetchone()
                    if existing is None:
                        cur = conn.execute(
                            "INSERT INTO candidates(source_id, archive_id, "
                            "version_no, span_start, span_end, source_language, "
                            "target_language, source_text, target_text, "
                            "target_codes_json, event_digest, event_json, "
                            "first_delivery_id, first_unit_pk, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (*key, unit["source_text"], unit["target_text"],
                             _json_or_none(unit["target_codes"]), event_digest,
                             event_json, delivery_id, unit["unit_pk"], stamp))
                        candidate_id = int(cur.lastrowid)
                        new_candidate_ids.append(candidate_id)
                    else:
                        candidate_id = int(existing["candidate_id"])
                    group_candidates.setdefault(key, set()).add(candidate_id)
                    conn.execute(
                        "INSERT OR IGNORE INTO candidate_sources(candidate_id, "
                        "delivery_id, unit_pk, created_at) VALUES (?,?,?,?)",
                        (candidate_id, delivery_id, unit["unit_pk"], stamp))
                    conn.execute(
                        "UPDATE units SET candidate_id=? WHERE unit_pk=?",
                        (candidate_id, unit["unit_pk"]))
                    unit["candidate_id"] = candidate_id

            for diag in diagnostics:
                conn.execute(
                    "INSERT INTO diagnostics(run_id, delivery_id, file_pk, "
                    "unit_pk, severity, code, detail, original, group_path, "
                    "unit_id, location_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, delivery_id, diag.get("file_pk"),
                     diag.get("unit_pk"), diag["severity"], diag["code"],
                     diag["detail"], diag.get("original"),
                     ("/" + "/".join(diag["group_path"]))
                     if diag.get("group_path") else None,
                     diag.get("unit_id"),
                     json.dumps(diag.get("location", {}), ensure_ascii=False),
                     stamp))

            for key in groups_touched:
                self._recompute_group(conn, key, stamp)
            conflict_groups = 0
            for key in groups_touched:
                state = conn.execute(
                    "SELECT state FROM group_state WHERE source_id=? AND "
                    "archive_id=? AND version_no=? AND span_start=? AND "
                    "span_end=? AND source_language=? AND target_language=?",
                    key).fetchone()
                if state is not None and state["state"] == "conflict":
                    conflict_groups += 1

            if fail_point == "before_commit":
                raise IntakeInterrupted(
                    "controlled interruption before commit")

        counts = {"candidate": 0, "draft": 0, "rejected": 0}
        for unit in unit_records:
            counts[unit["status"]] = counts.get(unit["status"], 0) + 1
        errors = [d for d in diagnostics if d["severity"] == "error"]
        summary = {
            "status": "partial" if errors else "completed",
            "delivery_id": delivery_id,
            "review_sha256": review_sha,
            "mapping_digest": mapping.digest,
            "files_total": len(file_plans),
            "files_accepted": sum(1 for r in file_plans if r.status == "accepted"),
            "files_rejected": sum(1 for r in file_plans if r.status != "accepted"),
            "units_total": len(unit_records),
            "units_candidate": counts.get("candidate", 0),
            "units_draft": counts.get("draft", 0),
            "units_rejected": counts.get("rejected", 0),
            "new_candidates": len(new_candidate_ids),
            "new_candidate_sources": sum(
                1 for result in file_plans for unit in result.units
                if unit["status"] == STATUS_CANDIDATE),
            "groups_touched": len(groups_touched),
            "conflict_groups": conflict_groups,
            "errors": len(errors),
            "diagnostics": len(diagnostics),
            "mapping_files_missing": missing_files,
            "file_results": [
                {"index": r.index, "original": r.original,
                 "status": r.status, "reason_code": r.reason_code,
                 "reason_detail": r.reason_detail,
                 "units": len(r.units),
                 "bound": ({"source_id": r.binding.source_id,
                            "archive_id": r.binding.archive_id,
                            "version_no": r.binding.version_no}
                           if r.binding else None)}
                for r in file_plans
            ],
            "unit_results": [
                {"file_index": r.index, "unit_id": u.get("unit_id"),
                 "unit_index": u["unit_index"],
                 "status": u["status"], "reason_code": u.get("reason_code"),
                 "reason_detail": u.get("reason_detail"),
                 "span": [u.get("span_start"), u.get("span_end")],
                 "group_path": u.get("group_path", []),
                 "candidate_id": u.get("candidate_id")}
                for r in file_plans for u in r.units
            ],
        }
        return summary, delivery_id, len(unit_records)

    def _recompute_group(self, conn, key: tuple, stamp: str) -> None:
        source_id, archive_id, version_no, start, end, sl, tl = key
        rows = [dict(row) for row in conn.execute(
            "SELECT candidate_id FROM candidates WHERE source_id=? AND "
            "archive_id=? AND version_no=? AND span_start=? AND span_end=? "
            "AND source_language=? AND target_language=? ORDER BY candidate_id",
            key)]
        if not rows:
            conn.execute(
                "DELETE FROM group_state WHERE source_id=? AND archive_id=? AND "
                "version_no=? AND span_start=? AND span_end=? AND "
                "source_language=? AND target_language=?", key)
            return
        if len(rows) == 1:
            conn.execute(
                "INSERT INTO group_state(source_id, archive_id, version_no, "
                "span_start, span_end, source_language, target_language, state, "
                "available_candidate_id, candidate_count, reason, recomputed_at) "
                "VALUES (?,?,?,?,?,?,?, 'available', ?, 1, NULL, ?) "
                "ON CONFLICT(source_id, archive_id, version_no, span_start, "
                "span_end, source_language, target_language) DO UPDATE SET "
                "state='available', available_candidate_id=excluded."
                "available_candidate_id, candidate_count=1, reason=NULL, "
                "recomputed_at=excluded.recomputed_at",
                (*key, rows[0]["candidate_id"], stamp))
        else:
            reason = (f"{len(rows)} distinct approved translations for the same "
                      "notice version, span and language pair; awaiting human "
                      "review — none is offered as the unique usable answer")
            conn.execute(
                "INSERT INTO group_state(source_id, archive_id, version_no, "
                "span_start, span_end, source_language, target_language, state, "
                "available_candidate_id, candidate_count, reason, recomputed_at) "
                "VALUES (?,?,?,?,?,?,?, 'conflict', NULL, ?, ?, ?) "
                "ON CONFLICT(source_id, archive_id, version_no, span_start, "
                "span_end, source_language, target_language) DO UPDATE SET "
                "state='conflict', available_candidate_id=NULL, "
                "candidate_count=excluded.candidate_count, reason=excluded.reason, "
                "recomputed_at=excluded.recomputed_at",
                (*key, len(rows), reason, stamp))

    # -- per-file planning ----------------------------------------------
    def _plan_file(self, index: int, element: Element, mapping: Mapping,
                   duplicate_originals: set[str]) -> FileResult:
        original = element.attrs.get("original")
        sl_raw = element.attrs.get("source-language")
        tl_raw = element.attrs.get("target-language")
        sl = normalize_language(sl_raw)
        tl = normalize_language(tl_raw)
        result = FileResult(index=index, element=element, original=original,
                            status="rejected", reason_code=None,
                            reason_detail=None, source_language=sl,
                            target_language=tl, binding=None, version=None)

        def reject(code: str, detail: str, body: Element | None = None) -> FileResult:
            result.status = "rejected"
            result.reason_code = code
            result.reason_detail = detail
            if body is not None:
                units = self._collect_units(body)
                duplicate_ids = self._duplicate_ids(units)
                result.units = [
                    self._process_unit(position, unit_el, group_path, result,
                                       duplicate_ids, body_text=None)
                    for position, (unit_el, group_path) in enumerate(units)]
            return result

        if not isinstance(original, str) or not original:
            return reject("file_original_missing",
                          "file element must carry a non-empty original")
        if original in duplicate_originals:
            return reject("duplicate_original",
                          f"original {original!r} appears on more than one file "
                          "element in this document; the binding would be "
                          "ambiguous")
        bodies = [child for child in element.children
                  if isinstance(child, Element)
                  and child.ns == XLIF_NAMESPACE and child.name == "body"]
        if not bodies:
            return reject("file_missing_body", "file has no body element")
        if len(bodies) > 1:
            return reject("file_multiple_bodies",
                          f"file has {len(bodies)} body elements; exactly one is "
                          "accepted")
        body = bodies[0]
        unsupported = [child for child in element.children
                       if isinstance(child, Element)
                       and not (child.ns == XLIF_NAMESPACE
                                and child.name in {"body", "header"})]
        if unsupported:
            return reject(
                "file_unsupported_child",
                "unsupported file child element(s): "
                + ", ".join(sorted({child.qname for child in unsupported})),
                body)
        # Container-level structure only: a stray element directly under body
        # or group invalidates the file (there is no unit id to attach it to),
        # but an unsupported construct *inside* one source/target invalidates
        # only that unit (``expand`` reports it), so other units still intake.
        container_children = {"group", "trans-unit"}
        for child in body.children:
            if isinstance(child, Element) and not (
                    child.ns == XLIF_NAMESPACE
                    and child.name in container_children):
                return reject(
                    "body_unsupported_child",
                    f"unsupported body child {child.qname!r}; only group and "
                    "trans-unit may appear directly in a body", body)

        def check_group(group: Element) -> str | None:
            for sub in group.children:
                if not isinstance(sub, Element):
                    continue
                if sub.ns != XLIF_NAMESPACE or sub.name not in container_children:
                    return f"unsupported group child {sub.qname!r}"
                if sub.name == "group":
                    nested = check_group(sub)
                    if nested:
                        return nested
            return None

        for group in body.find_all("group"):
            problem = check_group(group)
            if problem:
                return reject("group_unsupported_child", problem, body)
        if sl is None or tl is None:
            return reject(
                "invalid_language",
                f"source-language={sl_raw!r}, target-language={tl_raw!r}; "
                "expected hyphen-separated ASCII tags such as zh-CN or en-US",
                body)
        if sl == tl:
            return reject(
                "same_language",
                f"source-language and target-language are both {sl!r}; "
                "en-US and en-GB are distinct targets and neither may be "
                "collapsed into the other", body)

        binding = mapping.entry(original)
        if binding is None:
            return reject(
                "unmapped_original",
                f"original {original!r} is not present in the binding map; the "
                "notice identity and version cannot be assumed", body)
        result.binding = binding
        if binding.source_language and binding.source_language != sl:
            return reject(
                "file_language_mismatch",
                f"binding map pins source_language={binding.source_language!r} "
                f"but the file declares {sl!r}", body)
        if binding.target_language and binding.target_language != tl:
            return reject(
                "file_language_mismatch",
                f"binding map pins target_language={binding.target_language!r} "
                f"but the file declares {tl!r}", body)
        version = self.pipeline.version(binding.source_id, binding.archive_id,
                                        binding.version_no)
        if version is None:
            known = self.pipeline.source_versions(binding.source_id,
                                                  binding.archive_id)
            known_versions = [v["version_no"] for v in known]
            identities = self.pipeline.source_identities(binding.source_id)
            return reject(
                "unknown_version",
                f"source {binding.source_id!r} archive {binding.archive_id!r} "
                f"v{binding.version_no} does not exist; versions present for "
                f"that identity: {known_versions or 'none'}; identities in that "
                f"source: {[i['archive_id'] for i in identities]}", body)
        result.version = version
        result.status = "accepted"
        result.reason_code = None
        result.reason_detail = None

        units = self._collect_units(body)
        duplicate_ids = self._duplicate_ids(units)
        result.units = [
            self._process_unit(
                position, unit_el, group_path, result, duplicate_ids,
                body_text=version["body"],
                span=binding.units.get(unit_el.attrs.get("id", "")))
            for position, (unit_el, group_path) in enumerate(units)
        ]
        return result

    @staticmethod
    def _collect_units(body: Element):
        """Document order of trans-units with their group id path."""
        collected: list[tuple[Element, list[str]]] = []

        def walk(container: Element, path: list[str]) -> None:
            for child in container.children:
                if not isinstance(child, Element):
                    continue
                if child.ns != XLIF_NAMESPACE:
                    continue
                if child.name == "group":
                    group_id = child.attrs.get("id")
                    label = group_id if group_id else f"group[{len(path)}]"
                    walk(child, path + [label])
                elif child.name == "trans-unit":
                    collected.append((child, list(path)))
                # Other children were rejected at file level; skip here.

        walk(body, [])
        return collected

    @staticmethod
    def _duplicate_ids(units: list[tuple[Element, list[str]]]) -> set[str]:
        seen: dict[str, int] = {}
        for element, _path in units:
            unit_id = element.attrs.get("id")
            if isinstance(unit_id, str) and unit_id:
                seen[unit_id] = seen.get(unit_id, 0) + 1
        return {unit_id for unit_id, count in seen.items() if count > 1}

    def _process_unit(self, unit_index: int, element: Element,
                      group_path: list[str], file_result: FileResult,
                      duplicate_ids: set[str], body_text: str | None,
                      span: "UnitSpan | None" = None) -> dict:
        unit_id = element.attrs.get("id")
        approved = element.attrs.get("approved")
        record: dict[str, Any] = {
            "unit_index": unit_index,
            "unit_id": unit_id if isinstance(unit_id, str) and unit_id else None,
            "group_path": group_path,
            "approved": approved,
            "target_state": None,
            "source_language": file_result.source_language,
            "target_language": file_result.target_language,
            "declared_source_lang": None,
            "declared_target_lang": None,
            "span_start": None,
            "span_end": None,
            "source_text": None,
            "target_text": None,
            "source_codes": None,
            "target_codes": None,
            "source_xml": None,
            "target_xml": None,
            "event_digest": None,
            "events": None,
            "status": STATUS_REJECTED,
            "reason_code": None,
            "reason_detail": None,
            "diagnostics": [],
            "line": element.line,
            "column": element.column,
            "source_line": None,
            "target_line": None,
        }

        def diag(code: str, detail: str, severity: str = "error",
                 location: dict | None = None) -> None:
            record["diagnostics"].append({
                "severity": severity, "code": code, "detail": detail,
                "original": file_result.original,
                "group_path": group_path,
                "unit_id": record["unit_id"],
                "location": location or {},
            })

        def reject(code: str, detail: str) -> dict:
            record["status"] = STATUS_REJECTED
            record["reason_code"] = code
            record["reason_detail"] = detail
            diag(code, detail)
            return record

        if not isinstance(unit_id, str) or not unit_id:
            return reject("unit_id_missing",
                          "trans-unit must carry a non-empty id (the binding map "
                          "is keyed by it)")
        if span is not None:
            record["span_start"], record["span_end"] = span.start, span.end
        if unit_id in duplicate_ids:
            return reject(
                "duplicate_unit_id",
                f"unit id {unit_id!r} occurs more than once in this file; every "
                "occurrence in the file is invalid, so no copy is kept as usable")
        if file_result.status != "accepted":
            return reject(
                file_result.reason_code or "file_rejected",
                f"file-level rejection: {file_result.reason_detail}")

        sources = [child for child in element.children
                   if isinstance(child, Element)
                   and child.ns == XLIF_NAMESPACE and child.name == "source"]
        targets = [child for child in element.children
                   if isinstance(child, Element)
                   and child.ns == XLIF_NAMESPACE and child.name == "target"]
        unsupported = [child for child in element.children
                       if isinstance(child, Element) and not (
                           child.ns == XLIF_NAMESPACE
                           and child.name in {"source", "target", "note"})]
        if unsupported:
            return reject(
                "unit_unsupported_child",
                "unsupported trans-unit child element(s): "
                + ", ".join(sorted({c.name for c in unsupported})))
        if len(sources) != 1:
            return reject(
                "source_count",
                f"trans-unit has {len(sources)} source elements; exactly one is "
                "required")
        if len(targets) != 1:
            return reject(
                "target_count",
                f"trans-unit has {len(targets)} target elements; exactly one is "
                "required (the source is never used as a translation)")
        source_el, target_el = sources[0], targets[0]
        record["source_line"] = source_el.line
        record["target_line"] = target_el.line
        record["source_xml"] = serialize_fragment(source_el)
        record["target_xml"] = serialize_fragment(target_el)
        record["target_state"] = target_el.attrs.get("state")

        src_declared = source_el.attrs.get(f"{XML_NAMESPACE} lang")
        tgt_declared = target_el.attrs.get(f"{XML_NAMESPACE} lang")
        record["declared_source_lang"] = src_declared
        record["declared_target_lang"] = tgt_declared
        for declared, expected, side in (
                (src_declared, file_result.source_language, "source"),
                (tgt_declared, file_result.target_language, "target")):
            if declared is None:
                continue
            normalized = normalize_language(declared)
            if normalized is None:
                return reject(
                    "unit_language_invalid",
                    f"{side} xml:lang={declared!r} is not a valid language tag")
            if normalized != expected:
                return reject(
                    "unit_language_mismatch",
                    f"{side} xml:lang={declared!r} does not match the file's "
                    f"{side}-language {expected!r}")

        try:
            source_expanded = expand(source_el, side="source")
        except InlineError as exc:
            return reject(exc.code, exc.message)
        try:
            target_expanded = expand(target_el, side="target")
        except InlineError as exc:
            return reject(exc.code, exc.message)

        record["source_text"] = source_expanded.text
        record["target_text"] = target_expanded.text
        record["source_codes"] = code_tree(source_expanded)
        record["target_codes"] = code_tree(target_expanded)
        record["events"] = events_for_comparison(target_expanded)
        record["event_digest"] = hashlib.sha256(json.dumps(
            record["events"], ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()

        binding = file_result.binding
        if span is None:
            return reject(
                "binding_missing",
                f"the binding map has no [start,end) span for unit {unit_id!r} "
                f"in original {file_result.original!r}")
        if span.end > len(body_text):
            return reject(
                "binding_out_of_bounds",
                f"span [{span.start},{span.end}) exceeds the archived body "
                f"length {len(body_text)}")
        expected = body_text[span.start:span.end]
        if source_expanded.text != expected:
            detail = (f"source expansion does not equal body[{span.start}:"
                      f"{span.end}] of {binding.source_id}/{binding.archive_id}"
                      f"/v{binding.version_no}: expected {expected!r}, got "
                      f"{source_expanded.text!r}")
            hints = self._binding_hints(binding, file_result.version, span,
                                        source_expanded.text)
            if hints:
                detail += " — " + "; ".join(hints)
            return reject("binding_mismatch", detail)
        if not target_expanded.codes and not target_expanded.text.strip():
            return reject("empty_target",
                          "target has no text (only whitespace at most) and no "
                          "codes; the source text is never substituted as a "
                          "translation")

        problems = compare_structure(source_expanded, target_expanded)
        if problems:
            first = problems[0]
            detail = "; ".join(p["detail"] for p in problems)
            record["diagnostics"].append({
                "severity": "error", "code": first["code"], "detail": detail,
                "original": file_result.original, "group_path": group_path,
                "unit_id": unit_id,
                "location": {"span": [span.start, span.end],
                             "source_codes": record["source_codes"],
                             "target_codes": record["target_codes"]},
            })
            record["status"] = STATUS_REJECTED
            record["reason_code"] = first["code"]
            record["reason_detail"] = detail
            return record

        if approved != "yes" or record["target_state"] != "final":
            reasons = []
            if approved != "yes":
                reasons.append(f"approved={approved!r} (needs 'yes')")
            if record["target_state"] != "final":
                reasons.append(
                    f"target state={record['target_state']!r} (needs 'final')")
            record["status"] = STATUS_DRAFT
            record["reason_code"] = "draft_not_approved"
            record["reason_detail"] = (
                "well-formed but not an approved final translation: "
                + ", ".join(reasons) + "; kept as draft, never queryable")
            diag(record["reason_code"], record["reason_detail"],
                 severity="info")
            return record

        record["status"] = STATUS_CANDIDATE
        return record

    def _binding_hints(self, binding: FileBinding, version: dict,
                       span: UnitSpan, text: str) -> list[str]:
        """Explain a mismatch without ever switching to another body."""
        hints: list[str] = []
        body = version["body"]
        position = body.find(text)
        if position >= 0:
            hints.append(f"the source text occurs in the bound body at "
                         f"[{position},{position + len(text)})")
        siblings = self.pipeline.sibling_versions(
            binding.source_id, binding.archive_id, version.get("notice_key"))
        for sibling in siblings:
            cropped = sibling["body"][span.start:span.end]
            if cropped == text:
                hints.append(
                    f"the span matches a different business variant "
                    f"{sibling['archive_id']} v{sibling['version_no']} — do not "
                    "borrow the other variant's body")
                break
        others = [v for v in self.pipeline.source_versions(
            binding.source_id, binding.archive_id)
            if v["version_no"] != binding.version_no]
        for other in others:
            cropped = other["body"][span.start:span.end]
            if cropped == text:
                hints.append(
                    f"the span matches version {other['version_no']} of the same "
                    f"identity — a new version is never substituted "
                    "automatically")
                break
        return hints


def _json_or_none(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def code_tree(expanded: Expanded) -> list[dict]:
    return [_branch_dict(branch) for branch in build_branches(expanded)]


def _branch_dict(branch) -> dict:
    return {"kind": branch.kind, "id": branch.code_id,
            "equiv_text": branch.equiv_text,
            "children": [_branch_dict(child) for child in branch.children]}

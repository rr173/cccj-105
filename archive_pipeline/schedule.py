"""Schedule-organizing job: archived notices → queryable project schedules.

Independent from web scanning and from the funding-table job, this module
only reads the local identity-schema pipeline database (read-only) plus the
content-addressed raw archive; it never fetches anything, never migrates the
pipeline database and never rewrites a scan result or an archived blob.
Its outputs live in a separate ``schedule.sqlite3``.

Event model
-----------
Every schedule-changing observation is an append-only **ledger row**
(``node_ledger``): ``set`` (a node gets a date, also used by extensions),
``cancel_node`` (one node disappears) or ``cancel_round`` (the whole round
stops being accepted — scoped differently from a single-node cancellation).
Rows are keyed by the exact evidence they came from, so re-organizing the
same archive with the same vocabulary is idempotent.

A run replays the *latest version* of each archive identity in chronological
order.  After every version has committed its own transaction, one final
transaction replaces the published snapshot (``current_nodes`` /
``round_schedules`` / ``pending_current``).  A crash before that point leaves
the previous published schedule untouched — no half-published arrangement is
ever queryable.

Cross-notice references
-----------------------
A supplement/correction that claims to change another notice is resolved by
``(source, notice number)``.  Zero matches, several matches, a role the
target never had, or purely vague wording all land in ``pending_*`` as
material awaiting human confirmation; such material is never merged into the
confirmed schedule.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .archive import RawArchive
from .scheduleconfig import ScheduleVocab, load_schedule_vocab
from .scheduleextract import (
    DateEvidence,
    NoticeExtraction,
    PendingItem,
    extract_notice,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VOCAB_CONFIG = ROOT / "config" / "schedule_vocab.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ScheduleError(RuntimeError):
    """Operational failure: missing/legacy pipeline state, bad config…"""


class ScheduleInterrupted(RuntimeError):
    """Controlled mid-version interruption (tests/demo).

    Raised inside the per-version transaction, which is therefore rolled
    back; no ledger row of the interrupted version survives.
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

CREATE TABLE IF NOT EXISTS extraction_versions (
    source_id       TEXT NOT NULL,
    archive_id      TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    notice_key      TEXT,
    title           TEXT NOT NULL,
    issued_on       TEXT NOT NULL,
    notice_type     TEXT NOT NULL,
    type_phrase     TEXT,
    is_latest       INTEGER NOT NULL,
    body_digest     TEXT NOT NULL,
    raw_digest      TEXT,
    raw_status      TEXT,             -- ok / missing_raw / digest_mismatch
    project_code    TEXT,
    config_digest   TEXT NOT NULL,
    run_id          INTEGER NOT NULL,
    extracted_at    TEXT NOT NULL,
    PRIMARY KEY (source_id, archive_id, version_no, config_digest)
);

CREATE TABLE IF NOT EXISTS date_evidence (
    evidence_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    archive_id      TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    config_digest   TEXT NOT NULL,
    run_id          INTEGER NOT NULL,
    role_name       TEXT NOT NULL,
    role_kind       TEXT NOT NULL,
    role_phrase     TEXT NOT NULL,
    role_phrase_start INTEGER NOT NULL,
    raw_date        TEXT NOT NULL,
    normalized_date TEXT NOT NULL,
    had_year        INTEGER NOT NULL,
    is_range        INTEGER NOT NULL,
    range_connector TEXT,
    endpoint        TEXT,
    round_no        INTEGER NOT NULL,
    round_phrase    TEXT,
    paragraph_index INTEGER NOT NULL,
    paragraph_start INTEGER NOT NULL,
    sentence_start  INTEGER NOT NULL,
    sentence_end    INTEGER NOT NULL,
    date_start      INTEGER NOT NULL,
    date_end        INTEGER NOT NULL,
    sentence_text   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_ledger (
    ledger_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key     TEXT NOT NULL,    -- source + root notice key
    round_no        INTEGER NOT NULL,
    role_name       TEXT NOT NULL,    -- '' for cancel_round
    action          TEXT NOT NULL,    -- set / cancel_node / cancel_round
    normalized_date TEXT,
    source_id       TEXT NOT NULL,
    notice_key      TEXT NOT NULL,
    archive_id      TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    issued_on       TEXT NOT NULL,
    config_digest   TEXT NOT NULL,
    run_id          INTEGER NOT NULL,
    evidence_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (project_key, round_no, role_name, action, normalized_date,
            source_id, archive_id, version_no, config_digest)
);

CREATE TABLE IF NOT EXISTS current_nodes (
    project_key     TEXT NOT NULL,
    round_no        INTEGER NOT NULL,
    role_name       TEXT NOT NULL,
    status          TEXT NOT NULL,    -- active / cancelled
    normalized_date TEXT,
    ledger_id       INTEGER NOT NULL,
    PRIMARY KEY (project_key, round_no, role_name)
);

CREATE TABLE IF NOT EXISTS round_schedules (
    project_key     TEXT NOT NULL,
    round_no        INTEGER NOT NULL,
    status          TEXT NOT NULL,    -- open / round_cancelled
    source_archive_id TEXT NOT NULL,
    source_notice_key TEXT NOT NULL,
    PRIMARY KEY (project_key, round_no)
);

CREATE TABLE IF NOT EXISTS pending_current (
    pending_id      TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    notice_key      TEXT,
    archive_id      TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    code            TEXT NOT NULL,
    detail          TEXT NOT NULL,
    target_source   TEXT,
    target_key      TEXT,
    role_name       TEXT,
    raw_date        TEXT,
    normalized_date TEXT,
    paragraph_index INTEGER NOT NULL,
    sentence_start  INTEGER NOT NULL,
    sentence_end    INTEGER NOT NULL,
    sentence_text   TEXT NOT NULL,
    config_digest   TEXT NOT NULL,
    run_id          INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_history (
    hist_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_id      TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    notice_key      TEXT,
    archive_id      TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    code            TEXT NOT NULL,
    detail          TEXT NOT NULL,
    target_source   TEXT,
    target_key      TEXT,
    role_name       TEXT,
    raw_date        TEXT,
    normalized_date TEXT,
    paragraph_index INTEGER NOT NULL,
    sentence_start  INTEGER NOT NULL,
    sentence_end    INTEGER NOT NULL,
    sentence_text   TEXT NOT NULL,
    config_digest   TEXT NOT NULL,
    run_id          INTEGER NOT NULL,
    recorded_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_meta (
    project_key     TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    root_notice_key TEXT NOT NULL,
    project_code    TEXT
);

CREATE INDEX IF NOT EXISTS idx_ledger_lookup
    ON node_ledger(project_key, round_no, role_name, ledger_id);
CREATE INDEX IF NOT EXISTS idx_evidence_version
    ON date_evidence(source_id, archive_id, version_no, config_digest);
CREATE INDEX IF NOT EXISTS idx_pending_current
    ON pending_current(source_id, archive_id, version_no);
"""


class ScheduleStore:
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

    # -- runs ------------------------------------------------------------
    def mark_interrupted_runs(self, at: str) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE runs SET status='interrupted', finished_at=? "
                "WHERE status='running'", (at,))
            return cur.rowcount

    def create_run(self, started_at: str, vocab: ScheduleVocab) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO runs(started_at, status, config_digest, config_json) "
                "VALUES (?, 'running', ?, ?)",
                (started_at, vocab.digest, vocab.canonical_json))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, at: str, status: str, summary: dict) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, summary_json=? "
                "WHERE run_id=?",
                (at, status,
                 json.dumps(summary, ensure_ascii=False, default=str), run_id))

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM runs ORDER BY run_id")]

    def run(self, run_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?",
                                (run_id,)).fetchone()
        return dict(row) if row else None

    # -- extraction bookkeeping ------------------------------------------
    def extraction_exists(self, key: tuple, config_digest: str) -> bool:
        source_id, archive_id, version_no = key
        row = self.conn.execute(
            "SELECT 1 FROM extraction_versions WHERE source_id=? AND archive_id=? "
            "AND version_no=? AND config_digest=?",
            (source_id, archive_id, version_no, config_digest)).fetchone()
        return row is not None

    def ledger_exists(self, row: dict) -> bool:
        sql = ("SELECT 1 FROM node_ledger WHERE project_key=? AND round_no=? "
               "AND role_name=? AND action=? AND IFNULL(normalized_date,'')="
               "IFNULL(?, '') AND source_id=? AND archive_id=? AND version_no=? "
               "AND config_digest=?")
        got = self.conn.execute(sql, (
            row["project_key"], row["round_no"], row["role_name"],
            row["action"], row["normalized_date"], row["source_id"],
            row["archive_id"], row["version_no"], row["config_digest"])).fetchone()
        return got is not None

    # -- queries ----------------------------------------------------------
    def current_nodes(self, project_key: str | None = None,
                      round_no: int | None = None,
                      on_date: str | None = None,
                      include_cancelled_rounds: bool = False) -> list[dict]:
        sql = ("SELECT n.*, m.source_id, m.root_notice_key, m.project_code, "
               "IFNULL(rs.status, 'open') AS round_status "
               "FROM current_nodes n JOIN project_meta m "
               "ON n.project_key = m.project_key "
               "LEFT JOIN round_schedules rs "
               "ON n.project_key = rs.project_key AND n.round_no = rs.round_no")
        clauses, params = [], []
        if project_key:
            clauses.append("n.project_key=?")
            params.append(project_key)
        if round_no is not None:
            clauses.append("n.round_no=?")
            params.append(round_no)
        if on_date:
            clauses.append("n.normalized_date=?")
            params.append(on_date)
        if not include_cancelled_rounds:
            clauses.append("IFNULL(rs.status, 'open') <> 'round_cancelled'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (" ORDER BY n.project_key, n.round_no, "
                "CASE n.role_name WHEN 'apply_open' THEN 0 "
                "WHEN 'submit_deadline' THEN 1 WHEN 'supplement_deadline' THEN 2 "
                "ELSE 3 END")
        return [dict(r) for r in self.conn.execute(sql, params)]

    def rounds(self, project_key: str | None = None,
               round_no: int | None = None) -> list[dict]:
        sql = ("SELECT r.*, m.source_id, m.root_notice_key, m.project_code "
               "FROM round_schedules r JOIN project_meta m "
               "ON r.project_key = m.project_key")
        clauses, params = [], []
        if project_key:
            clauses.append("r.project_key=?")
            params.append(project_key)
        if round_no is not None:
            clauses.append("r.round_no=?")
            params.append(round_no)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.project_key, r.round_no"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def ledger(self, project_key: str | None = None,
               run_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM node_ledger"
        clauses, params = [], []
        if project_key:
            clauses.append("project_key=?")
            params.append(project_key)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ledger_id"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def evidence(self, source_id: str, archive_id: str, version_no: int,
                 config_digest: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM date_evidence WHERE source_id=? AND archive_id=? "
            "AND version_no=? AND config_digest=? ORDER BY date_start",
            (source_id, archive_id, version_no, config_digest))]

    def evidence_for_ledger(self, ledger_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM node_ledger WHERE ledger_id=?", (ledger_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["evidence"] = self.evidence(
            row["source_id"], row["archive_id"], row["version_no"],
            row["config_digest"])
        return out

    def pending(self, current_only: bool = True,
                source_id: str | None = None) -> list[dict]:
        table = "pending_current" if current_only else "pending_history"
        sql = f"SELECT * FROM {table}"
        clauses, params = [], []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_id, archive_id, version_no, paragraph_index"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def extractions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM extraction_versions ORDER BY source_id, archive_id, "
            "version_no, config_digest")]

    def projects(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM project_meta ORDER BY project_key")]


class PipelineReader:
    """Read-only view of the identity-schema pipeline database."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        if not self.path.exists():
            raise ScheduleError(
                f"pipeline database not found: {self.path} "
                "(run a scan first, or build the sample workdir)")
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        user_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version < 2:
            raise ScheduleError(
                "pipeline database uses the pre-identity schema; the schedule "
                "job consumes identity-schema data only and never migrates the "
                "old database. Run 'python -m archive_pipeline.cli repair "
                "--preview' then 'repair --apply' first.")
        if not self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='versions'"
        ).fetchone():
            raise ScheduleError(f"{self.path} has no versions table")

    def latest_versions(self) -> list[dict]:
        """One row per (source, archive identity): its highest version."""
        return [dict(r) for r in self.conn.execute(
            "SELECT v.* FROM versions v JOIN ("
            "  SELECT source_id, archive_id, MAX(version_no) AS max_no "
            "  FROM versions GROUP BY source_id, archive_id"
            ") t ON v.source_id=t.source_id AND v.archive_id=t.archive_id "
            "AND v.version_no=t.max_no ORDER BY v.issued_on, v.source_id, "
            "v.archive_id, v.version_no")]

    def all_versions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT source_id, archive_id, notice_key, version_no, title, "
            "issued_on, body, raw_digest FROM versions "
            "ORDER BY source_id, archive_id, version_no")]

    def identities_for_notice(self, source_id: str,
                              notice_key: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT DISTINCT archive_id, notice_key, state FROM document_identities "
            "WHERE source_id=? AND notice_key=?", (source_id, notice_key))]

    def notices_for_project_code(self, project_code: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT DISTINCT source_id, archive_id, notice_key, title, issued_on "
            "FROM versions WHERE title LIKE ? OR body LIKE ? "
            "ORDER BY issued_on", (f"%{project_code}%", f"%{project_code}%"))]

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------


class ScheduleJob:
    """Replays archived notices into schedule ledger rows and snapshots."""

    def __init__(self, workdir: str | Path,
                 vocab_path: str | Path | None = None, progress=None):
        self.workdir = Path(workdir)
        self.vocab: ScheduleVocab = load_schedule_vocab(
            vocab_path or DEFAULT_VOCAB_CONFIG)
        self.pipeline = PipelineReader(self.workdir / "pipeline.sqlite3")
        self.archive = RawArchive(self.workdir / "archive")
        self.store = ScheduleStore(self.workdir / "schedule.sqlite3")
        self.progress = progress or (lambda _msg: None)
        self._fail_point: tuple[str, int] | None = None

    def close(self) -> None:
        self.pipeline.close()
        self.store.close()

    # ------------------------------------------------------------------
    def organize(self, source_id: str | None = None,
                 fail_point: tuple[str, int] | None = None) -> dict:
        self._fail_point = fail_point
        started = now_iso()
        stale = self.store.mark_interrupted_runs(started)
        if stale:
            self.progress(f"marked {stale} stale running run(s) as interrupted")
        run_id = self.store.create_run(started, self.vocab)
        summary = {"run_id": run_id, "config_digest": self.vocab.digest,
                   "versions_seen": 0, "versions_skipped": 0,
                   "ledger_rows_added": 0, "pending_open": 0,
                   "cancels_node": 0, "cancels_round": 0}
        try:
            latest = self.pipeline.latest_versions()
            if source_id:
                latest = [v for v in latest if v["source_id"] == source_id]
            summary["versions_seen"] = len(latest)
            project_roots: dict[str, dict] = {}
            for version in latest:
                added, skipped = self._process_version(
                    version, run_id, summary, project_roots)
                summary["versions_skipped"] += skipped
            self._publish_snapshot(run_id, started, summary)
        except ScheduleInterrupted:
            self.store.finish_run(run_id, now_iso(), "interrupted", summary)
            raise
        except Exception:
            self.store.finish_run(run_id, now_iso(), "failed", summary)
            raise
        self.store.finish_run(run_id, now_iso(), "completed", summary)
        return summary

    # ------------------------------------------------------------------
    def _check_raw(self, version: dict) -> str:
        """Soft-verify the raw archive; the blob is never substituted."""
        try:
            raw = self.archive.load(version["raw_digest"])
        except FileNotFoundError:
            return "missing_raw"
        if hashlib.sha256(raw).hexdigest() != version["raw_digest"]:
            return "digest_mismatch"
        return "ok"

    def _process_version(self, version: dict, run_id: int, summary: dict,
                         project_roots: dict[str, dict]) -> tuple[int, int]:
        key3 = (version["source_id"], version["archive_id"],
                version["version_no"])
        raw_status = self._check_raw(version)
        if self.store.extraction_exists(key3, self.vocab.digest):
            return 0, 1

        extraction = extract_notice(
            title=version["title"], notice_key=version["notice_key"] or "",
            issued_on=version["issued_on"], body=version["body"],
            vocab=self.vocab)

        ledger_rows, pendings = self._plan_effects(
            version, extraction, project_roots)
        for item in pendings:
            if item.code.startswith("cancel"):
                if item.code == "cancel_round":
                    summary["cancels_round"] += 1
                else:
                    summary["cancels_node"] += 1

        at = now_iso()
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO extraction_versions(source_id, archive_id, "
                "version_no, notice_key, title, issued_on, notice_type, "
                "type_phrase, is_latest, body_digest, raw_digest, raw_status, "
                "project_code, config_digest, run_id, extracted_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
                (version["source_id"], version["archive_id"],
                 version["version_no"], version["notice_key"],
                 version["title"], version["issued_on"],
                 extraction.notice_type, extraction.notice_type_phrase,
                 hashlib.sha256(version["body"].encode()).hexdigest(),
                 version["raw_digest"], raw_status, extraction.project_code,
                 self.vocab.digest, run_id, at))
            for ev in extraction.dates:
                conn.execute(
                    "INSERT INTO date_evidence(source_id, archive_id, "
                    "version_no, config_digest, run_id, role_name, role_kind, "
                    "role_phrase, role_phrase_start, raw_date, normalized_date, "
                    "had_year, is_range, range_connector, endpoint, round_no, "
                    "round_phrase, paragraph_index, paragraph_start, "
                    "sentence_start, sentence_end, date_start, date_end, "
                    "sentence_text) VALUES ("
                    + ",".join("?" * 24) + ")",
                    (version["source_id"], version["archive_id"],
                     version["version_no"], self.vocab.digest, run_id,
                     ev.role_name, ev.role_kind, ev.role_phrase,
                     ev.role_phrase_start, ev.raw_date, ev.normalized_date,
                     int(ev.had_year), int(ev.is_range), ev.range_connector,
                     ev.endpoint, ev.round_no, ev.round_phrase,
                     ev.paragraph_index, ev.paragraph_start,
                     ev.sentence_start, ev.sentence_end, ev.date_start,
                     ev.date_end, ev.sentence_text))
            for item in pendings:
                pid = self._pending_id(version, item)
                self._upsert_pending(conn, pid, version, item, run_id, at)
            for row in ledger_rows:
                if not self.store.ledger_exists(row):
                    conn.execute(
                        "INSERT INTO node_ledger(project_key, round_no, "
                        "role_name, action, normalized_date, source_id, "
                        "notice_key, archive_id, version_no, issued_on, "
                        "config_digest, run_id, evidence_json, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row["project_key"], row["round_no"],
                         row["role_name"], row["action"],
                         row["normalized_date"], row["source_id"],
                         row["notice_key"], row["archive_id"],
                         row["version_no"], row["issued_on"],
                         row["config_digest"], run_id,
                         json.dumps(row["evidence"], ensure_ascii=False,
                                    default=str), at))
                    summary["ledger_rows_added"] += 1
            if self._fail_point == (version["archive_id"], version["version_no"]):
                raise ScheduleInterrupted(
                    f"interrupted inside version transaction {key3}")
        self.progress(
            f"  {version['notice_key']} v{version['version_no']}: "
            f"{extraction.notice_type}, {len(extraction.dates)} date "
            f"mention(s), {len(pendings)} pending, {len(ledger_rows)} "
            f"ledger row(s) [{raw_status}]")
        return len(ledger_rows), 0

    # ------------------------------------------------------------------
    def _project_key(self, version: dict) -> str:
        return f"{version['source_id']}::{version['notice_key']}"

    def _pending_id(self, version: dict, item: PendingItem) -> str:
        # Stable across vocabulary versions: the same sentence/concern in the
        # same archived version keeps one pending_id, so a later vocabulary
        # that resolves it closes the item while old rows remain in history.
        basis = "|".join((
            version["source_id"], version["archive_id"],
            str(version["version_no"]), item.code, item.detail,
            str(item.sentence_start), str(item.sentence_end),
            item.target_key or "", item.role_name or "",
            item.normalized_date or ""))
        return "pend_" + hashlib.sha256(basis.encode()).hexdigest()[:24]

    def _upsert_pending(self, conn, pid: str, version: dict,
                        item: PendingItem, run_id: int, at: str) -> None:
        payload = (
            pid, version["source_id"], version["notice_key"],
            version["archive_id"], version["version_no"], item.code,
            item.detail, item.target_source, item.target_key, item.role_name,
            item.raw_date, item.normalized_date, item.paragraph_index,
            item.sentence_start, item.sentence_end, item.sentence_text,
            self.vocab.digest, run_id, at)
        conn.execute(
            "INSERT INTO pending_current(pending_id, source_id, notice_key, "
            "archive_id, version_no, code, detail, target_source, target_key, "
            "role_name, raw_date, normalized_date, paragraph_index, "
            "sentence_start, sentence_end, sentence_text, config_digest, "
            "run_id, updated_at) VALUES (" + ",".join("?" * 19) + ") "
            "ON CONFLICT(pending_id) DO UPDATE SET detail=excluded.detail, "
            "target_source=excluded.target_source, target_key=excluded.target_key, "
            "role_name=excluded.role_name, raw_date=excluded.raw_date, "
            "normalized_date=excluded.normalized_date, "
            "config_digest=excluded.config_digest, run_id=excluded.run_id, "
            "updated_at=excluded.updated_at", payload)
        conn.execute(
            "INSERT INTO pending_history(pending_id, source_id, notice_key, "
            "archive_id, version_no, code, detail, target_source, target_key, "
            "role_name, raw_date, normalized_date, paragraph_index, "
            "sentence_start, sentence_end, sentence_text, config_digest, "
            "run_id, recorded_at) VALUES (" + ",".join("?" * 19) + ")",
            (*payload[:17], run_id, at))

    # ------------------------------------------------------------------
    def _resolve_target(self, version: dict, extraction: NoticeExtraction
                        ) -> tuple[dict | None, PendingItem | None]:
        """Resolve a cross-notice reference to exactly one archive identity."""
        explicit = [h for h in extraction.target_hints
                    if h["kind"] in ("explicit", "implicit_key")]
        own_source = version["source_id"]
        for hint in explicit:
            target_source = hint.get("target_source") or own_source
            target_key = hint["target_key"]
            matches = self.pipeline.identities_for_notice(
                target_source, target_key)
            if len(matches) == 1:
                return {"source_id": target_source,
                        "archive_id": matches[0]["archive_id"],
                        "notice_key": target_key}, None
            code = ("target_not_found" if not matches
                    else "target_ambiguous")
            return None, PendingItem(
                code=code,
                detail=(f"reference {hint['phrase']!r} resolved to "
                        f"{len(matches)} notice(s); need a unique target"),
                paragraph_index=0, paragraph_start=0,
                sentence_start=0, sentence_end=0,
                sentence_text=hint["phrase"],
                target_source=target_source, target_key=target_key)
        if extraction.project_code:
            matches = self.pipeline.notices_for_project_code(
                extraction.project_code)
            roots = {(m["source_id"], m["archive_id"]) for m in matches}
            if len(roots) == 1:
                only = matches[0]
                return {"source_id": only["source_id"],
                        "archive_id": only["archive_id"],
                        "notice_key": only["notice_key"]}, None
            return None, PendingItem(
                code=("target_not_found" if not roots
                      else "target_ambiguous"),
                detail=(f"project code {extraction.project_code!r} matched "
                        f"{len(roots)} notice(s)"),
                paragraph_index=0, paragraph_start=0,
                sentence_start=0, sentence_end=0,
                sentence_text=extraction.project_code)
        return None, PendingItem(
            code="target_unspecified",
            detail="no resolvable notice number or project code reference",
            paragraph_index=0, paragraph_start=0,
            sentence_start=0, sentence_end=0,
            sentence_text=version["title"])

    def _plan_effects(self, version: dict, extraction: NoticeExtraction,
                      project_roots: dict[str, dict]
                      ) -> tuple[list[dict], list[PendingItem]]:
        rows: list[dict] = []
        pendings: list[PendingItem] = list(extraction.pending)
        ntype = extraction.notice_type

        if ntype == "application":
            project_key = self._project_key(version)
            project_roots.setdefault(project_key, {
                "source_id": version["source_id"],
                "root_notice_key": version["notice_key"],
                "project_code": extraction.project_code})
            rows += self._set_rows(version, extraction, project_key,
                                   extraction.dates)
            rows += self._cancel_rows(version, extraction, project_key)
            return rows, pendings

        # supplement / correction / withdrawal need a unique target
        target, target_pending = self._resolve_target(version, extraction)
        if target is None:
            pendings.append(target_pending)
            # A non-application notice that resolves to nothing and quotes no
            # concrete date at all is still material a human must review.
            if not extraction.dates and not any(
                    p.code == "vague_statement" for p in pendings):
                pendings.append(PendingItem(
                    code="vague_statement",
                    detail=("non-application notice without a concrete date "
                            "or a resolvable target"),
                    paragraph_index=0, paragraph_start=0,
                    sentence_start=0, sentence_end=len(version["body"]),
                    sentence_text=version["title"]))
            return rows, pendings
        project_key = f"{target['source_id']}::{target['notice_key']}"

        if ntype == "withdrawal":
            rows += self._cancel_rows(version, extraction, project_key)
            return rows, pendings

        # supplement / correction carry dates
        target_roles = self._target_roles(project_key)
        accepted: list[DateEvidence] = []
        for ev in extraction.dates:
            if ntype == "correction" and ev.role_name not in target_roles:
                pendings.append(PendingItem(
                    code="role_mismatch",
                    detail=(f"correction targets role {ev.role_name!r} which "
                            "the referenced notice never scheduled"),
                    paragraph_index=ev.paragraph_index,
                    paragraph_start=ev.paragraph_start,
                    sentence_start=ev.sentence_start,
                    sentence_end=ev.sentence_end,
                    sentence_text=ev.sentence_text,
                    target_source=target["source_id"],
                    target_key=target["notice_key"], role_name=ev.role_name,
                    raw_date=ev.raw_date, normalized_date=ev.normalized_date,
                    date_start=ev.date_start, date_end=ev.date_end))
                continue
            accepted.append(ev)
        rows += self._set_rows(version, extraction, project_key, accepted)
        return rows, pendings

    def _target_roles(self, project_key: str) -> set[str]:
        return {r["role_name"] for r in self.store.ledger(project_key)
                if r["action"] == "set"}

    def _set_rows(self, version: dict, extraction: NoticeExtraction,
                  project_key: str, evidence_list: list[DateEvidence]
                  ) -> list[dict]:
        rows: list[dict] = []
        for ev in evidence_list:
            rows.append({
                "project_key": project_key, "round_no": ev.round_no,
                "role_name": ev.role_name, "action": "set",
                "normalized_date": ev.normalized_date,
                "source_id": version["source_id"],
                "notice_key": version["notice_key"],
                "archive_id": version["archive_id"],
                "version_no": version["version_no"],
                "issued_on": version["issued_on"],
                "config_digest": self.vocab.digest,
                "evidence": asdict(ev)})
        return rows

    def _cancel_rows(self, version: dict, extraction: NoticeExtraction,
                     project_key: str) -> list[dict]:
        rows: list[dict] = []
        review_roles = {spec.name for spec in self.vocab.roles
                        if "review" in spec.name}
        for item in extraction.cancels_node:
            target_roles = [r for r in self._roles_at_round(
                project_key, item["round_no"]) if r in review_roles]
            for role in target_roles:
                rows.append(self._cancel_row(
                    version, project_key, item["round_no"], role,
                    "cancel_node", item))
        for item in extraction.cancels_round:
            rows.append(self._cancel_row(
                version, project_key, item["round_no"], "",
                "cancel_round", item))
        return rows

    def _roles_at_round(self, project_key: str, round_no: int) -> list[str]:
        return sorted({r["role_name"] for r in self.store.ledger(project_key)
                       if r["round_no"] == round_no and r["action"] == "set"})

    def _cancel_row(self, version: dict, project_key: str, round_no: int,
                    role_name: str, action: str, item: dict) -> dict:
        return {
            "project_key": project_key, "round_no": round_no,
            "role_name": role_name, "action": action,
            "normalized_date": None,
            "source_id": version["source_id"],
            "notice_key": version["notice_key"],
            "archive_id": version["archive_id"],
            "version_no": version["version_no"],
            "issued_on": version["issued_on"],
            "config_digest": self.vocab.digest,
            "evidence": item}

    # ------------------------------------------------------------------
    def _pending_resolved(self, conn, pending_row: dict) -> bool:
        """Whether an older pending item has since been resolved.

        An ``unmapped_date`` item is closed when the same archived version,
        re-extracted under a newer vocabulary, now yields a date mention
        inside the same sentence.  Other pending reasons (ambiguous target,
        role mismatch, vague wording) stay open until a human acts.
        """
        if pending_row["code"] != "unmapped_date":
            return False
        newer = conn.execute(
            "SELECT 1 FROM date_evidence WHERE source_id=? AND archive_id=? "
            "AND version_no=? AND sentence_start=? AND sentence_end=? "
            "AND config_digest <> ? LIMIT 1",
            (pending_row["source_id"], pending_row["archive_id"],
             pending_row["version_no"], pending_row["sentence_start"],
             pending_row["sentence_end"],
             pending_row["config_digest"])).fetchone()
        return newer is not None

    def _publish_snapshot(self, run_id: int, at: str, summary: dict) -> None:
        """Recompute the queryable snapshot from the whole ledger."""
        with self.store.tx() as conn:
            conn.execute("DELETE FROM current_nodes")
            conn.execute("DELETE FROM round_schedules")
            conn.execute("DELETE FROM project_meta")
            # projects first
            keys = sorted({r["project_key"] for r in conn.execute(
                "SELECT DISTINCT project_key FROM node_ledger")})
            for key in keys:
                first = conn.execute(
                    "SELECT source_id, notice_key FROM node_ledger "
                    "WHERE project_key=? ORDER BY ledger_id LIMIT 1",
                    (key,)).fetchone()
                code_row = conn.execute(
                    "SELECT project_code FROM extraction_versions WHERE "
                    "source_id=? AND notice_key=? AND project_code IS NOT NULL "
                    "LIMIT 1",
                    (first["source_id"], first["notice_key"])).fetchone()
                conn.execute(
                    "INSERT INTO project_meta(project_key, source_id, "
                    "root_notice_key, project_code) VALUES (?,?,?,?)",
                    (key, first["source_id"], first["notice_key"],
                     code_row["project_code"] if code_row else None))
            # replay ledger chronologically
            for row in conn.execute("SELECT * FROM node_ledger ORDER BY ledger_id"):
                row = dict(row)
                if row["action"] == "cancel_round":
                    conn.execute(
                        "INSERT INTO round_schedules(project_key, round_no, "
                        "status, source_archive_id, source_notice_key) "
                        "VALUES (?,?, 'round_cancelled', ?, ?) "
                        "ON CONFLICT(project_key, round_no) DO UPDATE SET "
                        "status='round_cancelled', source_archive_id=excluded."
                        "source_archive_id, source_notice_key=excluded."
                        "source_notice_key",
                        (row["project_key"], row["round_no"],
                         row["archive_id"], row["notice_key"]))
                    continue
                conn.execute(
                    "INSERT INTO round_schedules(project_key, round_no, "
                    "status, source_archive_id, source_notice_key) "
                    "VALUES (?, ?, 'open', ?, ?) "
                    "ON CONFLICT(project_key, round_no) DO NOTHING",
                    (row["project_key"], row["round_no"], row["archive_id"],
                     row["notice_key"]))
                status = "cancelled" if row["action"] == "cancel_node" else "active"
                conn.execute(
                    "INSERT INTO current_nodes(project_key, round_no, "
                    "role_name, status, normalized_date, ledger_id) "
                    "VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(project_key, round_no, role_name) DO UPDATE "
                    "SET status=excluded.status, normalized_date=excluded."
                    "normalized_date, ledger_id=excluded.ledger_id",
                    (row["project_key"], row["round_no"], row["role_name"],
                     status, row["normalized_date"], row["ledger_id"]))
            # pending snapshot: the newest judgment per pending_id wins, so a
            # vocabulary change that books the previously unmapped date also
            # closes the old pending item (it stays in pending_history).
            # The filter is NOT limited to the current run — an idempotent
            # re-organize that adds no new judgments must not clear items.
            conn.execute("DELETE FROM pending_current")
            latest_rows = [dict(r) for r in conn.execute(
                "SELECT h.* FROM pending_history h JOIN ("
                "  SELECT pending_id, MAX(hist_id) AS max_hist "
                "  FROM pending_history GROUP BY pending_id"
                ") t ON h.hist_id = t.max_hist")]
            for row in latest_rows:
                if self._pending_resolved(conn, row):
                    continue
                conn.execute(
                    "INSERT INTO pending_current SELECT "
                    "pending_id, source_id, notice_key, archive_id, version_no, "
                    "code, detail, target_source, target_key, role_name, "
                    "raw_date, normalized_date, paragraph_index, sentence_start, "
                    "sentence_end, sentence_text, config_digest, run_id, ? "
                    "FROM pending_history WHERE hist_id=?",
                    (at, row["hist_id"]))
            summary["pending_open"] = conn.execute(
                "SELECT COUNT(*) FROM pending_current").fetchone()[0]

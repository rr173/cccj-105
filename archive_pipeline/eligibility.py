"""Offline eligibility pre-screen job: clause × applications → auditable results.

Independent from web scanning, funding and schedule jobs.  It consumes

* exactly one manually authored clause file (validated *before* any application
  is read; see :mod:`eligclause`),
* the identity-schema pipeline database opened read-only, only to verify the
  clause's ``source_id`` / ``archive_id`` / ``version_no`` binding and the
  cited body slices, and
* one JSONL application file, processed **line by line** so memory holds one
  application at a time regardless of batch size.

Results live in a separate ``work/eligibility.sqlite3``.  The input order is
preserved by the ``seq`` column; every application stores the complete
explanation tree.  A successful run records a three-way summary.  A bad JSON
line, a duplicate ``application_id`` or damaged member structure aborts the
file with a line number: the run is marked **incomplete**, keeps the rows
already written, and stores **no** success summary.  That is a different
failure class from a single application field being missing/null/wrong-typed,
which is a per-application *unknown* value with its own recorded reason.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .eligclause import Clause, load_clause
from .eligengine import (
    FALSE,
    TRUE,
    UNKNOWN,
    VERDICT_LABELS,
    ApplicationDocument,
    ApplicationStructureError,
    Evaluator,
    now_iso,
    validate_application,
)

ROOT = Path(__file__).resolve().parent.parent


class EligibilityError(RuntimeError):
    """Operational failure: unreadable input, unwritable results…"""


class InputError(RuntimeError):
    """A structural failure of the application file at an exact line."""

    def __init__(self, code: str, detail: str, line_no: int | None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.line_no = line_no

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "line_no": self.line_no}


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    clause_path         TEXT NOT NULL,
    clause_file_sha256  TEXT NOT NULL,
    clause_digest       TEXT NOT NULL,
    clause_summary_json TEXT NOT NULL,
    input_path          TEXT NOT NULL,
    input_sha256        TEXT,
    input_bytes         INTEGER,
    input_lines         INTEGER,
    blank_lines         INTEGER,
    applications_written INTEGER NOT NULL DEFAULT 0,
    summary_json        TEXT,
    error_json          TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    run_id           INTEGER NOT NULL,
    seq              INTEGER NOT NULL,
    application_id   TEXT NOT NULL,
    line_no          INTEGER NOT NULL,
    verdict          TEXT NOT NULL,
    verdict_label    TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    UNIQUE (run_id, application_id)
);

CREATE TABLE IF NOT EXISTS input_errors (
    error_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL,
    line_no    INTEGER,
    code       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applications_id
    ON applications(application_id, run_id);
"""


def scan_input(path: Path) -> dict:
    """One streaming pass: SHA-256, byte size and line/blank-line counts.

    Nothing is retained per line, so this is safe for files of any length.
    """
    hasher = hashlib.sha256()
    size = 0
    total = 0
    blank = 0
    with open(path, "rb") as handle:
        for raw in handle:
            hasher.update(raw)
            size += len(raw)
            total += 1
            if not raw.strip():
                blank += 1
    return {"sha256": hasher.hexdigest(), "bytes": size, "lines": total,
            "blank_lines": blank}


@dataclass
class RunOutcome:
    run_id: int
    status: str
    summary: dict | None
    error: dict | None
    applications_written: int


class EligibilityStore:
    """The job's own database; the pipeline database is never written."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA synchronous = NORMAL")

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
                "UPDATE runs SET status='incomplete', finished_at=?, "
                "error_json=COALESCE(error_json, ?) WHERE status='running'",
                (at, json.dumps({"code": "process_interrupted",
                                 "detail": "the process died mid-run; rows written "
                                           "before the interruption remain but the "
                                           "run is incomplete"}, ensure_ascii=False)))
            return cur.rowcount

    def create_run(self, clause: Clause, input_path: Path,
                   input_summary: dict, started_at: str) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO runs(started_at, status, clause_path, "
                "clause_file_sha256, clause_digest, clause_summary_json, "
                "input_path, input_sha256, input_bytes, input_lines, blank_lines) "
                "VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (started_at, str(clause.path), clause.file_sha256, clause.digest,
                 json.dumps(clause.summary(), ensure_ascii=False),
                 str(input_path), input_summary["sha256"], input_summary["bytes"],
                 input_summary["lines"], input_summary["blank_lines"]))
            return int(cur.lastrowid)

    def finish_completed(self, run_id: int, summary: dict, written: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status='completed', "
                "applications_written=?, summary_json=? WHERE run_id=?",
                (now_iso(), written,
                 json.dumps(summary, ensure_ascii=False), run_id))

    def finish_incomplete(self, run_id: int, error: dict, written: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status='incomplete', "
                "applications_written=?, summary_json=NULL, error_json=? "
                "WHERE run_id=?",
                (now_iso(), written,
                 json.dumps(error, ensure_ascii=False), run_id))
            conn.execute(
                "INSERT INTO input_errors(run_id, line_no, code, detail, "
                "recorded_at) VALUES (?,?,?,?,?)",
                (run_id, error.get("line_no"), error["code"], error["detail"],
                 now_iso()))

    def finish_failed(self, run_id: int, error: dict, written: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status='failed', "
                "applications_written=?, summary_json=NULL, error_json=? "
                "WHERE run_id=?",
                (now_iso(), written,
                 json.dumps(error, ensure_ascii=False), run_id))

    def run(self, run_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_run(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def runs(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM runs ORDER BY run_id")]

    def run_counts(self, run_id: int) -> dict[str, int]:
        counts = {TRUE: 0, FALSE: 0, UNKNOWN: 0}
        for row in self.conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM applications "
                "WHERE run_id=? GROUP BY verdict", (run_id,)):
            counts[row["verdict"]] = int(row["n"])
        return counts

    # -- applications ----------------------------------------------------
    def add_application(self, run_id: int, seq: int,
                        application: ApplicationDocument, result: dict,
                        conn=None) -> None:
        """Insert one judged application (inside an open tx when given)."""
        explanation = json.dumps(result, ensure_ascii=False, default=str)
        sql = ("INSERT INTO applications(run_id, seq, application_id, line_no, "
               "verdict, verdict_label, explanation_json) VALUES (?,?,?,?,?,?,?)")
        params = (run_id, seq, application.application_id, application.line_no,
                  result["verdict"], result["verdict_label"], explanation)
        if conn is not None:
            conn.execute(sql, params)
            return
        with self.tx() as own:
            own.execute(sql, params)

    def add_applications_batch(self, run_id: int,
                               rows: list[tuple[int, ApplicationDocument, dict]]) -> None:
        """Commit a prefix of judged applications in one transaction.

        ``rows`` holds ``(seq, application, result)`` tuples.  A batch is only
        flushed after every record in it has been judged, so a structural
        failure still leaves exactly the successfully judged prefix on disk.
        """
        if not rows:
            return
        with self.tx() as conn:
            for seq, application, result in rows:
                self.add_application(run_id, seq, application, result,
                                     conn=conn)

    def application_id_exists(self, run_id: int, application_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM applications WHERE run_id=? AND application_id=?",
            (run_id, application_id)).fetchone() is not None

    def application_by_id(self, application_id: str,
                          run_id: int | None = None) -> dict | None:
        if run_id is None:
            row = self.conn.execute(
                "SELECT a.*, r.status AS run_status FROM applications a "
                "JOIN runs r ON r.run_id=a.run_id "
                "WHERE a.application_id=? ORDER BY a.run_id DESC LIMIT 1",
                (application_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT a.*, r.status AS run_status FROM applications a "
                "JOIN runs r ON r.run_id=a.run_id "
                "WHERE a.application_id=? AND a.run_id=?",
                (application_id, run_id)).fetchone()
        return dict(row) if row else None

    def applications(self, run_id: int) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT run_id, seq, application_id, line_no, verdict, verdict_label "
            "FROM applications WHERE run_id=? ORDER BY seq", (run_id,))]

    def input_errors(self, run_id: int | None = None) -> list[dict]:
        if run_id is None:
            rows = self.conn.execute(
                "SELECT * FROM input_errors ORDER BY error_id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM input_errors WHERE run_id=? ORDER BY error_id",
                (run_id,))
        return [dict(row) for row in rows]


class EligibilityJob:
    """Validates one clause, then pre-screens one JSONL file line by line."""

    def __init__(self, workdir: str | Path, clause_path: str | Path,
                 pipeline_db: str | Path | None = None, progress=None):
        self.workdir = Path(workdir)
        self.pipeline_db = (Path(pipeline_db) if pipeline_db
                            else self.workdir / "pipeline.sqlite3")
        # Clause validation happens *now*, before any application is touched.
        self.clause = load_clause(self.pipeline_db, clause_path)
        self.store = EligibilityStore(self.workdir / "eligibility.sqlite3")
        self.progress = progress or (lambda _message: None)

    def close(self) -> None:
        self.store.close()

    def run(self, applications_path: str | Path,
            echo=None) -> RunOutcome:
        applications_path = Path(applications_path)
        if not applications_path.exists():
            raise EligibilityError(
                f"application file not found: {applications_path}")
        started = now_iso()
        stale = self.store.mark_interrupted_runs(started)
        if stale:
            self.progress(f"marked {stale} stale running run(s) as incomplete")
        try:
            input_summary = scan_input(applications_path)
        except OSError as exc:
            raise EligibilityError(
                f"cannot read application file {applications_path}: {exc}") from exc
        run_id = self.store.create_run(self.clause, applications_path,
                                       input_summary, started)
        evaluator = Evaluator(self.clause)
        counts = {TRUE: 0, FALSE: 0, UNKNOWN: 0}
        written = 0
        seq = 0
        pending: list[tuple[int, ApplicationDocument, dict]] = []
        try:
            with open(applications_path, "rb") as handle:
                for line_no, raw_line in enumerate(handle, 1):
                    try:
                        text = raw_line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise InputError(
                            "invalid_utf8",
                            f"line {line_no}: not valid UTF-8 ({exc})", line_no)
                    if not text.strip():
                        continue
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise InputError(
                            "bad_json",
                            f"line {line_no}: invalid JSON ({exc.msg} at column "
                            f"{exc.colno})", line_no)
                    try:
                        application = validate_application(record, line_no)
                    except ApplicationStructureError as exc:
                        raise InputError(exc.code, exc.detail, line_no)
                    # Duplicate detection uses only the indexed result table
                    # plus the small (<=200) in-flight batch — application
                    # documents themselves are never accumulated.
                    if (self.store.application_id_exists(
                            run_id, application.application_id)
                            or any(app.application_id == application.application_id
                                   for _seq, app, _result in pending)):
                        raise InputError(
                            "duplicate_application_id",
                            f"line {line_no}: application_id "
                            f"{application.application_id!r} already appeared "
                            "earlier in this file", line_no)
                    result = evaluator.evaluate(application)
                    pending.append((seq, application, result))
                    counts[result["verdict"]] += 1
                    written += 1
                    if len(pending) >= 200:
                        self.store.add_applications_batch(run_id, pending)
                        pending = []
                    if echo:
                        echo({
                            "seq": seq,
                            "line_no": line_no,
                            "application_id": application.application_id,
                            "verdict": result["verdict"],
                            "verdict_label": result["verdict_label"],
                            "root_condition": result["root_condition"],
                            "summary_reason": result["explanation"]
                            .get("tree", {}).get("reason"),
                        })
                    seq += 1
                self.store.add_applications_batch(run_id, pending)
                pending = []
        except InputError as exc:
            # Anything still batched belongs to *judged* lines only; flush it
            # before closing the run so the on-disk prefix is exact.
            try:
                self.store.add_applications_batch(run_id, pending)
            except Exception:  # pragma: no cover - secondary failure
                pass
            error = exc.as_dict()
            self.store.finish_incomplete(run_id, error, written)
            self.progress(
                f"INCOMPLETE run {run_id}: {exc.code} at line {exc.line_no}; "
                f"{written} application(s) judged; no success summary")
            return RunOutcome(run_id, "incomplete", None, error, written)
        except EligibilityError:
            raise
        except Exception as exc:  # unexpected: keep the file, mark failed
            try:
                self.store.add_applications_batch(run_id, pending)
            except Exception:  # pragma: no cover - secondary failure
                pass
            error = {"code": "internal_error", "detail": repr(exc),
                     "line_no": None}
            self.store.finish_failed(run_id, error, written)
            self.progress(f"FAILED run {run_id}: {exc!r}")
            return RunOutcome(run_id, "failed", None, error, written)

        summary = {
            "run_id": run_id,
            "clause_digest": self.clause.digest,
            "clause_file_sha256": self.clause.file_sha256,
            "clause_binding": {
                "source_id": self.clause.binding.source_id,
                "archive_id": self.clause.binding.archive_id,
                "version_no": self.clause.binding.version_no,
                "notice_key": self.clause.binding.notice_key,
            },
            "input": {
                "path": str(applications_path),
                "sha256": input_summary["sha256"],
                "bytes": input_summary["bytes"],
                "lines": input_summary["lines"],
                "blank_lines": input_summary["blank_lines"],
            },
            "applications_total": written,
            "counts": {
                "satisfied": counts[TRUE],
                "not_satisfied": counts[FALSE],
                "needs_evidence": counts[UNKNOWN],
            },
            "labels": {"satisfied": VERDICT_LABELS[TRUE],
                       "not_satisfied": VERDICT_LABELS[FALSE],
                       "needs_evidence": VERDICT_LABELS[UNKNOWN]},
            "ordered_application_ids": [row["application_id"]
                                        for row in self.store.applications(run_id)],
        }
        self.store.finish_completed(run_id, summary, written)
        self.progress(
            f"completed run {run_id}: {written} application(s) — "
            f"{summary['counts']}")
        return RunOutcome(run_id, "completed", summary, None, written)


def explain_application(workdir: str | Path, application_id: str,
                        run_id: int | None = None) -> dict | None:
    """Reopen the result database and return one stored full explanation."""
    db = Path(workdir) / "eligibility.sqlite3"
    if not db.exists():
        raise EligibilityError(f"no eligibility database: {db}")
    store = EligibilityStore(db)
    try:
        row = store.application_by_id(application_id, run_id)
        if row is None:
            return None
        run = store.run(row["run_id"])
        explanation = json.loads(row.pop("explanation_json"))
        out = {
            "run_id": row["run_id"],
            "run_status": row["run_status"],
            "seq": row["seq"],
            "line_no": row["line_no"],
            "application_id": row["application_id"],
            "verdict": row["verdict"],
            "verdict_label": row["verdict_label"],
            "clause_digest": run["clause_digest"] if run else None,
            "clause_file_sha256": run["clause_file_sha256"] if run else None,
            "clause_summary": (json.loads(run["clause_summary_json"])
                               if run else None),
            "input": (json.loads(run["summary_json"])["input"]
                      if run and run["summary_json"] else None),
            "explanation": explanation,
        }
        return out
    finally:
        store.close()

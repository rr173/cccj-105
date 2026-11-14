"""Strict streaming loaders for the fact inputs of the relation job.

Two kinds of input exist:

* the reserved ``notice`` relation, read **read-only** from the archive
  pipeline SQLite database inside one read transaction, so every version row
  in a run comes from the same point in time (a concurrent collector inserting
  a version cannot leak half of a newer state into the run);
* ordinary base relations, read from local JSONL files, one line at a time.

A JSONL record must be an object with exactly the declared columns.  A missing
column, an extra column, ``null``, a non-string for a string column and a
non-integer (including a boolean) for an int column are each rejected with the
file name, the 1-based line number and the offending column.  A syntactic
failure is fatal; the caller aborts the whole job (it does).

Facts are a **set**: every tuple is written with ``INSERT OR IGNORE`` into the
relational fact table and duplicates on disk collapse.  The loaders therefore
never hold the whole relation in memory; they stream.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .relrule import (
    FACT_PAD,
    MAX_COLUMNS,
    NOTICE_COLUMNS,
    NOTICE_RELATION,
    Column,
    Relation,
)

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

_SLOTS = ", ".join(f"c{i}" for i in range(MAX_COLUMNS))
_PLACEHOLDERS = ",".join("?" for _ in range(MAX_COLUMNS + 2))
_INSERT_SQL = (f"INSERT OR IGNORE INTO facts(run_id, relation, {_SLOTS}) "
               f"VALUES ({_PLACEHOLDERS})")


class FactError(RuntimeError):
    """A structural fact failure at an exact file and line."""

    def __init__(self, code: str, detail: str, *, path: str = "",
                 line_no: int | None = None, column: str | None = None,
                 relation: str | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.path = path
        self.line_no = line_no
        self.column = column
        self.relation = relation

    def as_dict(self) -> dict:
        out = {"code": self.code, "detail": self.detail, "path": self.path,
               "line_no": self.line_no, "relation": self.relation}
        if self.column is not None:
            out["column"] = self.column
        return out


def encode(value) -> str:
    """Persist a typed value; strings and ints never share a representation."""
    return json.dumps(value, ensure_ascii=False)


def tuple_params(run_id: int, relation: str, values: list[str]) -> tuple:
    padded = list(values) + [FACT_PAD] * (MAX_COLUMNS - len(values))
    return (run_id, relation, *padded)


def count_changes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT changes()").fetchone()
    return int(row[0])


@dataclass
class Digest:
    path: str
    sha256: str
    bytes: int
    lines: int
    records: int
    duplicate_records: int
    distinct: int
    columns: list[list[str]]
    relation: str
    kind: str = "jsonl"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "relation": self.relation,
                "path": self.path, "sha256": self.sha256,
                "bytes": self.bytes, "lines": self.lines,
                "records": self.records,
                "duplicate_records": self.duplicate_records,
                "distinct": self.distinct,
                "columns": self.columns}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


# ---------------------------------------------------------------------------
# JSONL loader
# ---------------------------------------------------------------------------


def load_jsonl_relation(
    path: str | Path,
    relation: Relation,
    conn: sqlite3.Connection,
    run_id: int,
) -> Digest:
    """Stream one JSONL base relation into the fact table.

    ``conn`` must be inside an open transaction.  The function only inserts;
    on the first bad line it raises :class:`FactError` and the caller rolls
    the transaction back, so a partially loaded relation is never committed.
    """
    path = Path(path)
    columns: tuple[Column, ...] = relation.columns
    index_of = {column.name: i for i, column in enumerate(columns)}
    hasher = hashlib.sha256()
    size = 0
    line_no = 0
    records = 0
    duplicates = 0
    inserted = 0
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise FactError("unreadable_input",
                        f"cannot read {relation.name} input {path}: {exc}",
                        path=str(path), relation=relation.name) from exc
    with handle:
        for raw in handle:
            hasher.update(raw)
            size += len(raw)
            line_no += 1
            if not raw.strip():
                raise FactError("blank_line",
                                f"{path}:{line_no}: blank lines are not facts "
                                "and are rejected (every line must be a record)",
                                path=str(path), line_no=line_no,
                                relation=relation.name)
            try:
                record = json.loads(raw.decode("utf-8"),
                                    object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise FactError("bad_json",
                                f"{path}:{line_no}: not valid UTF-8 JSON: {exc}",
                                path=str(path), line_no=line_no,
                                relation=relation.name) from exc
            if not isinstance(record, dict):
                raise FactError("not_object",
                                f"{path}:{line_no}: record must be a JSON object",
                                path=str(path), line_no=line_no,
                                relation=relation.name)
            missing = [c.name for c in columns if c.name not in record]
            extra = sorted(set(record) - set(index_of))
            if missing:
                raise FactError(
                    "missing_column",
                    f"{path}:{line_no}: missing column(s) {missing} for relation "
                    f"{relation.name!r}", path=str(path), line_no=line_no,
                    column=missing[0], relation=relation.name)
            if extra:
                raise FactError(
                    "extra_column",
                    f"{path}:{line_no}: unknown column(s) {extra} for relation "
                    f"{relation.name!r}", path=str(path), line_no=line_no,
                    column=extra[0], relation=relation.name)
            values: list[str] = []
            for column in columns:
                value = record[column.name]
                if value is None:
                    raise FactError(
                        "null_value",
                        f"{path}:{line_no}: column {column.name!r} is null; "
                        "nulls are rejected rather than coerced",
                        path=str(path), line_no=line_no, column=column.name,
                        relation=relation.name)
                if column.type == "string":
                    if not isinstance(value, str):
                        raise FactError(
                            "type_mismatch",
                            f"{path}:{line_no}: column {column.name!r} must be a "
                            f"string, got {type(value).__name__}",
                            path=str(path), line_no=line_no,
                            column=column.name, relation=relation.name)
                else:  # int
                    if type(value) is not int:
                        raise FactError(
                            "type_mismatch",
                            f"{path}:{line_no}: column {column.name!r} must be a "
                            f"signed 64-bit integer, got {type(value).__name__}",
                            path=str(path), line_no=line_no,
                            column=column.name, relation=relation.name)
                    if not (INT64_MIN <= value <= INT64_MAX):
                        raise FactError(
                            "int_out_of_range",
                            f"{path}:{line_no}: column {column.name!r} is outside "
                            "the signed 64-bit range", path=str(path),
                            line_no=line_no, column=column.name,
                            relation=relation.name)
                values.append(encode(value))
            records += 1
            conn.execute(_INSERT_SQL,
                         tuple_params(run_id, relation.name, values))
            if count_changes(conn):
                inserted += 1
            else:
                duplicates += 1
    return Digest(path=str(path), sha256=hasher.hexdigest(), bytes=size,
                  lines=line_no, records=records, duplicate_records=duplicates,
                  distinct=inserted,
                  columns=[[c.name, c.type] for c in columns],
                  relation=relation.name, kind="jsonl")


# ---------------------------------------------------------------------------
# Notice loader (archive pipeline DB, read-only, single snapshot)
# ---------------------------------------------------------------------------


@dataclass
class NoticeView:
    """The notice relation as one consistent read snapshot."""

    db_path: str
    db_sha256_before: str
    db_sha256_after: str
    records: int
    duplicates: int
    distinct: int
    sources: list[str]

    @property
    def consistent(self) -> bool:
        return self.db_sha256_before == self.db_sha256_after

    def as_dict(self) -> dict:
        return {"kind": "pipeline-sqlite", "relation": NOTICE_RELATION,
                "path": self.db_path, "sha256": self.db_sha256_before,
                "sha256_after": self.db_sha256_after,
                "consistent": self.consistent,
                "records": self.records,
                "duplicate_records": self.duplicates,
                "distinct": self.distinct,
                "sources": self.sources,
                "columns": [[n, t] for n, t in NOTICE_COLUMNS],
                "identity": ["source_id", "archive_id", "version_no"]}


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_notice_relation(db_path: str | Path, conn: sqlite3.Connection,
                         run_id: int) -> NoticeView:
    """Copy every archived notice version into the fact table.

    The archive database is copied to a private temporary file first (the
    SQLite backup API reads it in one transaction), and the notice rows are
    then read from that frozen copy.  A collector that commits a new version
    while the job runs can therefore never leak a row into this run: the
    snapshot is a file-level copy, not merely a "read the same rows" hope.
    The source file is never opened for writing.  Before/after hashes of the
    source file are recorded so the snapshot claim is checkable.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FactError("missing_pipeline_db",
                        f"archive pipeline database not found: {db_path}",
                        path=str(db_path), relation=NOTICE_RELATION)
    digest_before = _sha256_file(db_path)
    snapshot_dir = Path(tempfile.mkdtemp(prefix="rel-notice-"))
    snapshot = snapshot_dir / "pipeline.sqlite3"
    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        source = sqlite3.connect(uri, uri=True, isolation_level=None,
                                 timeout=30)
        try:
            # The read transaction pins the source to one snapshot; the copy
            # is made from that snapshot, so a concurrent writer's commits
            # are invisible to this backup even if it runs for a while.
            source.execute("BEGIN")
            try:
                source.backup(sqlite3.connect(str(snapshot),
                                              isolation_level=None))
            finally:
                source.execute("COMMIT")
        finally:
            source.close()
        frozen = sqlite3.connect(str(snapshot), isolation_level=None)
        frozen.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise FactError("pipeline_db_unreadable",
                        f"cannot snapshot {db_path} read-only: {exc}",
                        path=str(db_path), relation=NOTICE_RELATION) from exc
    records = duplicates = inserted = 0
    sources: list[str] = []
    try:
        columns = {row[1] for row in frozen.execute(
            "PRAGMA table_info(versions)")}
        required = {"source_id", "archive_id", "version_no", "title", "body"}
        missing = required - columns
        if missing:
            raise FactError(
                "pipeline_schema",
                f"{db_path} has no usable versions table (missing "
                f"{sorted(missing)}); this job needs the identity schema",
                path=str(db_path), relation=NOTICE_RELATION)
        sources = [row[0] for row in frozen.execute(
            "SELECT DISTINCT source_id FROM versions ORDER BY source_id")]
        cursor = frozen.execute(
            "SELECT source_id, archive_id, version_no, title, body "
            "FROM versions ORDER BY source_id, archive_id, version_no")
        for row in cursor:
            source_id, archive_id, version_no, title, body = (
                row[0], row[1], row[2], row[3], row[4])
            for label, value in (("source_id", source_id),
                                 ("title", title), ("body", body)):
                if not isinstance(value, str):
                    raise FactError(
                        "pipeline_type",
                        f"{db_path}: versions.{label} must be text "
                        f"({source_id}/{archive_id})",
                        path=str(db_path), relation=NOTICE_RELATION,
                        column=label)
            if type(version_no) is not int:
                raise FactError(
                    "pipeline_type",
                    f"{db_path}: versions.version_no must be an integer "
                    f"({source_id}/{archive_id})",
                    path=str(db_path), relation=NOTICE_RELATION,
                    column="version_no")
            records += 1
            conn.execute(_INSERT_SQL, tuple_params(
                run_id, NOTICE_RELATION,
                [encode(source_id), encode(archive_id), encode(version_no),
                 encode(title), encode(body)]))
            if count_changes(conn):
                inserted += 1
            else:
                duplicates += 1
    finally:
        frozen.close()
        shutil.rmtree(snapshot_dir, ignore_errors=True)
    digest_after = _sha256_file(db_path)
    return NoticeView(str(db_path), digest_before, digest_after, records,
                      duplicates, inserted, sources)

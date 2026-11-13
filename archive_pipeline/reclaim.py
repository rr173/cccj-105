"""Space reclamation for orphaned raw responses in the content-addressed archive.

Scope
-----
Only a raw response that is already on disk but is **not referenced by any
durable collection record** may be reclaimed.  Versions, scan facts, scan
request manifests, pagination manifests and the databases of downstream jobs
(funding facts, schedule ledger, eligibility results) are reference sources.
They are only *read*; no row is ever deleted or rewritten.

Reference rule
--------------
A digest is either referenced or not.  If it is referenced, **every copy** of
it under every source/date directory is protected -- an old-looking metadata
file next to one copy cannot make another copy collectable.  If a database or
a JSON manifest cannot be read reliably, reclamation refuses to run at all:
"nothing references it" is only a safe conclusion when every source was
readable.

Filesystem rule
---------------
Only the existing archive layout is recognized::

    <archive-root>/<source_id>/<yyyymmdd>/<sha256>.bin
    <archive-root>/<source_id>/<yyyymmdd>/<sha256>.bin.gz
    <archive-root>/<source_id>/<yyyymmdd>/<sha256>.json

Ordinary files only (no symlinks, no odd paths).  For gzip the digest is
verified over the **decompressed** response bytes, never over the compressed
container.  Incomplete groups, corrupt gzip, digest mismatches, metadata
ownership conflicts, symlinks and foreign paths are diagnostics -- never
reclaim candidates.

Candidate rule
--------------
A complete, verified, unreferenced group becomes a candidate only when it is
older than the operator-supplied grace cutoff (filesystem mtime *and* the
metadata ``fetched_at``).  Bytes written moments ago by a scan that has not
committed its facts yet must never be collected just because no row exists.

Plan / isolate / restore / purge
--------------------------------
The plan is a persisted, re-openable snapshot with per-file rows: relative
path, response digest, on-disk bytes, reference verdict, and the file and
database evidence used for the check.  The summary separates protected,
within-grace, isolatable and anomalous occupancy.  An estimated amount is
never labelled as freed space.

Isolation moves files into a program-managed quarantine inside the archive
root.  It is journalled per file (move-then-record) so a process killed
between the response file and its metadata leaves enough durable state to
resume or undo.  Before the first move the plan is revalidated: a candidate
that has gained a reference, or a file whose bytes changed, makes the plan
stale and refuses it entirely.  Restore refuses to overwrite a different file
that appeared at an original path and keeps both.  Purge re-checks for new
references and never counts a file twice when retried after an interruption.

Concurrency
-----------
Plan/isolate/restore/purge all hold the exclusive work-directory lock
(:mod:`archive_pipeline.worklock`) while collection holds it shared, so the
window between writing raw bytes and committing the database facts is covered
by construction -- not by a larger grace period.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .worklock import exclusive_lock

SCHEMA_VERSION = 1

HEX64 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SLUG = re.compile(r"^[a-z0-9_\-]+$")
DATE_DIR = re.compile(r"^[0-9]{8}$")

VERDICT_PROTECTED = "protected"
VERDICT_GRACE = "within_grace"
VERDICT_CANDIDATE = "isolatable"
VERDICT_ANOMALY = "anomaly"

QUARANTINE_TOP = ".reclaim"
QUARANTINE_SUBDIR = "quarantine"

PLAN_PLANNED = "planned"
PLAN_ISOLATING = "isolating"
PLAN_ISOLATED = "isolated"
PLAN_RESTORING = "restoring"
PLAN_RESTORED = "restored"
PLAN_PURGING = "purging"
PLAN_PURGED = "purged"
PLAN_STALE = "stale"
PLAN_ISOLATE_INCOMPLETE = "isolation_incomplete"
PLAN_RESTORED_WITH_CONFLICTS = "restored_with_conflicts"
PLAN_PURGE_BLOCKED = "purge_blocked"

FILE_PLANNED = "planned"
FILE_MOVED = "moved"
FILE_RESTORED = "restored"
FILE_PURGED = "purged"
FILE_CONFLICT = "conflict"
FILE_FAILED = "failed"
FILE_MISSING = "missing"

ACTION_MOVE_IN = "move_in"
ACTION_MOVE_OUT = "move_out"
ACTION_UNLINK = "unlink"

REFERENCE_TABLES = (
    ("versions", "raw_digest", "version"),
    ("scan_items", "raw_digest", "scan_item"),
)

JSON_COLUMNS = (
    ("scan_items", "detail_json", "scan_item_detail"),
    ("scans", "requests_json", "scan_requests"),
    ("scans", "pages_json", "scan_pages"),
)

# Downstream jobs in the same work directory are additional durable evidence.
JOB_DATABASES = (
    ("funding.sqlite3", (("facts", "raw_digest", "funding_fact"),
                         ("version_errors", "raw_digest", "funding_version_error"),
                         ("table_outcomes", "raw_digest", "funding_table_outcome"))),
    ("schedule.sqlite3", (("extraction_versions", "raw_digest", "schedule_extraction"),)),
    ("eligibility.sqlite3", ()),
)

MANIFEST_DIRS = ("reports/requests", "requests", "manifests")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(
            f"timestamp {value!r} has no timezone; pass an ISO-8601 value with "
            "an explicit offset")
    return moment


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReclaimError(RuntimeError):
    """Usage/precondition failure; nothing was changed."""


class PlanPurged(ReclaimError):
    """The plan was permanently purged and can no longer be isolated/undone."""


class ReferenceError(ReclaimError):
    """A reference database or manifest could not be read reliably."""


class StalePlan(ReclaimError):
    """The saved plan no longer matches the database or the files."""

    def __init__(self, message: str, conflicts: list[dict]):
        super().__init__(message)
        self.conflicts = conflicts


class PlanConflict(ReclaimError):
    """An operation found conflicting content and refused part of the work."""

    def __init__(self, message: str, conflicts: list[dict]):
        super().__init__(message)
        self.conflicts = conflicts


class ReclaimInterrupted(RuntimeError):
    """Controlled interruption used by tests/demos (state stays journalled)."""


class ReclaimCrash(BaseException):
    """Hard process stop used to demonstrate a kill mid-isolation."""


# ----------------------------------------------------------------------
# Reference collection (read-only)
# ----------------------------------------------------------------------
def _iter_json_digests(value: Any) -> list[str]:
    """Every 64-hex value stored under a key that names a digest."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if (isinstance(item, str) and HEX64.match(item)
                    and ("digest" in lowered or "sha256" in lowered)):
                found.append(item)
            else:
                found.extend(_iter_json_digests(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_iter_json_digests(item))
    return found


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


class ReferenceIndex:
    """Digests referenced by durable collection records, with evidence."""

    def __init__(self, digests: set[str], evidence: dict[str, list[dict]],
                 sources: list[dict]):
        self.digests = digests
        self.evidence = evidence
        self.sources = sources

    def referenced(self, digest: str) -> bool:
        return digest in self.digests

    def explain(self, digest: str) -> list[dict]:
        return self.evidence.get(digest, [])

    def summary(self) -> dict:
        return {
            "sources": self.sources,
            "referenced_digest_count": len(self.digests),
            "reference_rows": sum(s.get("rows_scanned", 0) for s in self.sources),
        }


def _note(index: dict, digest: str, evidence: dict) -> None:
    index.setdefault(digest, []).append(evidence)


def _scan_pipeline_database(db_path: Path, digests: dict[str, list[dict]],
                            sources: list[dict]) -> None:
    try:
        conn = _open_readonly(db_path)
    except sqlite3.Error as exc:
        raise ReferenceError(f"cannot open reference database {db_path}: {exc}") from exc
    try:
        try:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            raise ReferenceError(
                f"cannot list tables of reference database {db_path}: {exc}") from exc
        if not tables:
            raise ReferenceError(
                f"reference database {db_path} contains no tables; refusing to "
                "assume it has no references")
        # A pre-identity database keeps its facts in different tables; reading
        # only the identity-schema tables would under-report references.
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < 2 and "url_frontier" in tables:
            raise ReferenceError(
                f"reference database {db_path} is a pre-identity database "
                "(user_version < 2 with legacy tables); run "
                "'python -m archive_pipeline.cli repair --apply' first")
        rows_scanned = 0
        columns_checked = 0
        for table, column, label in REFERENCE_TABLES:
            if table not in tables:
                continue
            columns = _table_columns(conn, table)
            if column not in columns:
                continue
            columns_checked += 1
            try:
                for row in conn.execute(
                        f"SELECT {column} AS digest FROM {table} "
                        f"WHERE {column} IS NOT NULL AND {column} != ''"):
                    digest = row["digest"]
                    rows_scanned += 1
                    if not HEX64.match(digest):
                        continue
                    _note(digests, digest, {
                        "kind": label, "database": db_path.name,
                        "table": table, "column": column,
                    })
            except sqlite3.Error as exc:
                raise ReferenceError(
                    f"cannot read {table}.{column} in {db_path}: {exc}") from exc
        for table, column, label in JSON_COLUMNS:
            if table not in tables:
                continue
            columns = _table_columns(conn, table)
            if column not in columns:
                continue
            columns_checked += 1
            try:
                rows = conn.execute(
                    f"SELECT rowid AS rid, {column} AS payload FROM {table}")
                for row in rows:
                    rows_scanned += 1
                    payload = row["payload"]
                    if not payload:
                        continue
                    try:
                        decoded = json.loads(payload)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ReferenceError(
                            f"unreadable manifest {db_path.name} "
                            f"{table}.{column} rowid={row['rid']}: {exc}") from exc
                    for digest in _iter_json_digests(decoded):
                        _note(digests, digest, {
                            "kind": label, "database": db_path.name,
                            "table": table, "column": column,
                            "rowid": row["rid"],
                        })
            except sqlite3.Error as exc:
                raise ReferenceError(
                    f"cannot read manifest {table}.{column} in {db_path}: {exc}") from exc
        if columns_checked == 0:
            raise ReferenceError(
                f"reference database {db_path} exposes none of the expected "
                "raw-digest columns; it is not a schema this tool can verify")
        sources.append({"database": db_path.name, "path": str(db_path),
                        "rows_scanned": rows_scanned,
                        "columns_checked": columns_checked})
    finally:
        conn.close()


def _scan_job_database(db_path: Path, specs, digests: dict[str, list[dict]],
                       sources: list[dict]) -> None:
    if db_path.stat().st_size == 0:
        raise ReferenceError(
            f"reference database {db_path} is empty; refusing to assume it has "
            "no references")
    try:
        conn = _open_readonly(db_path)
    except sqlite3.Error as exc:
        raise ReferenceError(f"cannot open reference database {db_path}: {exc}") from exc
    try:
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        rows_scanned = 0
        for table, column, label in specs:
            if table not in tables or column not in _table_columns(conn, table):
                continue
            for row in conn.execute(
                    f"SELECT {column} AS digest FROM {table} "
                    f"WHERE {column} IS NOT NULL AND {column} != ''"):
                rows_scanned += 1
                digest = row["digest"]
                if HEX64.match(digest):
                    _note(digests, digest, {
                        "kind": label, "database": db_path.name,
                        "table": table, "column": column,
                    })
        sources.append({"database": db_path.name, "path": str(db_path),
                        "rows_scanned": rows_scanned,
                        "columns_checked": len(specs)})
    except sqlite3.Error as exc:
        raise ReferenceError(
            f"cannot read reference database {db_path}: {exc}") from exc
    finally:
        conn.close()


def _scan_manifest_file(path: Path, digests: dict[str, list[dict]],
                        sources: list[dict]) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceError(f"unreadable request/pagination manifest {path}: {exc}") from exc
    found = _iter_json_digests(payload)
    for digest in found:
        _note(digests, digest, {"kind": "json_manifest", "path": str(path)})
    sources.append({"manifest": str(path), "rows_scanned": len(found)})


def build_reference_index(workdir: str | Path) -> ReferenceIndex:
    """Read every durable reference source; any unreadable source is fatal."""
    workdir = Path(workdir)
    digests: dict[str, list[dict]] = {}
    sources: list[dict] = []
    pipeline = workdir / "pipeline.sqlite3"
    if not pipeline.exists():
        raise ReferenceError(
            f"reference database {pipeline} is missing; refusing to treat the "
            "archive as unreferenced without the collection database")
    _scan_pipeline_database(pipeline, digests, sources)
    for name, specs in JOB_DATABASES:
        path = workdir / name
        if path.exists():
            _scan_job_database(path, specs, digests, sources)
    for relative in MANIFEST_DIRS:
        directory = workdir / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            _scan_manifest_file(path, digests, sources)
    return ReferenceIndex(set(digests), digests, sources)


# ----------------------------------------------------------------------
# Filesystem scan
# ----------------------------------------------------------------------
@dataclass
class ScannedFile:
    rel_path: str
    role: str            # blob / blob_gz / meta
    size: int
    sha256: str | None
    mtime_ns: int
    mtime: datetime
    anomaly: str = ""


@dataclass
class ScannedGroup:
    source_id: str
    date_dir: str
    digest: str
    group_rel: str
    files: list[ScannedFile] = field(default_factory=list)
    verdict: str = VERDICT_ANOMALY
    reason: str = ""
    anomalies: list[str] = field(default_factory=list)
    metadata: dict | None = None
    reference_evidence: list[dict] = field(default_factory=list)

    @property
    def disk_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def response_bytes(self) -> int | None:
        meta = self.metadata or {}
        value = meta.get("bytes")
        return int(value) if isinstance(value, int) else None

    def to_payload(self) -> dict:
        return {
            "source_id": self.source_id,
            "date_dir": self.date_dir,
            "digest": self.digest,
            "group_rel": self.group_rel,
            "verdict": self.verdict,
            "reason": self.reason,
            "anomalies": self.anomalies,
            "disk_bytes": self.disk_bytes,
            "response_bytes": self.response_bytes,
            "reference_evidence": self.reference_evidence,
            "files": [
                {"rel_path": f.rel_path, "role": f.role, "size": f.size,
                 "file_sha256": f.sha256,
                 "mtime": f.mtime.isoformat(timespec="seconds"),
                 "anomaly": f.anomaly}
                for f in self.files
            ],
        }


ROLE_BY_SUFFIX = {".bin": "blob", ".gz": "blob_gz", ".json": "meta"}


def _classify_name(name: str) -> tuple[str, str] | None:
    """Return ``(digest, role)`` for a recognized archive file name."""
    if name.endswith(".bin.gz"):
        digest, role = name[:-7], "blob_gz"
    elif name.endswith(".bin"):
        digest, role = name[:-4], "blob"
    elif name.endswith(".json"):
        digest, role = name[:-5], "meta"
    else:
        return None
    if not HEX64.match(digest):
        return None
    return digest, role


def _safe_child(root: Path, rel: str) -> Path:
    """Join ``rel`` onto ``root`` after proving it cannot escape the root.

    Only the *parent* components are resolved: the final component is left
    alone so a symlink at the target path is detected by the caller instead of
    silently followed somewhere else.
    """
    if not rel or rel.startswith("/") or "\\" in rel:
        raise ReclaimError(f"unsafe relative path {rel!r}")
    parts = Path(rel).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ReclaimError(f"unsafe relative path {rel!r}")
    candidate = root.joinpath(*parts)
    resolved_root = root.resolve()
    resolved_parent = candidate.parent.resolve()
    if (resolved_parent != resolved_root
            and resolved_root not in resolved_parent.parents):
        raise ReclaimError(f"path escapes the archive root: {rel!r}")
    return candidate


def _is_regular(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _read_metadata(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("metadata is not a JSON object")
    return payload


def _verify_group(root: Path, group: ScannedGroup) -> str | None:
    """Return an anomaly code, or None when the group is complete and verified."""
    roles = [f.role for f in group.files]
    if "blob" in roles and "blob_gz" in roles:
        return "duplicate_blob_representation"
    if "meta" not in roles:
        return "incomplete_group_missing_metadata"
    if "blob" not in roles and "blob_gz" not in roles:
        return "incomplete_group_missing_blob"
    blob = next(f for f in group.files if f.role in {"blob", "blob_gz"})
    meta = next(f for f in group.files if f.role == "meta")
    blob_path = _safe_child(root, blob.rel_path)
    meta_path = _safe_child(root, meta.rel_path)
    if blob.anomaly or meta.anomaly:
        return blob.anomaly or meta.anomaly
    try:
        raw = blob_path.read_bytes()
    except OSError as exc:
        return f"unreadable_blob:{exc.__class__.__name__}"
    if blob.role == "blob_gz":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
            return f"corrupt_gzip:{exc.__class__.__name__}"
    if hashlib.sha256(raw).hexdigest() != group.digest:
        return "digest_mismatch"
    try:
        metadata = _read_metadata(meta_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"unreadable_metadata:{exc.__class__.__name__}"
    group.metadata = metadata
    if metadata.get("digest") != group.digest:
        return "metadata_digest_conflict"
    if metadata.get("digest_algorithm") not in (None, "sha256"):
        return "metadata_digest_algorithm_conflict"
    if metadata.get("source_id") not in (None, group.source_id):
        return "metadata_source_conflict"
    if metadata.get("bytes") is not None and metadata.get("bytes") != len(raw):
        return "metadata_size_conflict"
    storage = metadata.get("storage")
    if storage is not None and storage not in {"identity", "gzip"}:
        return "metadata_storage_conflict"
    if storage == "gzip" and blob.role != "blob_gz":
        return "metadata_storage_conflict"
    if storage == "identity" and blob.role != "blob":
        return "metadata_storage_conflict"
    for text in (metadata.get("fetched_at"),):
        if text is not None:
            try:
                parse_iso(str(text))
            except ValueError:
                return "metadata_fetched_at_invalid"
    return None


def scan_archive(archive_root: str | Path, references: ReferenceIndex,
                 cutoff: datetime) -> tuple[list[ScannedGroup], list[dict]]:
    """Classify every archive file; never mutates anything."""
    archive_root = Path(archive_root)
    groups: dict[tuple[str, str, str], ScannedGroup] = {}
    anomalies: list[dict] = []
    if not archive_root.exists():
        raise ReclaimError(f"archive root does not exist: {archive_root}")
    if not archive_root.is_dir() or archive_root.is_symlink():
        raise ReclaimError(f"archive root is not a directory: {archive_root}")

    for top in sorted(archive_root.iterdir()):
        if top.name == QUARANTINE_TOP:
            continue
        if top.is_symlink():
            anomalies.append({"rel_path": top.name, "reason": "symlink_at_root",
                              "size": top.lstat().st_size})
            groups[("\0root", top.name, "")] = _anomaly_group(
                "", "", top.name, top.lstat().st_size, "symlink_at_root")
            continue
        if not top.is_dir():
            stat = top.lstat()
            anomalies.append({"rel_path": top.name, "reason": "foreign_file_at_root",
                              "size": stat.st_size})
            groups[("\0root", top.name, "")] = _anomaly_group(
                "", "", top.name, stat.st_size, "foreign_file_at_root")
            continue
        if not SOURCE_SLUG.match(top.name):
            anomalies.append({"rel_path": top.name,
                              "reason": "unexpected_root_directory", "size": 0})
            groups[("\0root", top.name, "")] = _anomaly_group(
                "", "", top.name, 0, "unexpected_root_directory")
            for date_dir in sorted(top.iterdir()):
                if not date_dir.is_dir() or date_dir.is_symlink():
                    continue
                for entry in sorted(date_dir.iterdir()):
                    if entry.is_file() and not entry.is_symlink():
                        rel = f"{top.name}/{date_dir.name}/{entry.name}"
                        anomalies.append({"rel_path": rel,
                                          "reason": "foreign_path",
                                          "size": entry.lstat().st_size})
                        groups[("\0raw", rel, "")] = _anomaly_group(
                            "", "", rel, entry.lstat().st_size, "foreign_path")
            continue
        source_id = top.name
        for date_dir in sorted(top.iterdir()):
            rel_dir = f"{source_id}/{date_dir.name}"
            if date_dir.is_symlink():
                anomalies.append({"rel_path": rel_dir, "reason": "symlink_directory",
                                  "size": date_dir.lstat().st_size})
                groups[("\0dir", rel_dir, "")] = _anomaly_group(
                    source_id, date_dir.name, rel_dir, date_dir.lstat().st_size,
                    "symlink_directory")
                continue
            if not date_dir.is_dir():
                anomalies.append({"rel_path": rel_dir,
                                  "reason": "unexpected_source_entry",
                                  "size": date_dir.lstat().st_size})
                groups[("\0raw", rel_dir, "")] = _anomaly_group(
                    source_id, date_dir.name, rel_dir,
                    date_dir.lstat().st_size, "unexpected_source_entry")
                continue
            if not DATE_DIR.match(date_dir.name):
                anomalies.append({"rel_path": rel_dir,
                                  "reason": "unexpected_date_directory"})
                for entry in sorted(date_dir.iterdir()):
                    if entry.is_file() and not entry.is_symlink():
                        anomalies.append({
                            "rel_path": f"{rel_dir}/{entry.name}",
                            "reason": "foreign_path", "size": entry.lstat().st_size})
                continue
            for entry in sorted(date_dir.iterdir()):
                rel_path = f"{rel_dir}/{entry.name}"
                stat = entry.lstat()
                if entry.is_symlink():
                    anomalies.append({"rel_path": rel_path, "reason": "symlink",
                                      "size": stat.st_size})
                    groups.setdefault(("\0raw", rel_path, ""), _anomaly_group(
                        source_id, date_dir.name, rel_path, stat.st_size, "symlink"))
                    continue
                classified = _classify_name(entry.name) if entry.is_file() else None
                if classified is None:
                    anomalies.append({"rel_path": rel_path,
                                      "reason": "unrecognized_path",
                                      "size": stat.st_size})
                    groups.setdefault(("\0raw", rel_path, ""), _anomaly_group(
                        source_id, date_dir.name, rel_path, stat.st_size,
                        "unrecognized_path"))
                    continue
                digest, role = classified
                key = (source_id, date_dir.name, digest)
                group = groups.get(key)
                if group is None:
                    group = ScannedGroup(
                        source_id=source_id, date_dir=date_dir.name,
                        digest=digest, group_rel=f"{rel_dir}/{digest}")
                    groups[key] = group
                group.files.append(ScannedFile(
                    rel_path=rel_path, role=role, size=stat.st_size,
                    sha256=sha256_file(entry), mtime_ns=stat.st_mtime_ns,
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)))

    result: list[ScannedGroup] = []
    for key, group in groups.items():
        if key[0] == "\0root" or key[0] == "\0dir" or key[0] == "\0raw":
            group.verdict = VERDICT_ANOMALY
            if not group.reason:
                group.reason = group.anomalies[0] if group.anomalies else "anomaly"
            result.append(group)
            continue
        problem = _verify_group(archive_root, group)
        if problem == "incomplete_group_missing_metadata":
            # An incomplete group is never a candidate, but if it is young it
            # is inside the grace period and belongs in that bucket for
            # statistics (an old one is an anomaly, since it can never be
            # completed and collected).
            latest = max(f.mtime for f in group.files)
            if latest >= cutoff:
                group.verdict = VERDICT_GRACE
                group.reason = "incomplete_group_missing_metadata_within_grace"
                group.anomalies.append("incomplete_group_missing_metadata")
                result.append(group)
                continue
        if problem:
            group.verdict = VERDICT_ANOMALY
            group.reason = problem
            group.anomalies.append(problem)
            result.append(group)
            continue
        if references.referenced(group.digest):
            group.verdict = VERDICT_PROTECTED
            group.reference_evidence = references.explain(group.digest)
            group.reason = ("referenced_by:"
                            + ",".join(sorted({e["kind"] for e in
                                               group.reference_evidence})))
            result.append(group)
            continue
        fetched_at = (group.metadata or {}).get("fetched_at")
        latest = max(f.mtime for f in group.files)
        if fetched_at is not None:
            latest = max(latest, parse_iso(str(fetched_at)))
        if latest >= cutoff:
            group.verdict = VERDICT_GRACE
            group.reason = f"newer_than_grace_cutoff:{latest.isoformat()}"
            result.append(group)
            continue
        group.verdict = VERDICT_CANDIDATE
        group.reason = "unreferenced_and_past_grace_cutoff"
        result.append(group)
    return result, anomalies


def _anomaly_group(source_id: str, date_dir: str, rel: str, size: int,
                   reason: str) -> ScannedGroup:
    group = ScannedGroup(source_id=source_id, date_dir=date_dir, digest="",
                         group_rel=rel, verdict=VERDICT_ANOMALY,
                         reason=reason, anomalies=[reason])
    group.files.append(ScannedFile(
        rel_path=rel, role="other", size=size, sha256=None, mtime_ns=0,
        mtime=datetime.fromtimestamp(0, tz=timezone.utc)))
    return group


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id        TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    workdir        TEXT NOT NULL,
    archive_root   TEXT NOT NULL,
    cutoff         TEXT NOT NULL,
    status         TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    summary_json   TEXT NOT NULL DEFAULT '{}',
    conflict_json  TEXT NOT NULL DEFAULT '[]',
    note           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS groups (
    group_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id         TEXT NOT NULL,
    grouping_key    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    date_dir        TEXT NOT NULL,
    digest          TEXT NOT NULL,
    group_rel       TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    reason          TEXT NOT NULL,
    disk_bytes      INTEGER NOT NULL,
    response_bytes  INTEGER,
    reference_json  TEXT NOT NULL DEFAULT '[]',
    anomalies_json  TEXT NOT NULL DEFAULT '[]',
    UNIQUE (plan_id, grouping_key)
);

CREATE TABLE IF NOT EXISTS files (
    file_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id        TEXT NOT NULL,
    group_id       INTEGER NOT NULL,
    rel_path       TEXT NOT NULL,
    role           TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    sha256         TEXT,
    mtime_ns       INTEGER NOT NULL,
    state          TEXT NOT NULL DEFAULT 'planned',
    quarantine_rel TEXT,
    moved_at       TEXT,
    restored_at    TEXT,
    purged_at      TEXT,
    released_bytes INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    UNIQUE (plan_id, rel_path)
);

CREATE TABLE IF NOT EXISTS actions (
    action_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT NOT NULL,
    file_id     INTEGER,
    action      TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


class ReclaimStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def plan_row(self, plan_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def file_rows(self, plan_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT f.*, g.digest AS digest, g.verdict AS verdict, "
            "g.group_rel AS group_rel, g.source_id AS source_id "
            "FROM files f JOIN groups g ON g.group_id=f.group_id "
            "WHERE f.plan_id=? ORDER BY f.file_id", (plan_id,))]

    def group_rows(self, plan_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM groups WHERE plan_id=? ORDER BY group_id", (plan_id,))]


def workdir_of(workdir: str | Path) -> Path:
    return Path(workdir)


def _default_archive_root(workdir: Path) -> Path:
    return workdir / "archive"


def resolve_archive_root(workdir: str | Path, archive_root: str | Path | None) -> Path:
    """Resolve the archive boundary from the configured/actual directory.

    With no explicit ``--archive`` the boundary is ``<workdir>/archive`` -- the
    directory the scanner actually writes to.  An explicit relative path is
    resolved against the work directory, never against the process CWD.
    """
    workdir = Path(workdir).resolve()
    if archive_root is None:
        return (workdir / "archive").resolve()
    root = Path(archive_root)
    if not root.is_absolute():
        root = workdir / root
    return root.resolve()


def _quarantine_root(archive_root: Path, plan_id: str) -> Path:
    return archive_root / QUARANTINE_TOP / QUARANTINE_SUBDIR / plan_id


def _quarantine_rel(plan_id: str, rel_path: str) -> str:
    return f"{QUARANTINE_TOP}/{QUARANTINE_SUBDIR}/{plan_id}/{rel_path}"


def _safe_quarantine_path(archive_root: Path, plan_id: str, rel_path: str) -> Path:
    return _safe_child(archive_root, _quarantine_rel(plan_id, rel_path))


def _summarize(groups: Iterable[ScannedGroup], references: ReferenceIndex,
               cutoff: datetime) -> dict:
    summary = {
        "cutoff": cutoff.isoformat(),
        "reference_sources": references.summary(),
        "protected": {"groups": 0, "disk_bytes": 0},
        "within_grace": {"groups": 0, "disk_bytes": 0},
        "isolatable": {"groups": 0, "disk_bytes": 0, "response_bytes": 0},
        "anomaly": {"groups": 0, "disk_bytes": 0},
        "estimated_reclaimable_disk_bytes": 0,
        "freed_disk_bytes": 0,
        "quarantined_disk_bytes": 0,
        "note": ("estimated_reclaimable_disk_bytes is the on-disk size of "
                 "isolatable groups; nothing has been freed and the estimate is "
                 "not released space"),
    }
    for group in groups:
        key = {VERDICT_PROTECTED: "protected",
               VERDICT_GRACE: "within_grace",
               VERDICT_CANDIDATE: "isolatable",
               VERDICT_ANOMALY: "anomaly"}[group.verdict]
        summary[key]["groups"] += 1
        summary[key]["disk_bytes"] += group.disk_bytes
        if key == "isolatable":
            summary[key]["response_bytes"] += group.response_bytes or 0
    summary["estimated_reclaimable_disk_bytes"] = \
        summary["isolatable"]["disk_bytes"]
    return summary


def preview(workdir: str | Path, archive_root: str | Path | None,
            cutoff: datetime) -> dict:
    """Read-only classification; creates no plan and moves nothing."""
    workdir = Path(workdir)
    root = resolve_archive_root(workdir, archive_root)
    references = build_reference_index(workdir)
    groups, anomalies = scan_archive(root, references, cutoff)
    summary = _summarize(groups, references, cutoff)
    return {
        "mode": "preview",
        "archive_root": str(root),
        "workdir": str(workdir),
        "summary": summary,
        "groups": [g.to_payload() for g in groups],
        "anomalies": anomalies,
        "file_moves": 0,
    }


def create_plan(workdir: str | Path, archive_root: str | Path | None,
                cutoff: datetime) -> dict:
    """Persist a re-openable plan; still moves nothing."""
    workdir = Path(workdir)
    root = resolve_archive_root(workdir, archive_root)
    store = ReclaimStore(workdir / "reclaim.sqlite3")
    try:
        with exclusive_lock(workdir, timeout=30.0):
            references = build_reference_index(workdir)
            groups, anomalies = scan_archive(root, references, cutoff)
            summary = _summarize(groups, references, cutoff)
            summary["anomaly_details"] = anomalies
            plan_id = f"rcl_{uuid.uuid4().hex[:16]}"
            with store.tx() as conn:
                conn.execute(
                    "INSERT INTO plans(plan_id, schema_version, created_at, "
                    "workdir, archive_root, cutoff, status, updated_at, "
                    "summary_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (plan_id, SCHEMA_VERSION, now_iso(), str(workdir), str(root),
                     cutoff.isoformat(), PLAN_PLANNED, now_iso(),
                     json.dumps(summary, ensure_ascii=False)))
                for index, group in enumerate(groups):
                    key = f"{group.source_id}|{group.date_dir}|{group.digest}|" \
                          f"{group.group_rel}|{index}"
                    cur = conn.execute(
                        "INSERT INTO groups(plan_id, grouping_key, source_id, "
                        "date_dir, digest, group_rel, verdict, reason, disk_bytes, "
                        "response_bytes, reference_json, anomalies_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, key, group.source_id, group.date_dir,
                         group.digest, group.group_rel, group.verdict,
                         group.reason, group.disk_bytes, group.response_bytes,
                         json.dumps(group.reference_evidence, ensure_ascii=False),
                         json.dumps(group.anomalies, ensure_ascii=False)))
                    group_id = int(cur.lastrowid)
                    for file in group.files:
                        quarantine_rel = None
                        if group.verdict == VERDICT_CANDIDATE:
                            quarantine_rel = _quarantine_rel(plan_id, file.rel_path)
                        conn.execute(
                            "INSERT INTO files(plan_id, group_id, rel_path, role, "
                            "size_bytes, sha256, mtime_ns, state, quarantine_rel) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (plan_id, group_id, file.rel_path, file.role,
                             file.size, file.sha256, file.mtime_ns,
                             FILE_PLANNED, quarantine_rel))
            return plan_detail(store, plan_id)
    finally:
        store.close()


def open_plan(workdir: str | Path, plan_id: str) -> dict:
    workdir = Path(workdir)
    store = ReclaimStore(workdir / "reclaim.sqlite3")
    try:
        return plan_detail(store, plan_id)
    finally:
        store.close()


def plan_journal(workdir: str | Path, plan_id: str) -> dict:
    """Per-file state plus the append-only action log for one plan."""
    workdir = Path(workdir)
    store = ReclaimStore(workdir / "reclaim.sqlite3")
    try:
        detail = plan_detail(store, plan_id)
        actions = []
        for row in store.conn.execute(
                "SELECT * FROM actions WHERE plan_id=? ORDER BY action_id",
                (plan_id,)):
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            actions.append(item)
        return {
            "plan_id": plan_id,
            "status": detail["status"],
            "released_disk_bytes": detail["released_disk_bytes"],
            "quarantined_disk_bytes": detail["quarantined_disk_bytes"],
            "files": [{"file_id": f["file_id"], "rel_path": f["rel_path"],
                       "role": f["role"], "state": f["state"],
                       "quarantine_rel": f["quarantine_rel"],
                       "released_bytes": f["released_bytes"], "note": f["note"]}
                      for f in store.file_rows(plan_id)],
            "actions": actions,
        }
    finally:
        store.close()


def list_plans(workdir: str | Path) -> list[dict]:
    workdir = Path(workdir)
    store = ReclaimStore(workdir / "reclaim.sqlite3")
    try:
        rows = [dict(r) for r in store.conn.execute(
            "SELECT plan_id, created_at, status, archive_root, cutoff, "
            "summary_json FROM plans ORDER BY created_at, plan_id")]
        result = []
        for row in rows:
            summary = json.loads(row.pop("summary_json"))
            row["estimated_reclaimable_disk_bytes"] = \
                summary.get("estimated_reclaimable_disk_bytes", 0)
            row["freed_disk_bytes"] = _released_bytes(store, row["plan_id"])
            result.append(row)
        return result
    finally:
        store.close()


def _released_bytes(store: ReclaimStore, plan_id: str) -> int:
    row = store.conn.execute(
        "SELECT COALESCE(SUM(released_bytes), 0) AS total FROM files "
        "WHERE plan_id=?", (plan_id,)).fetchone()
    return int(row["total"])


def _counted_quarantined_bytes(store: ReclaimStore, plan_id: str) -> int:
    row = store.conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM files "
        "WHERE plan_id=? AND state=?", (plan_id, FILE_MOVED)).fetchone()
    return int(row["total"])


def plan_detail(store: ReclaimStore, plan_id: str) -> dict:
    plan = store.plan_row(plan_id)
    if plan is None:
        raise ReclaimError(f"unknown plan {plan_id}")
    plan["summary"] = json.loads(plan.pop("summary_json"))
    plan["conflicts"] = json.loads(plan.pop("conflict_json"))
    plan["released_disk_bytes"] = _released_bytes(store, plan_id)
    plan["quarantined_disk_bytes"] = _counted_quarantined_bytes(store, plan_id)
    plan["groups"] = []
    for group in store.group_rows(plan_id):
        group["reference_evidence"] = json.loads(group.pop("reference_json"))
        group["anomalies"] = json.loads(group.pop("anomalies_json"))
        group["files"] = [
            {**{k: f[k] for k in ("file_id", "rel_path", "role", "size_bytes",
                                  "sha256", "state", "quarantine_rel", "note",
                                  "released_bytes", "moved_at")},
             "digest": group["digest"]}
            for f in store.file_rows(plan_id) if f["group_id"] == group["group_id"]
        ]
        plan["groups"].append(group)
    return plan


# ----------------------------------------------------------------------
# Journaled operations
# ----------------------------------------------------------------------
FILE_MOVING = "moving"
FILE_RESTORING = "restoring"
FILE_PURGING = "purging"


def _plan_files(store: ReclaimStore, plan_id: str) -> list[dict]:
    return store.file_rows(plan_id)


def _candidate_files(store: ReclaimStore, plan_id: str) -> list[dict]:
    return [f for f in store.file_rows(plan_id)
            if f["verdict"] == VERDICT_CANDIDATE]


def _set_plan(store: ReclaimStore, plan_id: str, status: str,
              conflicts: list[dict] | None = None, note: str | None = None) -> None:
    with store.tx() as conn:
        if conflicts is None:
            conn.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE plan_id=?",
                (status, now_iso(), plan_id))
        else:
            conn.execute(
                "UPDATE plans SET status=?, updated_at=?, conflict_json=? "
                "WHERE plan_id=?",
                (status, now_iso(),
                 json.dumps(conflicts, ensure_ascii=False), plan_id))
        if note is not None:
            conn.execute("UPDATE plans SET note=? WHERE plan_id=?", (note, plan_id))


def _begin_action(store: ReclaimStore, plan_id: str, file_id: int, action: str,
                  detail: dict) -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO actions(plan_id, file_id, action, status, created_at, "
            "detail_json) VALUES (?,?,?,?,?,?)",
            (plan_id, file_id, action, "started", now_iso(),
             json.dumps(detail, ensure_ascii=False)))
        return int(cur.lastrowid)


def _finish_action(store: ReclaimStore, action_id: int, status: str,
                   detail: dict | None = None) -> None:
    with store.tx() as conn:
        if detail is None:
            conn.execute(
                "UPDATE actions SET status=?, finished_at=? WHERE action_id=?",
                (status, now_iso(), action_id))
        else:
            conn.execute(
                "UPDATE actions SET status=?, finished_at=?, detail_json=? "
                "WHERE action_id=?",
                (status, now_iso(),
                 json.dumps(detail, ensure_ascii=False), action_id))


def _set_file(store: ReclaimStore, file_id: int, state: str,
              note: str | None = None, moved_at: str | None = None,
              restored_at: str | None = None, purged_at: str | None = None,
              released_bytes: int | None = None) -> None:
    fields = ["state=?"]
    values: list[Any] = [state]
    for column, value in (("note", note), ("moved_at", moved_at),
                          ("restored_at", restored_at),
                          ("purged_at", purged_at),
                          ("released_bytes", released_bytes)):
        if value is not None:
            fields.append(f"{column}=?")
            values.append(value)
    values.append(file_id)
    with store.tx() as conn:
        conn.execute(f"UPDATE files SET {', '.join(fields)} WHERE file_id=?",
                     values)


def _revalidate_candidates(store: ReclaimStore, plan: dict, workdir: Path,
                           archive_root: Path, cutoff: datetime) -> tuple[list[dict], bool]:
    """Re-derive references and re-classify the archive for a saved plan.

    Returns ``(conflicts, references_refreshed)``.  Any conflict means the
    plan must not move a single file: a candidate that gained a reference,
    changed bytes, lost part of its group, or was re-classified differently
    makes the whole plan stale.
    """
    conflicts: list[dict] = []
    candidates = _candidate_files(store, plan["plan_id"])
    digests = {f["digest"] for f in candidates}
    try:
        index = build_reference_index(workdir)
    except ReferenceError as exc:
        return [{"reason": "reference_unreadable", "detail": str(exc)}], False
    for digest in sorted(digests):
        if index.referenced(digest):
            conflicts.append({
                "reason": "new_reference",
                "digest": digest,
                "evidence": index.explain(digest),
            })
    try:
        current_groups, _ = scan_archive(archive_root, index, cutoff)
    except ReclaimError as exc:
        return conflicts + [{"reason": "archive_unreadable",
                             "detail": str(exc)}], True
    planned_paths = {f["rel_path"] for f in candidates
                     if f["state"] == FILE_PLANNED}
    moved_groups = {f["group_rel"] for f in candidates
                    if f["state"] in {FILE_MOVED, FILE_MOVING, FILE_CONFLICT}}
    by_path: dict[str, ScannedGroup] = {}
    for group in current_groups:
        for file in group.files:
            by_path[file.rel_path] = group
    for file in candidates:
        if file["state"] in {FILE_MOVED, FILE_MOVING, FILE_CONFLICT,
                             FILE_RESTORED, FILE_PURGED}:
            # Already handled by the interrupted run; only 'planned' and
            # 'failed' rows are subject to freshness revalidation.
            continue
        path = _safe_child(archive_root, file["rel_path"])
        quarantined = None
        if file.get("quarantine_rel"):
            quarantined = _safe_child(archive_root, file["quarantine_rel"])
        if (not path.exists() and not path.is_symlink() and quarantined
                and quarantined.is_file() and not quarantined.is_symlink()
                and file["sha256"]
                and sha256_file(quarantined) == file["sha256"]):
            # The file was moved just before the journal recorded it.  That is
            # not a conflict; _recover_moves/adoption will settle it.
            continue
        if path.is_symlink():
            conflicts.append({"reason": "file_became_symlink",
                              "rel_path": file["rel_path"]})
            continue
        if not path.is_file():
            current = by_path.get(file["rel_path"])
            if current is None:
                conflicts.append({"reason": "group_missing",
                                  "rel_path": file["rel_path"]})
            else:
                conflicts.append({"reason": "group_no_longer_isolatable",
                                  "rel_path": file["rel_path"],
                                  "group_rel": current.group_rel,
                                  "group_verdict": current.verdict,
                                  "group_reason": current.reason})
            continue
        stat = path.stat()
        if stat.st_size != file["size_bytes"] \
                or stat.st_mtime_ns != file["mtime_ns"] \
                or (file["sha256"] and sha256_file(path) != file["sha256"]):
            conflicts.append({"reason": "file_replaced",
                              "rel_path": file["rel_path"],
                              "expected_size": file["size_bytes"],
                              "actual_size": stat.st_size})
            continue
        current = by_path.get(file["rel_path"])
        if current is None:
            conflicts.append({"reason": "group_missing",
                              "rel_path": file["rel_path"]})
            continue
        moved_group = current.group_rel in moved_groups
        if current.verdict != VERDICT_CANDIDATE and not moved_group:
            conflicts.append({"reason": "group_no_longer_isolatable",
                              "rel_path": file["rel_path"],
                              "group_rel": current.group_rel,
                              "group_verdict": current.verdict,
                              "group_reason": current.reason})
    return conflicts, True


def isolate_plan(workdir: str | Path, plan_id: str, *,
                 interrupt_after_moves: int | None = None) -> dict:
    """Validate, then move every candidate file into program-managed quarantine."""
    workdir = Path(workdir)
    store = ReclaimStore(workdir / "reclaim.sqlite3")
    try:
        with exclusive_lock(workdir, timeout=30.0):
            plan = plan_detail(store, plan_id)
            archive_root = Path(plan["archive_root"])
            if plan["status"] == PLAN_PURGED:
                raise PlanPurged(f"plan {plan_id} was already purged; it cannot "
                                 "be isolated again")
            if plan["status"] == PLAN_ISOLATED:
                return {"plan_id": plan_id, "status": "already_isolated",
                        "moved": 0, "plan": plan}
            if plan["status"] in {PLAN_RESTORED, PLAN_RESTORED_WITH_CONFLICTS}:
                raise ReclaimError(f"plan {plan_id} was already restored; create "
                                   "a new plan instead of reusing it")
            if plan["status"] == PLAN_RESTORING:
                raise ReclaimError(
                    f"plan {plan_id} has a partially completed restore; finish "
                    "it with 'restore' before isolating again")
            resumed = plan["status"] == PLAN_ISOLATING
            recovered = _recover_moves(store, plan) if resumed else []
            candidates = _candidate_files(store, plan_id)
            # The revalidation runs even when resuming: a crash between moving
            # the response and its metadata must not be followed by a blind
            # replay of an old list.  Idempotent because only files still in
            # state 'planned' are touched.
            # Freshness is judged against the *plan's* cutoff: bytes written
            # after the plan was saved were never part of it and must not be
            # acted on.  Revalidation also runs when resuming an interrupted
            # isolation: a crash never authorises a blind replay.
            conflicts, _ = _revalidate_candidates(
                store, plan, workdir, archive_root,
                cutoff=parse_iso(plan["cutoff"]))
            if conflicts:
                _set_plan(store, plan_id, PLAN_STALE, conflicts,
                          "revalidation failed; no file was moved")
                raise StalePlan(
                    f"plan {plan_id} is stale: {len(conflicts)} conflict(s) "
                    "found during revalidation; no file was moved",
                    conflicts)
            _set_plan(store, plan_id, PLAN_ISOLATING)
            moved = 0
            results: list[dict] = []
            try:
                for file in candidates:
                    if file["state"] in {FILE_MOVED, FILE_CONFLICT}:
                        continue
                    if file["state"] not in {FILE_PLANNED, FILE_FAILED}:
                        results.append({"rel_path": file["rel_path"],
                                        "state": file["state"],
                                        "result": "skipped"})
                        continue
                    outcome = _isolate_file(store, plan_id, file, archive_root)
                    results.append(outcome)
                    if outcome["result"] == "moved":
                        moved += 1
                        if (interrupt_after_moves is not None
                                and moved >= interrupt_after_moves):
                            raise ReclaimCrash(
                                f"simulated process kill after {moved} move(s)")
            except ReclaimCrash:
                # The journal (action rows + file states) is the durable truth.
                raise
            conflicts = [r for r in results if r["result"] != "moved"]
            if conflicts:
                _set_plan(store, plan_id, PLAN_ISOLATE_INCOMPLETE,
                          [c for c in conflicts],
                          "some files could not be moved; see per-file results")
            else:
                _set_plan(store, plan_id, PLAN_ISOLATED)
            return {"plan_id": plan_id,
                    "status": plan_detail(store, plan_id)["status"],
                    "resumed": resumed, "recovered": recovered,
                    "moved": moved, "results": results,
                    "plan": plan_detail(store, plan_id)}
    finally:
        store.close()


def _isolate_file(store: ReclaimStore, plan_id: str, file: dict,
                  archive_root: Path) -> dict:
    src = _safe_child(archive_root, file["rel_path"])
    dest_rel = file["quarantine_rel"]
    dest = _safe_child(archive_root, dest_rel)
    if dest.exists():
        # A leftover from a previous attempt; only accept identical bytes.
        if dest.is_file() and file["sha256"] and sha256_file(dest) == file["sha256"]:
            _set_file(store, file["file_id"], FILE_MOVED,
                      note="quarantine copy already present; adopted",
                      moved_at=now_iso())
            return {"rel_path": file["rel_path"], "result": "moved",
                    "note": "adopted existing quarantine copy"}
        _set_file(store, file["file_id"], FILE_CONFLICT,
                  note=f"quarantine path already occupied: {dest_rel}")
        return {"rel_path": file["rel_path"], "result": "conflict",
                "reason": "quarantine_path_occupied", "quarantine_rel": dest_rel}
    action_id = _begin_action(store, plan_id, file["file_id"], ACTION_MOVE_IN,
                              {"src": file["rel_path"], "dst": dest_rel,
                               "sha256": file["sha256"], "size": file["size_bytes"]})
    _set_file(store, file["file_id"], FILE_MOVING)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _fsync_dir(dest.parent)
        os.rename(src, dest)
        _fsync_dir(src.parent)
        _fsync_dir(dest.parent)
    except OSError as exc:
        _finish_action(store, action_id, FILE_FAILED, {"error": repr(exc)})
        _set_file(store, file["file_id"], FILE_FAILED,
                  note=f"move failed: {exc}")
        return {"rel_path": file["rel_path"], "result": "failed",
                "reason": repr(exc)}
    _finish_action(store, action_id, "done")
    _set_file(store, file["file_id"], FILE_MOVED, moved_at=now_iso())
    return {"rel_path": file["rel_path"], "result": "moved",
            "quarantine_rel": dest_rel}


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _recover_moves(store: ReclaimStore, plan: dict) -> list[dict]:
    """Reconcile journalled move_in actions whose outcome is unknown."""
    recovered: list[dict] = []
    archive_root = Path(plan["archive_root"])
    actions = [dict(r) for r in store.conn.execute(
        "SELECT * FROM actions WHERE plan_id=? AND action=? AND status='started' "
        "ORDER BY action_id", (plan["plan_id"], ACTION_MOVE_IN))]
    files = {f["file_id"]: f for f in _plan_files(store, plan["plan_id"])}
    for action in actions:
        detail = json.loads(action["detail_json"])
        file = files.get(action["file_id"])
        if file is None:
            _finish_action(store, action["action_id"], "orphaned")
            continue
        src = _safe_child(archive_root, detail["src"])
        dest = _safe_child(archive_root, detail["dst"])
        expected = detail.get("sha256")
        src_ok = src.is_file() and not src.is_symlink() and expected \
            and sha256_file(src) == expected
        dest_ok = dest.is_file() and not dest.is_symlink() and expected \
            and sha256_file(dest) == expected
        if dest_ok and not src.exists():
            _finish_action(store, action["action_id"], "recovered",
                           {"outcome": "completed_before_crash"})
            _set_file(store, file["file_id"], FILE_MOVED, moved_at=now_iso(),
                      note="reconciled after interruption: file was in quarantine")
            recovered.append({"rel_path": file["rel_path"],
                              "outcome": "move_completed"})
        elif src_ok and not dest.exists():
            _finish_action(store, action["action_id"], "aborted",
                           {"outcome": "move_had_not_happened"})
            _set_file(store, file["file_id"], FILE_PLANNED,
                      note="reconciled after interruption: file was still in place")
            recovered.append({"rel_path": file["rel_path"],
                              "outcome": "move_not_started"})
        else:
            _finish_action(store, action["action_id"], "conflict",
                           {"outcome": "ambiguous", "src_exists": src.exists(),
                            "dest_exists": dest.exists()})
            _set_file(store, file["file_id"], FILE_CONFLICT,
                      note="interrupted move could not be reconciled; both "
                           "locations are kept for inspection")
            recovered.append({"rel_path": file["rel_path"],
                              "outcome": "conflict"})
    return recovered


def restore_plan(workdir: str | Path, plan_id: str) -> dict:
    """Move quarantined files back; never overwrite different content."""
    workdir = Path(workdir)
    store = ReclaimStore(workdir / "reclaim.sqlite3")
    try:
        with exclusive_lock(workdir, timeout=30.0):
            plan = plan_detail(store, plan_id)
            archive_root = Path(plan["archive_root"])
            if plan["status"] == PLAN_PURGED:
                raise PlanPurged(
                    f"plan {plan_id} was permanently purged; it is not restorable")
            if plan["status"] in {PLAN_RESTORED, PLAN_RESTORED_WITH_CONFLICTS}:
                return {"plan_id": plan_id, "status": "already_restored",
                        "restored": 0, "conflicts": plan["conflicts"],
                        "plan": plan}
            if plan["status"] == PLAN_PURGED:
                raise PlanPurged(
                    f"plan {plan_id} was permanently purged; it is not restorable")
            if plan["status"] == PLAN_ISOLATING:
                _recover_moves(store, plan)
            if plan["status"] == PLAN_RESTORING:
                _recover_restores(store, plan)
            _set_plan(store, plan_id, PLAN_RESTORING)
            files = [f for f in _plan_files(store, plan_id)
                     if f["state"] in {FILE_MOVED, FILE_MOVING, FILE_CONFLICT}
                     and f["verdict"] == VERDICT_CANDIDATE]
            restored = 0
            conflicts: list[dict] = []
            results: list[dict] = []
            for file in files:
                if file["state"] == FILE_CONFLICT and not file["quarantine_rel"]:
                    conflicts.append({"rel_path": file["rel_path"],
                                      "reason": "no_quarantine_location"})
                    continue
                outcome = _restore_file(store, plan_id, file, archive_root)
                results.append(outcome)
                if outcome["result"] == "restored":
                    restored += 1
                elif outcome["result"] == "conflict":
                    conflicts.append(outcome)
            if conflicts:
                _set_plan(store, plan_id, PLAN_RESTORED_WITH_CONFLICTS, conflicts,
                          "restored with conflicts; conflicting files kept in "
                          "both locations")
            else:
                _set_plan(store, plan_id, PLAN_RESTORED)
            return {"plan_id": plan_id, "status": plan_detail(store, plan_id)["status"],
                    "restored": restored, "conflicts": conflicts,
                    "results": results, "plan": plan_detail(store, plan_id)}
    finally:
        store.close()


def _restore_file(store: ReclaimStore, plan_id: str, file: dict,
                  archive_root: Path) -> dict:
    src_rel = file["quarantine_rel"]
    dest_rel = file["rel_path"]
    src = _safe_child(archive_root, src_rel)
    dest = _safe_child(archive_root, dest_rel)
    if dest.exists() or dest.is_symlink():
        if dest.is_file() and not dest.is_symlink() and file["sha256"] \
                and sha256_file(dest) == file["sha256"]:
            # Same content already present: keep it, drop the duplicate copy.
            action_id = _begin_action(
                store, plan_id, file["file_id"], ACTION_MOVE_OUT,
                {"src": src_rel, "dst": dest_rel, "dedupe": "identical"})
            try:
                if src.exists():
                    os.unlink(src)
                _fsync_dir(dest.parent)
            except OSError as exc:
                _finish_action(store, action_id, FILE_FAILED, {"error": repr(exc)})
                _set_file(store, file["file_id"], FILE_CONFLICT,
                          note=f"could not remove duplicate copy: {exc}")
                return {"rel_path": dest_rel, "result": "failed",
                        "reason": repr(exc)}
            _finish_action(store, action_id, "done")
            _set_file(store, file["file_id"], FILE_RESTORED,
                      restored_at=now_iso(),
                      note="identical content was already at the original path; "
                           "quarantine copy removed, existing file kept")
            return {"rel_path": dest_rel, "result": "restored",
                    "note": "identical existing file kept; duplicate removed"}
        _set_file(store, file["file_id"], FILE_CONFLICT,
                  note="original path holds different content; both files kept")
        return {"rel_path": dest_rel, "result": "conflict",
                "reason": "original_path_occupied_by_different_content",
                "quarantine_rel": src_rel}
    if not src.is_file() or src.is_symlink():
        _set_file(store, file["file_id"], FILE_CONFLICT,
                  note="quarantined file is missing")
        return {"rel_path": dest_rel, "result": "conflict",
                "reason": "quarantined_file_missing",
                "quarantine_rel": src_rel}
    if file["sha256"] and sha256_file(src) != file["sha256"]:
        _set_file(store, file["file_id"], FILE_CONFLICT,
                  note="quarantined file content changed")
        return {"rel_path": dest_rel, "result": "conflict",
                "reason": "quarantined_file_content_changed",
                "quarantine_rel": src_rel}
    action_id = _begin_action(store, plan_id, file["file_id"], ACTION_MOVE_OUT,
                              {"src": src_rel, "dst": dest_rel,
                               "sha256": file["sha256"]})
    _set_file(store, file["file_id"], FILE_RESTORING)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _fsync_dir(dest.parent)
        os.rename(src, dest)
        _fsync_dir(src.parent)
        _fsync_dir(dest.parent)
    except OSError as exc:
        _finish_action(store, action_id, FILE_FAILED, {"error": repr(exc)})
        _set_file(store, file["file_id"], FILE_FAILED,
                  note=f"restore failed: {exc}")
        return {"rel_path": dest_rel, "result": "failed", "reason": repr(exc)}
    _finish_action(store, action_id, "done")
    _set_file(store, file["file_id"], FILE_RESTORED, restored_at=now_iso())
    return {"rel_path": dest_rel, "result": "restored"}


def _recover_restores(store: ReclaimStore, plan: dict) -> list[dict]:
    """Reconcile move_out actions of an interrupted restore."""
    recovered: list[dict] = []
    archive_root = Path(plan["archive_root"])
    actions = [dict(r) for r in store.conn.execute(
        "SELECT * FROM actions WHERE plan_id=? AND action=? AND status='started' "
        "ORDER BY action_id", (plan["plan_id"], ACTION_MOVE_OUT))]
    files = {f["file_id"]: f for f in _plan_files(store, plan["plan_id"])}
    for action in actions:
        detail = json.loads(action["detail_json"])
        file = files.get(action["file_id"])
        if file is None:
            _finish_action(store, action["action_id"], "orphaned")
            continue
        dest = _safe_child(archive_root, detail["dst"])
        expected = detail.get("sha256")
        if dest.is_file() and not dest.is_symlink() and expected \
                and sha256_file(dest) == expected:
            _finish_action(store, action["action_id"], "recovered",
                           {"outcome": "restore_completed_before_crash"})
            _set_file(store, file["file_id"], FILE_RESTORED,
                      restored_at=now_iso(),
                      note="reconciled after interruption: file was back in place")
            recovered.append({"rel_path": file["rel_path"],
                              "outcome": "restore_completed"})
        else:
            _finish_action(store, action["action_id"], "aborted",
                           {"outcome": "restore_had_not_happened"})
            _set_file(store, file["file_id"], FILE_MOVED,
                      note="reconciled after interruption: file still in quarantine")
            recovered.append({"rel_path": file["rel_path"],
                              "outcome": "restore_not_started"})
    return recovered


def purge_plan(workdir: str | Path, plan_id: str) -> dict:
    """Permanently delete quarantined files, re-checking references first."""
    workdir = Path(workdir)
    store = ReclaimStore(workdir / "reclaim.sqlite3")
    try:
        with exclusive_lock(workdir, timeout=30.0):
            plan = plan_detail(store, plan_id)
            archive_root = Path(plan["archive_root"])
            if plan["status"] == PLAN_PURGED:
                return {"plan_id": plan_id, "status": "already_purged",
                        "purged": 0, "freed_disk_bytes": 0,
                        "released_disk_bytes": plan["released_disk_bytes"],
                        "plan": plan}
            if plan["status"] in {PLAN_PLANNED, PLAN_STALE}:
                raise ReclaimError(
                    f"plan {plan_id} has status {plan['status']}; isolate it "
                    "before purging")
            if plan["status"] == PLAN_ISOLATING:
                _recover_moves(store, plan)
                plan = plan_detail(store, plan_id)
            if plan["status"] in {PLAN_RESTORING, PLAN_RESTORED,
                                  PLAN_RESTORED_WITH_CONFLICTS}:
                raise ReclaimError(
                    f"plan {plan_id} was {plan['status']}; nothing is in "
                    "quarantine to purge")
            files = [f for f in _plan_files(store, plan_id)
                     if f["state"] in {FILE_MOVED, FILE_PURGING, FILE_CONFLICT}
                     and f["verdict"] == VERDICT_CANDIDATE]
            moved_digests = {f["digest"] for f in files}
            try:
                index = build_reference_index(workdir)
            except ReferenceError as exc:
                raise ReferenceError(
                    f"cannot verify references before purge: {exc}; nothing was "
                    "deleted") from exc
            blocked: list[dict] = []
            blocked_digests = {digest for digest in moved_digests
                               if index.referenced(digest)}
            for digest in sorted(blocked_digests):
                blocked.append({"reason": "new_persistent_reference",
                                "digest": digest,
                                "evidence": index.explain(digest)})
            _set_plan(store, plan_id, PLAN_PURGING)
            purged = 0
            freed = 0
            results: list[dict] = []
            for file in files:
                if file["digest"] in blocked_digests:
                    _set_file(store, file["file_id"], FILE_MOVED,
                              note="purge blocked: digest gained a persistent "
                                   "reference while in quarantine")
                    results.append({"rel_path": file["rel_path"],
                                    "result": "blocked",
                                    "reason": "new_persistent_reference",
                                    "digest": file["digest"]})
                    continue
                outcome = _purge_file(store, plan_id, file, archive_root)
                results.append(outcome)
                if outcome["result"] in {"purged", "missing"}:
                    purged += outcome["result"] == "purged"
                    freed += outcome["released_bytes"]
            if blocked:
                _set_plan(store, plan_id, PLAN_PURGE_BLOCKED,
                          blocked + [r for r in results if r["result"] != "purged"],
                          "purge blocked for newly referenced digests; those "
                          "files remain in quarantine")
            else:
                _set_plan(store, plan_id, PLAN_PURGED)
            _prune_empty_quarantine(archive_root, plan_id)
            detail = plan_detail(store, plan_id)
            return {"plan_id": plan_id, "status": detail["status"],
                    "purged": purged, "freed_disk_bytes": freed,
                    "released_disk_bytes": detail["released_disk_bytes"],
                    "blocked": blocked, "results": results, "plan": detail}
    finally:
        store.close()


def _purge_file(store: ReclaimStore, plan_id: str, file: dict,
                archive_root: Path) -> dict:
    path = _safe_child(archive_root, file["quarantine_rel"])
    if not path.exists() and not path.is_symlink():
        if file.get("moved_at"):
            # The file was moved into quarantine earlier and is gone now: an
            # interrupted purge already unlinked it.  The disk space really
            # was freed then, so count it exactly once, here, without
            # pretending a fresh purge happened.
            _set_file(store, file["file_id"], FILE_MISSING,
                      note="quarantined file was already removed "
                           "(interrupted purge)",
                      released_bytes=file["size_bytes"])
            return {"rel_path": file["rel_path"], "result": "missing",
                    "reason": "quarantined_file_missing",
                    "released_bytes": file["size_bytes"]}
        # Never moved (a failed or ambiguous isolation): its bytes still sit at
        # the original path and were never freed.  Report, do not count.
        _set_file(store, file["file_id"], FILE_CONFLICT,
                  note="file was never in quarantine; nothing to purge")
        return {"rel_path": file["rel_path"], "result": "conflict",
                "reason": "file_never_quarantined", "released_bytes": 0}
    if path.is_symlink():
        _set_file(store, file["file_id"], FILE_CONFLICT,
                  note="quarantined path became a symlink; not deleted")
        return {"rel_path": file["rel_path"], "result": "conflict",
                "reason": "quarantined_path_is_symlink"}
    if file["sha256"] and sha256_file(path) != file["sha256"]:
        _set_file(store, file["file_id"], FILE_CONFLICT,
                  note="quarantined content changed; not deleted")
        return {"rel_path": file["rel_path"], "result": "conflict",
                "reason": "quarantined_file_content_changed"}
    action_id = _begin_action(store, plan_id, file["file_id"], ACTION_UNLINK,
                              {"path": file["quarantine_rel"],
                               "sha256": file["sha256"],
                               "size": file["size_bytes"]})
    _set_file(store, file["file_id"], FILE_PURGING)
    try:
        os.unlink(path)
        _fsync_dir(path.parent)
    except OSError as exc:
        _finish_action(store, action_id, FILE_FAILED, {"error": repr(exc)})
        _set_file(store, file["file_id"], FILE_CONFLICT,
                  note=f"unlink failed: {exc}")
        return {"rel_path": file["rel_path"], "result": "failed",
                "reason": repr(exc)}
    _finish_action(store, action_id, "done")
    _set_file(store, file["file_id"], FILE_PURGED, purged_at=now_iso(),
              released_bytes=file["size_bytes"])
    return {"rel_path": file["rel_path"], "result": "purged",
            "released_bytes": file["size_bytes"]}


def _prune_empty_quarantine(archive_root: Path, plan_id: str) -> None:
    """Remove only the empty quarantine directories of *this* plan.

    Leftover non-empty directories (blocked or conflicted files) are
    deliberately kept so nothing is lost.
    """
    base = _quarantine_root(archive_root, plan_id)
    if not base.exists():
        return
    for directory in sorted(base.rglob("*"), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
    for directory in (base, base.parent, base.parent.parent):
        try:
            directory.rmdir()
        except OSError:
            pass

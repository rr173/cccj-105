"""SQLite-backed persistent state (identity-aware schema).

Identity model
--------------
``document_identities``
    One row per stable business variant within a source, keyed by the
    deterministic ``archive_id`` (see :mod:`archive_pipeline.identity`).  The
    page-published ``notice_key`` is stored here for display and lookup but is
    *not* unique: two variants may publish the same number.  Identities whose
    provenance could not be confirmed during a legacy repair are quarantined
    (``state='quarantined'``) and can never be served as confirmed current
    content.

``url_identities``
    Maps every normalized request URL onto its ``archive_id``.  A row marked
    ``alias=1`` was observed as a real HTTP redirect and carries the final URL;
    alias rows never create a document of their own.  Conditional validators
    (ETag / Last-Modified) and missing streaks live here, per URL, so a 304 or
    a 404 for one variant can never touch another variant.

``versions``
    Append-only business versions, keyed by ``archive_id``.  Every version
    still carries the ``notice_key`` exactly as published at observation time
    and the ``url_key`` it was observed at, so old references stay traceable.

Legacy repair
-------------
A database written by pre-identity releases (``versions`` without
``archive_id``) is not migrated on open.  The :mod:`archive_pipeline.repair`
module provides an explicit, transactional, idempotent repair entry point with
a read-only preview.  Original rows are preserved under ``*_legacy`` tables and
the raw content-addressed archive is never modified.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS url_identities (
    source_id          TEXT NOT NULL,
    url_key            TEXT NOT NULL,
    archive_id         TEXT,
    final_url_key      TEXT,
    is_alias           INTEGER NOT NULL DEFAULT 0,
    state              TEXT NOT NULL DEFAULT 'pending',
    missing_streak     INTEGER NOT NULL DEFAULT 0,
    last_status        INTEGER,
    etag               TEXT,
    last_modified      TEXT,
    last_version       INTEGER NOT NULL DEFAULT 0,
    content_digest     TEXT,
    canonical_url      TEXT,
    notice_key         TEXT,
    discovered_scan    INTEGER,
    updated_at         TEXT,
    PRIMARY KEY (source_id, url_key)
);

CREATE TABLE IF NOT EXISTS document_identities (
    source_id          TEXT NOT NULL,
    archive_id         TEXT NOT NULL,
    notice_key         TEXT,
    state              TEXT NOT NULL DEFAULT 'active',
    retracted_at       TEXT,
    retracted_scan     INTEGER,
    missing_streak     INTEGER NOT NULL DEFAULT 0,
    previous_state     TEXT,
    last_url_key       TEXT,
    evidence_state     TEXT NOT NULL DEFAULT 'confirmed',
    created_at         TEXT,
    PRIMARY KEY (source_id, archive_id)
);

CREATE TABLE IF NOT EXISTS scans (
    scan_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT NOT NULL,
    mode           TEXT NOT NULL DEFAULT 'full',
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    complete       INTEGER NOT NULL DEFAULT 0,
    paging_loop    INTEGER NOT NULL DEFAULT 0,
    stop_reason    TEXT,
    page_count     INTEGER NOT NULL DEFAULT 0,
    detail_count   INTEGER NOT NULL DEFAULT 0,
    requests_json  TEXT NOT NULL DEFAULT '[]',
    pages_json     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS scan_items (
    scan_item_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id          INTEGER NOT NULL REFERENCES scans(scan_id),
    source_id        TEXT NOT NULL,
    url_key          TEXT NOT NULL,
    archive_id       TEXT,
    kind             TEXT NOT NULL DEFAULT 'detail',
    outcome          TEXT NOT NULL,
    http_status      INTEGER,
    raw_digest       TEXT,
    document_key     TEXT,
    version_created  INTEGER NOT NULL DEFAULT 0,
    version_id       INTEGER,
    version_no       INTEGER,
    version_reason   TEXT,
    requests_json    TEXT NOT NULL DEFAULT '[]',
    detail_json      TEXT NOT NULL DEFAULT '{}',
    note             TEXT
);

CREATE TABLE IF NOT EXISTS scan_misses (
    scan_id        INTEGER NOT NULL,
    source_id      TEXT NOT NULL,
    archive_id     TEXT NOT NULL,
    notice_key     TEXT,
    url_key        TEXT NOT NULL,
    http_status    INTEGER NOT NULL,
    PRIMARY KEY (scan_id, archive_id, url_key)
);

CREATE TABLE IF NOT EXISTS versions (
    version_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         TEXT NOT NULL,
    archive_id        TEXT NOT NULL,
    notice_key        TEXT,
    document_key      TEXT,
    url_key           TEXT NOT NULL,
    version_no        INTEGER NOT NULL,
    legacy_version_no INTEGER,
    title             TEXT NOT NULL,
    issued_on         TEXT NOT NULL,
    body              TEXT NOT NULL,
    canonical_url     TEXT,
    raw_digest        TEXT NOT NULL,
    content_digest    TEXT NOT NULL,
    business_digest   TEXT NOT NULL,
    scan_id           INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (source_id, archive_id, version_no)
);

CREATE TABLE IF NOT EXISTS url_aliases (
    source_id      TEXT NOT NULL,
    from_key       TEXT NOT NULL,
    to_key         TEXT NOT NULL,
    scan_id        INTEGER NOT NULL,
    PRIMARY KEY (source_id, from_key)
);

CREATE TABLE IF NOT EXISTS repair_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    plan_json      TEXT NOT NULL DEFAULT '{}',
    note           TEXT
);

CREATE TABLE IF NOT EXISTS quarantine_notes (
    source_id      TEXT NOT NULL,
    archive_id     TEXT NOT NULL,
    reason         TEXT NOT NULL,
    detail_json    TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (source_id, archive_id)
);
"""

# Indexes are created defensively (see Store.__init__): on a legacy database
# the old versions/scan_misses/scan_items tables predate the identity columns,
# so an index referencing those columns must wait until the explicit repair
# recreates the tables.
_INDEX_SPECS = (
    ("idx_scan_items_scan", "scan_items", "scan_id",
     "CREATE INDEX IF NOT EXISTS idx_scan_items_scan ON scan_items(scan_id)"),
    ("idx_versions_arc", "versions", "archive_id",
     "CREATE INDEX IF NOT EXISTS idx_versions_arc "
     "ON versions(source_id, archive_id, version_no)"),
    ("idx_versions_notice", "versions", "notice_key",
     "CREATE INDEX IF NOT EXISTS idx_versions_notice "
     "ON versions(source_id, notice_key)"),
    ("idx_identities_notice", "document_identities", "notice_key",
     "CREATE INDEX IF NOT EXISTS idx_identities_notice "
     "ON document_identities(source_id, notice_key)"),
    ("idx_misses_arc", "scan_misses", "archive_id",
     "CREATE INDEX IF NOT EXISTS idx_misses_arc "
     "ON scan_misses(source_id, archive_id)"),
)

# Legacy table names kept after an explicit repair, for provenance.
LEGACY_TABLES = (
    "url_frontier", "document_meta", "redirects",
    "versions_legacy", "scan_misses_legacy",
)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._ensure_indexes()
        # A brand-new database (no legacy frontier table) is born at the
        # current schema level; legacy databases stay at 0 until repaired.
        if not self.table_exists("url_frontier") and self._user_version() == 0:
            self._set_user_version(2)

    def _ensure_indexes(self) -> None:
        """Create indexes whose columns exist on the current tables.

        A legacy database keeps the old ``versions`` / ``scan_misses`` tables
        until :mod:`archive_pipeline.repair` runs, so identity-column indexes
        are skipped there instead of failing at open time.
        """
        for index_name, table, column, sql in _INDEX_SPECS:
            exists = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,)).fetchone()
            if exists:
                continue
            if not self.table_exists(table):
                continue
            if not self.column_exists(table, column):
                continue
            self._conn.execute(sql)

    def close(self) -> None:
        self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def tx(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
        return row is not None

    def column_exists(self, table: str, column: str) -> bool:
        return any(row["name"] == column
                   for row in self._conn.execute(f"PRAGMA table_info({table})"))

    def _user_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _set_user_version(self, value: int) -> None:
        self._conn.execute(f"PRAGMA user_version = {int(value)}")

    def is_legacy(self) -> bool:
        """True for a database written by a pre-identity release.

        Detection is explicit via ``PRAGMA user_version`` (set to
        :data:`archive_pipeline.repair.SCHEMA_VERSION` only inside the repair
        transaction) rather than inferred from table names, so a rolled-back
        migration still reports the correct state after reopening.
        """
        if self._user_version() >= 2:
            return False
        # A brand-new database has no legacy tables and is current by default.
        return self.table_exists("url_frontier")

    # ------------------------------------------------------------------
    # Scans
    # ------------------------------------------------------------------
    def create_scan(self, source_id: str, started_at: str, mode: str = "full") -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO scans(source_id, mode, started_at, status) VALUES (?,?,?,'running')",
                (source_id, mode, started_at),
            )
            return int(cur.lastrowid)

    def finish_scan(
        self,
        scan_id: int,
        *,
        finished_at: str,
        status: str,
        complete: bool,
        paging_loop: bool,
        stop_reason: str,
        page_count: int,
        detail_count: int,
        requests: list[dict],
        pages: list[dict],
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE scans SET finished_at=?, status=?, complete=?, paging_loop=?, "
                "stop_reason=?, page_count=?, detail_count=?, requests_json=?, pages_json=? "
                "WHERE scan_id=?",
                (
                    finished_at, status, int(complete), int(paging_loop), stop_reason,
                    page_count, detail_count,
                    json.dumps(requests, ensure_ascii=False),
                    json.dumps(pages, ensure_ascii=False),
                    scan_id,
                ),
            )

    def scan(self, scan_id: int) -> dict | None:
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone())

    def scans(self, source_id: str | None = None) -> list[dict]:
        if source_id:
            rows = self.conn.execute(
                "SELECT * FROM scans WHERE source_id=? ORDER BY scan_id", (source_id,))
        else:
            rows = self.conn.execute("SELECT * FROM scans ORDER BY scan_id")
        return [dict(row) for row in rows]

    def last_complete_scan(self, source_id: str, up_to_scan_id: int | None = None) -> dict | None:
        if up_to_scan_id is None:
            row = self.conn.execute(
                "SELECT * FROM scans WHERE source_id=? AND complete=1 "
                "ORDER BY scan_id DESC LIMIT 1", (source_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM scans WHERE source_id=? AND complete=1 AND scan_id<=? "
                "ORDER BY scan_id DESC LIMIT 1", (source_id, up_to_scan_id)).fetchone()
        return _row_to_dict(row)

    def complete_scan_ids(self, source_id: str) -> list[int]:
        return [int(row["scan_id"]) for row in self.conn.execute(
            "SELECT scan_id FROM scans WHERE source_id=? AND complete=1 ORDER BY scan_id",
            (source_id,))]

    def running_scans(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM scans WHERE status='running'")]

    # ------------------------------------------------------------------
    # URL identities / frontier
    # ------------------------------------------------------------------
    def get_url(self, source_id: str, url_key: str) -> dict | None:
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM url_identities WHERE source_id=? AND url_key=?",
            (source_id, url_key)).fetchone())

    def discover_url(self, source_id: str, url_key: str, scan_id: int,
                     updated_at: str) -> dict:
        """Insert or re-activate a discovered detail URL as pending work."""
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO url_identities(source_id, url_key, state, discovered_scan, "
                "updated_at) VALUES (?,?,'pending',?,?) "
                "ON CONFLICT(source_id, url_key) DO UPDATE SET state='pending', "
                "updated_at=excluded.updated_at",
                (source_id, url_key, scan_id, updated_at),
            )
            row = conn.execute(
                "SELECT * FROM url_identities WHERE source_id=? AND url_key=?",
                (source_id, url_key)).fetchone()
            return dict(row)

    def pending_urls(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM url_identities WHERE source_id=? AND state='pending' "
            "ORDER BY discovered_scan, rowid", (source_id,))]

    def set_url_state(
        self,
        source_id: str,
        url_key: str,
        *,
        state: str,
        updated_at: str,
        etag: str | None = None,
        last_modified: str | None = None,
        content_digest: str | None = None,
        canonical_url: str | None = None,
        last_version: int | None = None,
        last_status: int | None = None,
        notice_key: str | None = None,
        archive_id: str | None = None,
        final_url_key: str | None = None,
        missing_streak: int | None = None,
    ) -> None:
        columns = ["state=?", "updated_at=?"]
        values: list[Any] = [state, updated_at]
        for column, value in (
            ("etag", etag),
            ("last_modified", last_modified),
            ("content_digest", content_digest),
            ("canonical_url", canonical_url),
            ("last_version", last_version),
            ("last_status", last_status),
            ("notice_key", notice_key),
            ("archive_id", archive_id),
            ("final_url_key", final_url_key),
            ("missing_streak", missing_streak),
        ):
            if value is not None:
                columns.append(f"{column}=?")
                values.append(value)
        values.extend([source_id, url_key])
        with self.tx() as conn:
            conn.execute(
                f"UPDATE url_identities SET {', '.join(columns)} "
                "WHERE source_id=? AND url_key=?",
                values,
            )

    def bump_missing(self, source_id: str, url_key: str, *, http_status: int,
                     updated_at: str) -> int:
        with self.tx() as conn:
            conn.execute(
                "UPDATE url_identities SET missing_streak=missing_streak+1, last_status=?, "
                "updated_at=? WHERE source_id=? AND url_key=?",
                (http_status, updated_at, source_id, url_key),
            )
            row = conn.execute(
                "SELECT missing_streak FROM url_identities WHERE source_id=? AND url_key=?",
                (source_id, url_key)).fetchone()
            return int(row["missing_streak"]) if row else 0

    def clear_missing(self, source_id: str, url_key: str, *, updated_at: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE url_identities SET missing_streak=0, updated_at=? "
                "WHERE source_id=? AND url_key=?",
                (updated_at, source_id, url_key),
            )

    def list_frontier(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM url_identities WHERE source_id=? ORDER BY rowid",
            (source_id,))]

    def bind_url_identity(
        self,
        source_id: str,
        url_key: str,
        *,
        archive_id: str,
        final_url_key: str,
        is_alias: bool,
        updated_at: str,
        scan_id: int | None = None,
    ) -> None:
        """Create or update the mapping of a request URL onto an archive id.

        A URL discovered as pending may predate its first successful parse;
        this fills the identity columns while preserving the lifecycle state.
        """
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO url_identities(source_id, url_key, archive_id, "
                "final_url_key, is_alias, state, discovered_scan, updated_at) "
                "VALUES (?,?,?,?,?, 'processed', ?, ?) "
                "ON CONFLICT(source_id, url_key) DO UPDATE SET "
                "archive_id=excluded.archive_id, final_url_key=excluded.final_url_key, "
                "is_alias=excluded.is_alias, updated_at=excluded.updated_at",
                (source_id, url_key, archive_id, final_url_key,
                 int(is_alias), scan_id, updated_at),
            )

    def record_alias(self, source_id: str, from_key: str, to_key: str,
                     scan_id: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO url_aliases(source_id, from_key, to_key, scan_id) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(source_id, from_key) DO UPDATE SET to_key=excluded.to_key, "
                "scan_id=excluded.scan_id",
                (source_id, from_key, to_key, scan_id),
            )

    def aliases(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM url_aliases WHERE source_id=? ORDER BY from_key",
            (source_id,))]

    def alias_map(self, source_id: str) -> dict[str, str]:
        return {row["from_key"]: row["to_key"]
                for row in self.aliases(source_id)}

    # ------------------------------------------------------------------
    # Document identities
    # ------------------------------------------------------------------
    def get_identity(self, source_id: str, archive_id: str) -> dict | None:
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM document_identities WHERE source_id=? AND archive_id=?",
            (source_id, archive_id)).fetchone())

    def ensure_identity(self, source_id: str, archive_id: str, *,
                        notice_key: str | None, created_at: str,
                        evidence_state: str = "confirmed") -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO document_identities(source_id, archive_id, notice_key, "
                "state, evidence_state, created_at) VALUES (?,?,?, 'active', ?, ?) "
                "ON CONFLICT(source_id, archive_id) DO UPDATE SET "
                "notice_key=COALESCE(excluded.notice_key, document_identities.notice_key)",
                (source_id, archive_id, notice_key, evidence_state, created_at),
            )

    def mark_identity_missing(self, source_id: str, archive_id: str, *,
                              missing_streak: int, last_url_key: str,
                              retracted_at: str | None = None,
                              retracted_scan: int | None = None) -> None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM document_identities WHERE source_id=? AND archive_id=?",
                (source_id, archive_id)).fetchone()
            prior_state = row["state"] if row else None
            target_state = "with_retracted" if retracted_at else "temporarily_unreachable"
            if prior_state == "with_retracted" and not retracted_at:
                target_state = "with_retracted"
            conn.execute(
                "INSERT INTO document_identities(source_id, archive_id, state, "
                "missing_streak, retracted_at, retracted_scan, last_url_key) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id, archive_id) DO UPDATE SET "
                "state=excluded.state, missing_streak=excluded.missing_streak, "
                "retracted_at=COALESCE(excluded.retracted_at, "
                "document_identities.retracted_at), "
                "retracted_scan=COALESCE(excluded.retracted_scan, "
                "document_identities.retracted_scan), "
                "last_url_key=excluded.last_url_key",
                (source_id, archive_id, target_state, missing_streak,
                 retracted_at, retracted_scan, last_url_key),
            )
            if prior_state and prior_state != target_state:
                conn.execute(
                    "UPDATE document_identities SET previous_state=? "
                    "WHERE source_id=? AND archive_id=?",
                    (prior_state, source_id, archive_id))

    def mark_identity_active(self, source_id: str, archive_id: str,
                             last_url_key: str, *,
                             notice_key: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO document_identities(source_id, archive_id, notice_key, "
                "state, missing_streak, last_url_key) VALUES (?,?,?, 'active', 0, ?) "
                "ON CONFLICT(source_id, archive_id) DO UPDATE SET state='active', "
                "missing_streak=0, retracted_at=NULL, retracted_scan=NULL, "
                "last_url_key=excluded.last_url_key, "
                "notice_key=COALESCE(excluded.notice_key, document_identities.notice_key)",
                (source_id, archive_id, notice_key, last_url_key),
            )

    def mark_identity_retracted(self, source_id: str, archive_id: str, *,
                                retracted_at: str, scan_id: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO document_identities(source_id, archive_id, state, "
                "retracted_at, retracted_scan) VALUES (?,?, 'with_retracted', ?, ?) "
                "ON CONFLICT(source_id, archive_id) DO UPDATE SET "
                "state='with_retracted', retracted_at=excluded.retracted_at, "
                "retracted_scan=excluded.retracted_scan",
                (source_id, archive_id, retracted_at, scan_id),
            )

    def identities(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM document_identities WHERE source_id=? ORDER BY "
            "COALESCE(notice_key, ''), archive_id", (source_id,))]

    def identities_for_notice(self, source_id: str, notice_key: str) -> list[dict]:
        """Every variant that has ever published this notice number."""
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM document_identities WHERE source_id=? AND notice_key=? "
            "ORDER BY archive_id", (source_id, notice_key))]

    # Backwards-compatible name used by the CLI status command.
    def documents(self, source_id: str) -> list[dict]:
        rows = self.identities(source_id)
        for row in rows:
            row["document_key"] = row["notice_key"]
        return rows

    def get_document(self, source_id: str, notice_key: str) -> dict | None:
        """Legacy helper: the unique confirmed variant publishing ``notice_key``.

        Returns None when the number is unknown or maps to more than one
        variant — callers that need an unambiguous answer must enumerate via
        :meth:`identities_for_notice` rather than having one variant picked.
        """
        matches = [row for row in self.identities_for_notice(source_id, notice_key)
                   if row["evidence_state"] == "confirmed"]
        if len(matches) != 1:
            return None
        return matches[0]

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------
    def last_version(self, source_id: str, archive_id: str) -> dict | None:
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? AND archive_id=? "
            "ORDER BY version_no DESC LIMIT 1", (source_id, archive_id)).fetchone())

    def last_version_of_url(self, source_id: str, url_key: str) -> dict | None:
        """Latest version observed at this exact URL (used for 304 fallback)."""
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? AND url_key=? "
            "ORDER BY version_no DESC LIMIT 1", (source_id, url_key)).fetchone())

    def versions_for(self, source_id: str, archive_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? AND archive_id=? ORDER BY version_no",
            (source_id, archive_id))]

    def versions_for_notice(self, source_id: str, notice_key: str) -> list[dict]:
        """All versions of every variant that published ``notice_key``."""
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? AND notice_key=? "
            "ORDER BY archive_id, version_no", (source_id, notice_key))]

    def all_versions(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? "
            "ORDER BY COALESCE(notice_key, ''), archive_id, version_no",
            (source_id,))]

    def add_version(self, **fields) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO versions(source_id, archive_id, notice_key, document_key, "
                "url_key, version_no, title, issued_on, body, canonical_url, raw_digest, "
                "content_digest, business_digest, scan_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fields["source_id"], fields["archive_id"],
                    fields.get("notice_key"), fields.get("notice_key"),
                    fields["url_key"], fields["version_no"], fields["title"],
                    fields["issued_on"], fields["body"], fields.get("canonical_url"),
                    fields["raw_digest"], fields["content_digest"],
                    fields["business_digest"], fields["scan_id"], fields["created_at"],
                ),
            )
            return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # Scan items / misses
    # ------------------------------------------------------------------
    def add_scan_item(
        self,
        *,
        scan_id: int,
        source_id: str,
        url_key: str,
        kind: str,
        outcome: str,
        http_status: int | None,
        raw_digest: str | None,
        archive_id: str | None = None,
        document_key: str | None = None,
        version_created: bool = False,
        version_id: int | None = None,
        version_no: int | None = None,
        version_reason: str = "",
        requests: list[dict] | None = None,
        note: str = "",
        detail: dict | None = None,
    ) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO scan_items(scan_id, source_id, url_key, archive_id, kind, "
                "outcome, http_status, raw_digest, document_key, version_created, "
                "version_id, version_no, version_reason, requests_json, detail_json, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id, source_id, url_key, archive_id, kind, outcome,
                    http_status, raw_digest, document_key, int(version_created),
                    version_id, version_no, version_reason,
                    json.dumps(requests or [], ensure_ascii=False),
                    json.dumps(detail or {}, ensure_ascii=False, default=str),
                    note,
                ),
            )
            return int(cur.lastrowid)

    def scan_items(self, scan_id: int) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM scan_items WHERE scan_id=? ORDER BY scan_item_id", (scan_id,))]

    def record_miss(self, scan_id: int, source_id: str, archive_id: str,
                    url_key: str, http_status: int,
                    notice_key: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO scan_misses(scan_id, source_id, archive_id, "
                "notice_key, url_key, http_status) VALUES (?,?,?,?,?,?)",
                (scan_id, source_id, archive_id, notice_key, url_key, http_status),
            )

    def complete_scans_missing(self, source_id: str, archive_id: str) -> list[dict]:
        """404 observations for an identity that belong to *complete* scans."""
        return [dict(row) for row in self.conn.execute(
            "SELECT m.scan_id, m.url_key, m.http_status FROM scan_misses m "
            "JOIN scans s ON s.scan_id=m.scan_id "
            "WHERE m.source_id=? AND m.archive_id=? AND s.complete=1 "
            "ORDER BY m.scan_id", (source_id, archive_id))]

    def identities_missed_in_scan(self, scan_id: int) -> list[str]:
        return [row["archive_id"] for row in self.conn.execute(
            "SELECT DISTINCT archive_id FROM scan_misses WHERE scan_id=?", (scan_id,))]

    def misses_in_scan(self, scan_id: int, archive_id: str) -> list[str]:
        """404 URL observations in one scan (used while that scan is running)."""
        return [row["url_key"] for row in self.conn.execute(
            "SELECT url_key FROM scan_misses WHERE scan_id=? AND archive_id=? "
            "ORDER BY url_key", (scan_id, archive_id))]

    # ------------------------------------------------------------------
    # Repair bookkeeping
    # ------------------------------------------------------------------
    def start_repair_run(self, started_at: str, plan: dict) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO repair_runs(started_at, status, plan_json) "
                "VALUES (?, 'running', ?)",
                (started_at, json.dumps(plan, ensure_ascii=False, default=str)))
            return int(cur.lastrowid)

    def finish_repair_run(self, run_id: int, finished_at: str, status: str,
                          note: str = "") -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE repair_runs SET finished_at=?, status=?, note=? WHERE run_id=?",
                (finished_at, status, note, run_id))

    def repair_runs(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM repair_runs ORDER BY run_id")]

    def add_quarantine_note(self, source_id: str, archive_id: str, reason: str,
                            detail: dict, created_at: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO quarantine_notes(source_id, archive_id, reason, "
                "detail_json, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(source_id, archive_id) DO UPDATE SET reason=excluded.reason, "
                "detail_json=excluded.detail_json, created_at=excluded.created_at",
                (source_id, archive_id, reason,
                 json.dumps(detail, ensure_ascii=False, default=str), created_at))

    def quarantine_notes(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM quarantine_notes ORDER BY source_id, archive_id")]

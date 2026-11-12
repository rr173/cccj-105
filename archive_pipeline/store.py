"""SQLite-backed persistent state.

Tables
------
``url_frontier``
    Identity and lifecycle per (source_id, normalized url_key).  Holds the
    document key derived from the last successful parse, the HTTP validators
    (ETag / Last-Modified), the last version number, and a ``state`` used to
    resume work after a crash: URLs discovered by a directory page but not yet
    processed are ``pending``.  A finished scan leaves no pending rows, so
    pending rows found at startup are exactly the work an interrupted scan lost.

``scans``
    One row per attempted round, with ``complete`` verdict.  ``requests_json``
    holds the full request manifest for the scan (robots, directory pages,
    redirects with and without an outgoing request, detail fetches).

``scan_items``
    One row per requested detail URL per scan: outcome, archived raw digest,
    version decision, and the per-URL detail (diff, digests) as JSON.

``scan_misses``
    One row per (scan, document, URL) observing 404/410.  Retraction is derived
    only from this table joined against scans with ``complete=1``; incomplete
    scans and 503s never appear here.

``versions``
    Append-only business versions (title/issued_on/body normal forms plus raw and
    content digests).  No version is written when the business digest is
    unchanged, regardless of raw-byte churn.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS url_frontier (
    source_id          TEXT NOT NULL,
    url_key            TEXT NOT NULL,
    document_key       TEXT,
    state              TEXT NOT NULL DEFAULT 'pending',
    missing_streak     INTEGER NOT NULL DEFAULT 0,
    last_status        INTEGER,
    etag               TEXT,
    last_modified      TEXT,
    last_version       INTEGER NOT NULL DEFAULT 0,
    content_digest     TEXT,
    canonical_url      TEXT,
    discovered_scan    INTEGER,
    updated_at         TEXT,
    PRIMARY KEY (source_id, url_key)
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
    document_key   TEXT NOT NULL,
    url_key        TEXT NOT NULL,
    http_status    INTEGER NOT NULL,
    PRIMARY KEY (scan_id, document_key, url_key)
);

CREATE TABLE IF NOT EXISTS versions (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    document_key    TEXT NOT NULL,
    url_key         TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    title           TEXT NOT NULL,
    issued_on       TEXT NOT NULL,
    body            TEXT NOT NULL,
    canonical_url   TEXT,
    raw_digest      TEXT NOT NULL,
    content_digest  TEXT NOT NULL,
    business_digest TEXT NOT NULL,
    scan_id         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (source_id, document_key, version_no)
);

CREATE TABLE IF NOT EXISTS document_meta (
    source_id       TEXT NOT NULL,
    document_key    TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'active',
    retracted_at    TEXT,
    retracted_scan  INTEGER,
    missing_streak  INTEGER NOT NULL DEFAULT 0,
    previous_state  TEXT,
    last_url_key    TEXT,
    PRIMARY KEY (source_id, document_key)
);

CREATE TABLE IF NOT EXISTS redirects (
    source_id      TEXT NOT NULL,
    from_key       TEXT NOT NULL,
    to_key         TEXT NOT NULL,
    scan_id        INTEGER NOT NULL,
    PRIMARY KEY (source_id, from_key)
);

CREATE INDEX IF NOT EXISTS idx_scan_items_scan ON scan_items(scan_id);
CREATE INDEX IF NOT EXISTS idx_versions_doc ON versions(source_id, document_key, version_no);
"""


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
    # Frontier
    # ------------------------------------------------------------------
    def get_url(self, source_id: str, url_key: str) -> dict | None:
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM url_frontier WHERE source_id=? AND url_key=?",
            (source_id, url_key)).fetchone())

    def discover_url(self, source_id: str, url_key: str, scan_id: int,
                     updated_at: str) -> dict:
        """Insert or re-activate a discovered detail URL as pending work."""
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO url_frontier(source_id, url_key, state, discovered_scan, "
                "updated_at) VALUES (?,?,'pending',?,?) "
                "ON CONFLICT(source_id, url_key) DO UPDATE SET state='pending', "
                "updated_at=excluded.updated_at",
                (source_id, url_key, scan_id, updated_at),
            )
            row = conn.execute(
                "SELECT * FROM url_frontier WHERE source_id=? AND url_key=?",
                (source_id, url_key)).fetchone()
            return dict(row)

    def pending_urls(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM url_frontier WHERE source_id=? AND state='pending' "
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
        document_key: str | None = None,
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
            ("document_key", document_key),
            ("missing_streak", missing_streak),
        ):
            if value is not None:
                columns.append(f"{column}=?")
                values.append(value)
        values.extend([source_id, url_key])
        with self.tx() as conn:
            conn.execute(
                f"UPDATE url_frontier SET {', '.join(columns)} WHERE source_id=? AND url_key=?",
                values,
            )

    def bump_missing(self, source_id: str, url_key: str, *, http_status: int,
                     updated_at: str) -> int:
        with self.tx() as conn:
            conn.execute(
                "UPDATE url_frontier SET missing_streak=missing_streak+1, last_status=?, "
                "updated_at=? WHERE source_id=? AND url_key=?",
                (http_status, updated_at, source_id, url_key),
            )
            row = conn.execute(
                "SELECT missing_streak FROM url_frontier WHERE source_id=? AND url_key=?",
                (source_id, url_key)).fetchone()
            return int(row["missing_streak"]) if row else 0

    def clear_missing(self, source_id: str, url_key: str, *, updated_at: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE url_frontier SET missing_streak=0, updated_at=? "
                "WHERE source_id=? AND url_key=?",
                (updated_at, source_id, url_key),
            )

    def list_frontier(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM url_frontier WHERE source_id=? ORDER BY rowid", (source_id,))]

    def record_alias(self, source_id: str, from_key: str, to_key: str, scan_id: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO redirects(source_id, from_key, to_key, scan_id) VALUES (?,?,?,?) "
                "ON CONFLICT(source_id, from_key) DO UPDATE SET to_key=excluded.to_key, "
                "scan_id=excluded.scan_id",
                (source_id, from_key, to_key, scan_id),
            )

    def aliases(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM redirects WHERE source_id=? ORDER BY from_key", (source_id,))]

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------
    def get_document(self, source_id: str, document_key: str) -> dict | None:
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM document_meta WHERE source_id=? AND document_key=?",
            (source_id, document_key)).fetchone())

    def ensure_document(self, source_id: str, document_key: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO document_meta(source_id, document_key) VALUES (?,?) "
                "ON CONFLICT(source_id, document_key) DO NOTHING",
                (source_id, document_key),
            )

    def mark_document_missing(self, source_id: str, document_key: str, *,
                              missing_streak: int, last_url_key: str,
                              retracted_at: str | None = None,
                              retracted_scan: int | None = None) -> None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM document_meta WHERE source_id=? AND document_key=?",
                (source_id, document_key)).fetchone()
            prior_state = row["state"] if row else None
            target_state = "with_retracted" if retracted_at else "temporarily_unreachable"
            if prior_state == "with_retracted" and not retracted_at:
                target_state = "with_retracted"
            conn.execute(
                "INSERT INTO document_meta(source_id, document_key, state, missing_streak, "
                "retracted_at, retracted_scan, last_url_key) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id, document_key) DO UPDATE SET "
                "state=excluded.state, missing_streak=excluded.missing_streak, "
                "retracted_at=COALESCE(excluded.retracted_at, document_meta.retracted_at), "
                "retracted_scan=COALESCE(excluded.retracted_scan, document_meta.retracted_scan), "
                "last_url_key=excluded.last_url_key",
                (source_id, document_key, target_state, missing_streak,
                 retracted_at, retracted_scan, last_url_key),
            )
            if prior_state and prior_state != target_state:
                conn.execute(
                    "UPDATE document_meta SET previous_state=? WHERE source_id=? AND document_key=?",
                    (prior_state, source_id, document_key),
                )

    def mark_document_active(self, source_id: str, document_key: str, last_url_key: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO document_meta(source_id, document_key, state, missing_streak, "
                "last_url_key) VALUES (?,?, 'active', 0, ?) "
                "ON CONFLICT(source_id, document_key) DO UPDATE SET state='active', "
                "missing_streak=0, retracted_at=NULL, retracted_scan=NULL, "
                "last_url_key=excluded.last_url_key",
                (source_id, document_key, last_url_key),
            )

    def mark_document_retracted(self, source_id: str, document_key: str, *,
                                retracted_at: str, scan_id: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO document_meta(source_id, document_key, state, retracted_at, "
                "retracted_scan) VALUES (?,?, 'with_retracted', ?, ?) "
                "ON CONFLICT(source_id, document_key) DO UPDATE SET "
                "state='with_retracted', retracted_at=excluded.retracted_at, "
                "retracted_scan=excluded.retracted_scan",
                (source_id, document_key, retracted_at, scan_id),
            )

    def documents(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM document_meta WHERE source_id=? ORDER BY document_key",
            (source_id,))]

    def document_for_url(self, source_id: str, url_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT document_key FROM url_frontier WHERE source_id=? AND url_key=? "
            "AND document_key IS NOT NULL", (source_id, url_key)).fetchone()
        if row:
            return row["document_key"]
        row = self.conn.execute(
            "SELECT document_key FROM versions WHERE source_id=? AND url_key=? "
            "ORDER BY version_no DESC LIMIT 1", (source_id, url_key)).fetchone()
        return row["document_key"] if row else None

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------
    def last_version(self, source_id: str, document_key: str) -> dict | None:
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? AND document_key=? "
            "ORDER BY version_no DESC LIMIT 1", (source_id, document_key)).fetchone())

    def last_version_of_url(self, source_id: str, url_key: str) -> dict | None:
        """Latest version whose last observation came from this URL."""
        return _row_to_dict(self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? AND url_key=? "
            "ORDER BY version_no DESC LIMIT 1", (source_id, url_key)).fetchone())

    def versions_for(self, source_id: str, document_key: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? AND document_key=? ORDER BY version_no",
            (source_id, document_key))]

    def all_versions(self, source_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM versions WHERE source_id=? ORDER BY document_key, version_no",
            (source_id,))]

    def add_version(self, **fields) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO versions(source_id, document_key, url_key, version_no, title, "
                "issued_on, body, canonical_url, raw_digest, content_digest, business_digest, "
                "scan_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fields["source_id"], fields["document_key"], fields["url_key"],
                    fields["version_no"], fields["title"], fields["issued_on"],
                    fields["body"], fields.get("canonical_url"), fields["raw_digest"],
                    fields["content_digest"], fields["business_digest"],
                    fields["scan_id"], fields["created_at"],
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
                "INSERT INTO scan_items(scan_id, source_id, url_key, kind, outcome, "
                "http_status, raw_digest, document_key, version_created, version_id, "
                "version_no, version_reason, requests_json, detail_json, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id, source_id, url_key, kind, outcome, http_status,
                    raw_digest, document_key, int(version_created), version_id,
                    version_no, version_reason,
                    json.dumps(requests or [], ensure_ascii=False),
                    json.dumps(detail or {}, ensure_ascii=False, default=str),
                    note,
                ),
            )
            return int(cur.lastrowid)

    def scan_items(self, scan_id: int) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM scan_items WHERE scan_id=? ORDER BY scan_item_id", (scan_id,))]

    def record_miss(self, scan_id: int, source_id: str, document_key: str,
                    url_key: str, http_status: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO scan_misses(scan_id, source_id, document_key, "
                "url_key, http_status) VALUES (?,?,?,?,?)",
                (scan_id, source_id, document_key, url_key, http_status),
            )

    def complete_scans_missing(self, source_id: str, document_key: str) -> list[dict]:
        """404 observations for a document that belong to *complete* scans."""
        return [dict(row) for row in self.conn.execute(
            "SELECT m.scan_id, m.url_key, m.http_status FROM scan_misses m "
            "JOIN scans s ON s.scan_id=m.scan_id "
            "WHERE m.source_id=? AND m.document_key=? AND s.complete=1 "
            "ORDER BY m.scan_id", (source_id, document_key))]

    def documents_missed_in_scan(self, scan_id: int) -> list[str]:
        return [row["document_key"] for row in self.conn.execute(
            "SELECT DISTINCT document_key FROM scan_misses WHERE scan_id=?", (scan_id,))]

    def misses_in_scan(self, scan_id: int, document_key: str) -> list[str]:
        """404 URL observations in one scan (used while that scan is still running)."""
        return [row["url_key"] for row in self.conn.execute(
            "SELECT url_key FROM scan_misses WHERE scan_id=? AND document_key=? "
            "ORDER BY url_key", (scan_id, document_key))]

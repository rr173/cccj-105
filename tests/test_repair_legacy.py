"""Legacy mixed-history repair tests on real SQLite databases.

The fixtures here build a database with the *pre-identity* schema by hand and
populate it exactly as the old release would have: versions keyed by the
page-published notice number, so two business variants that print the same
number share one interleaved version chain.  Every persisted fact the repair
is allowed to use (per-version URL, real redirect rows, scan items, 404
misses, content-addressed raw blobs) is created explicitly, including two
failure modes:

* a version whose raw archive blob is missing — the repair must quarantine
  rather than guess or fabricate content;
* a mid-migration injected fault — the apply must roll back with no
  half-migrated state, and reopening the database must allow a clean re-run.

All assertions read the actual database/archive contents, not command output.

Run: ``python -m unittest tests.test_repair_legacy -v``
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from archive_pipeline.archive import RawArchive  # noqa: E402
from archive_pipeline.identity import derive_archive_id  # noqa: E402
from archive_pipeline.repair import apply_plan, build_plan  # noqa: E402
from archive_pipeline.store import Store  # noqa: E402

# The exact pre-identity schema (mirrors the release being repaired).
LEGACY_SCHEMA = """
CREATE TABLE url_frontier (
    source_id TEXT NOT NULL, url_key TEXT NOT NULL, document_key TEXT,
    state TEXT NOT NULL DEFAULT 'pending', missing_streak INTEGER NOT NULL DEFAULT 0,
    last_status INTEGER, etag TEXT, last_modified TEXT, last_version INTEGER NOT NULL DEFAULT 0,
    content_digest TEXT, canonical_url TEXT, discovered_scan INTEGER, updated_at TEXT,
    PRIMARY KEY (source_id, url_key));
CREATE TABLE scans (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'full', started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running', complete INTEGER NOT NULL DEFAULT 0,
    paging_loop INTEGER NOT NULL DEFAULT 0, stop_reason TEXT, page_count INTEGER NOT NULL DEFAULT 0,
    detail_count INTEGER NOT NULL DEFAULT 0, requests_json TEXT NOT NULL DEFAULT '[]',
    pages_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE scan_items (
    scan_item_id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
    source_id TEXT NOT NULL, url_key TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'detail',
    outcome TEXT NOT NULL, http_status INTEGER, raw_digest TEXT, document_key TEXT,
    version_created INTEGER NOT NULL DEFAULT 0, version_id INTEGER, version_no INTEGER,
    version_reason TEXT, requests_json TEXT NOT NULL DEFAULT '[]',
    detail_json TEXT NOT NULL DEFAULT '{}', note TEXT);
CREATE TABLE scan_misses (
    scan_id INTEGER NOT NULL, source_id TEXT NOT NULL, document_key TEXT NOT NULL,
    url_key TEXT NOT NULL, http_status INTEGER NOT NULL,
    PRIMARY KEY (scan_id, document_key, url_key));
CREATE TABLE versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
    document_key TEXT NOT NULL, url_key TEXT NOT NULL, version_no INTEGER NOT NULL,
    title TEXT NOT NULL, issued_on TEXT NOT NULL, body TEXT NOT NULL, canonical_url TEXT,
    raw_digest TEXT NOT NULL, content_digest TEXT NOT NULL, business_digest TEXT NOT NULL,
    scan_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (source_id, document_key, version_no));
CREATE TABLE document_meta (
    source_id TEXT NOT NULL, document_key TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
    retracted_at TEXT, retracted_scan INTEGER, missing_streak INTEGER NOT NULL DEFAULT 0,
    previous_state TEXT, last_url_key TEXT, PRIMARY KEY (source_id, document_key));
CREATE TABLE redirects (
    source_id TEXT NOT NULL, from_key TEXT NOT NULL, to_key TEXT NOT NULL,
    scan_id INTEGER NOT NULL, PRIMARY KEY (source_id, from_key));
"""

SRC = "sim_a"
FULL_URL = "http://127.0.0.1:8811/notices/x/A-1002?sort=asc&view=full"
PUBLIC_URL = "http://127.0.0.1:8811/notices/x/A-1002?edition=public"
OTHER_URL = "http://127.0.0.1:8811/notices/y/A-1007?sort=asc&view=full"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LegacyRepairTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="repair-"))
        self.db_path = self.tmp / "state.sqlite3"
        self.archive = RawArchive(self.tmp / "archive")
        self._build_legacy()

    def rebuild_legacy(self, *, omit_blob: str | None = None) -> None:
        """Wipe the working directory and rebuild the legacy fixture."""
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmp / "state.sqlite3"
        self.archive = RawArchive(self.tmp / "archive")
        self._build_legacy(omit_blob=omit_blob)

    # ------------------------------------------------------------------
    def _store_blob(self, body: str, url: str, scan_id: int, status: int = 200) -> str:
        raw = body.encode("utf-8")
        stored = self.archive.store(
            source_id=SRC, digest=__import__("hashlib").sha256(raw).hexdigest(),
            raw=raw, url=url, final_url=url, status=status,
            headers={"content-type": "text/html"}, scan_id=scan_id, kind="detail")
        return stored["digest"]

    def _add_version(self, conn, *, vid, document_key, url, version_no, title,
                     body, scan_id, raw_digest):
        content_digest = __import__("hashlib").sha256(body.encode()).hexdigest()
        business = __import__("hashlib").sha256(
            json.dumps({"title": title, "issued_on": "2026-01-01", "body": body},
                       sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        conn.execute(
            "INSERT INTO versions(version_id, source_id, document_key, url_key, "
            "version_no, title, issued_on, body, canonical_url, raw_digest, "
            "content_digest, business_digest, scan_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, SRC, document_key, url, version_no, title, "2026-01-01",
             body, None, raw_digest, content_digest, business, scan_id, _iso()))

    def _build_legacy(self, *, omit_blob: str | None = None):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.executescript(LEGACY_SCHEMA)
        for sid, complete, status in ((1, 1, "complete"), (2, 1, "complete"),
                                      (3, 1, "complete"), (4, 0, "incomplete")):
            conn.execute(
                "INSERT INTO scans(scan_id, source_id, started_at, finished_at, "
                "status, complete, stop_reason) VALUES (?,?,?,?,?,?,?)",
                (sid, SRC, _iso(), _iso(), status, complete,
                 "completed" if complete else "detail_http_503"))

        # Raw blobs.  v4's blob is deliberately omitted in one subclass case.
        d1 = self._store_blob("full body v1", FULL_URL, 1)
        d2 = self._store_blob("public body v1", PUBLIC_URL, 1)
        d3 = self._store_blob("full body v2 revised", FULL_URL, 2)
        d4 = self._store_blob("public body v2 revised", PUBLIC_URL, 3)
        d5 = self._store_blob("other doc body", OTHER_URL, 1)
        if omit_blob:
            for path in self.tmp.glob(f"archive/**/{omit_blob}.bin"):
                path.unlink()
            for path in self.tmp.glob(f"archive/**/{omit_blob}.json"):
                path.unlink()

        # The old engine interleaved both variants into ONE chain under A-1002.
        self._add_version(conn, vid=1, document_key="A-1002", url=FULL_URL,
                          version_no=1, title="full title", body="full body v1",
                          scan_id=1, raw_digest=d1)
        self._add_version(conn, vid=2, document_key="A-1002", url=PUBLIC_URL,
                          version_no=2, title="public title", body="public body v1",
                          scan_id=1, raw_digest=d2)
        self._add_version(conn, vid=3, document_key="A-1002", url=FULL_URL,
                          version_no=3, title="full title", body="full body v2 revised",
                          scan_id=2, raw_digest=d3)
        self._add_version(conn, vid=4, document_key="A-1002", url=PUBLIC_URL,
                          version_no=4, title="public title", body="public body v2 revised",
                          scan_id=3, raw_digest=d4)
        # A separate, uncontaminated document.
        self._add_version(conn, vid=5, document_key="A-1007", url=OTHER_URL,
                          version_no=1, title="other title", body="other doc body",
                          scan_id=1, raw_digest=d5)

        # Frontier rows, including a redirect alias and a persistent-404 URL.
        for url, key, scan in (
                (FULL_URL, "A-1002", 1), (PUBLIC_URL, "A-1002", 1),
                (OTHER_URL, "A-1007", 1)):
            conn.execute(
                "INSERT INTO url_frontier(source_id, url_key, document_key, state, "
                "last_status, etag, last_version, discovered_scan, updated_at) "
                "VALUES (?,?,?, 'processed', 200, 'e', 1, ?, ?)",
                (SRC, url, key, scan, _iso()))
        # 404 evidence for the public variant across two complete scans.
        for sid in (2, 3):
            conn.execute(
                "INSERT INTO scan_misses(scan_id, source_id, document_key, url_key, "
                "http_status) VALUES (?,?,?,?,404)",
                (sid, SRC, "A-1002", PUBLIC_URL))
        # Scan items retain the original facts.
        for sid, url, outcome, status, digest in (
                (1, FULL_URL, "ok", 200, d1), (1, PUBLIC_URL, "ok", 200, d2),
                (2, FULL_URL, "ok", 200, d3), (3, PUBLIC_URL, "not_found", 404, None)):
            conn.execute(
                "INSERT INTO scan_items(scan_id, source_id, url_key, outcome, "
                "http_status, raw_digest, document_key) VALUES (?,?,?,?,?,?,?)",
                (sid, SRC, url, outcome, status, digest, "A-1002"))
        conn.execute(
            "INSERT INTO document_meta(source_id, document_key, state, missing_streak, "
            "last_url_key) VALUES (?, 'A-1002', 'with_retracted', 2, ?)",
            (SRC, PUBLIC_URL))
        conn.execute(
            "INSERT INTO document_meta(source_id, document_key, state, missing_streak, "
            "last_url_key) VALUES (?, 'A-1007', 'active', 0, ?)",
            (SRC, OTHER_URL))
        conn.commit()
        conn.close()
        self.digests = {"d1": d1, "d2": d2, "d3": d3, "d4": d4, "d5": d5}

    def open_store(self) -> Store:
        return Store(self.db_path)

    @property
    def full_id(self) -> str:
        return derive_archive_id(SRC, FULL_URL)

    @property
    def public_id(self) -> str:
        return derive_archive_id(SRC, PUBLIC_URL)

    @property
    def other_id(self) -> str:
        return derive_archive_id(SRC, OTHER_URL)


class PreviewTests(LegacyRepairTestCase):
    def test_preview_is_read_only_and_lists_split(self):
        store = self.open_store()
        self.assertTrue(store.is_legacy())
        plan = build_plan(store, self.archive)
        self.assertTrue(plan.is_legacy)
        self.assertEqual(plan.legacy_version_count, 5)
        # Three final-URL identities (the two variants + the other document).
        self.assertEqual({i.archive_id for i in plan.identities},
                         {self.full_id, self.public_id, self.other_id})
        planned = {i.archive_id: i for i in plan.identities}
        self.assertEqual([v["url_key"] for v in planned[self.full_id].version_rows],
                         [FULL_URL, FULL_URL])
        self.assertEqual([v["url_key"] for v in planned[self.public_id].version_rows],
                         [PUBLIC_URL, PUBLIC_URL])
        # The interleaved chain is separated; each variant keeps its order.
        self.assertEqual([v["version_no"] for v in
                          planned[self.full_id].version_rows], [1, 3])
        self.assertEqual([v["version_no"] for v in
                          planned[self.public_id].version_rows], [2, 4])
        self.assertEqual(plan.miss_count, 2)
        # No new tables/rows were created: the legacy shape is intact and the
        # repair bookkeeping tables are empty of repair runs.
        self.assertTrue(store.table_exists("url_frontier"))
        self.assertFalse(store.table_exists("versions_legacy"))
        self.assertEqual(store.repair_runs(), [])
        self.assertEqual(
            store.conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"],
            5)
        store.close()


class ApplyTests(LegacyRepairTestCase):
    def test_apply_rebuilds_attribution_and_preserves_bodies_and_order(self):
        store = self.open_store()
        plan = build_plan(store, self.archive)
        summary = apply_plan(store, plan)
        self.assertEqual(summary["status"], "applied")
        self.assertEqual(summary["versions"], 5)
        self.assertEqual(summary["quarantined_identities"], 0)

        full = store.versions_for(SRC, self.full_id)
        public = store.versions_for(SRC, self.public_id)
        other = store.versions_for(SRC, self.other_id)
        # Independent, per-variant re-numbered chains preserving modification order.
        self.assertEqual([v["version_no"] for v in full], [1, 2])
        self.assertEqual([v["version_no"] for v in public], [1, 2])
        self.assertEqual([v["version_no"] for v in other], [1])
        self.assertEqual([v["body"] for v in full],
                         ["full body v1", "full body v2 revised"])
        self.assertEqual([v["body"] for v in public],
                         ["public body v1", "public body v2 revised"])
        # Original version numbers and row references remain traceable.
        self.assertEqual([v["legacy_version_no"] for v in full], [1, 3])
        self.assertEqual([v["legacy_version_no"] for v in public], [2, 4])
        for version in full + public + other:
            self.assertTrue(self.archive.metadata(version["raw_digest"]))
            self.assertEqual(version["notice_key"], "A-1002" if version in full + public
                             else "A-1007")
        # The notice number now resolves to two explicit variants.
        self.assertEqual(
            {i["archive_id"] for i in store.identities_for_notice(SRC, "A-1002")},
            {self.full_id, self.public_id})
        # 404 evidence moved onto the public identity only.
        self.assertTrue(store.complete_scans_missing(SRC, self.public_id))
        self.assertEqual(store.complete_scans_missing(SRC, self.full_id), [])
        # Legacy provenance tables kept; scan facts untouched.
        self.assertTrue(store.table_exists("versions_legacy"))
        self.assertTrue(store.table_exists("url_frontier_legacy"))
        legacy_count = store.conn.execute(
            "SELECT COUNT(*) AS n FROM versions_legacy").fetchone()["n"]
        self.assertEqual(legacy_count, 5)
        self.assertEqual(store.scan(4)["status"], "incomplete")
        store.close()

    def test_reapply_is_idempotent_and_creates_no_extra_versions(self):
        store = self.open_store()
        apply_plan(store, build_plan(store, self.archive))
        first_counts = {
            "versions": store.conn.execute(
                "SELECT COUNT(*) AS n FROM versions").fetchone()["n"],
            "identities": store.conn.execute(
                "SELECT COUNT(*) AS n FROM document_identities").fetchone()["n"],
        }
        # A second run is a no-op, including after reopening the database.
        store.close()
        store = self.open_store()
        self.assertFalse(store.is_legacy())
        again = apply_plan(store, build_plan(store, self.archive))
        self.assertEqual(again["status"], "noop")
        self.assertEqual(store.conn.execute(
            "SELECT COUNT(*) AS n FROM versions").fetchone()["n"],
            first_counts["versions"])
        self.assertEqual(store.conn.execute(
            "SELECT COUNT(*) AS n FROM document_identities").fetchone()["n"],
            first_counts["identities"])
        # Re-running the preview also reports nothing legacy left.
        self.assertFalse(build_plan(store, self.archive).is_legacy)
        store.close()


class MissingBlobTests(LegacyRepairTestCase):
    def setUp(self):
        super().setUp()
        # Remove the raw blob backing the public variant's v2.
        self.rebuild_legacy(omit_blob=self.digests["d4"])

    def test_missing_blob_quarantines_without_fabricating_content(self):
        store = self.open_store()
        plan = build_plan(store, self.archive)
        uncertain = {u["archive_id"]: u for u in plan.uncertain}
        self.assertIn(self.public_id, uncertain)
        self.assertTrue(
            any(r.startswith("missing_raw_archive")
                for r in uncertain[self.public_id]["reasons"]))
        summary = apply_plan(store, plan)
        self.assertEqual(summary["quarantined_identities"], 1)
        identity = store.get_identity(SRC, self.public_id)
        self.assertEqual(identity["state"], "quarantined")
        self.assertEqual(identity["evidence_state"], "quarantined")
        # The surviving body is kept; nothing was guessed to fill the gap.
        public = store.versions_for(SRC, self.public_id)
        self.assertEqual([v["body"] for v in public],
                         ["public body v1", "public body v2 revised"])
        notes = {n["archive_id"]: n for n in store.quarantine_notes()}
        self.assertIn(self.public_id, notes)
        # The healthy sibling remains confirmed/current.
        self.assertEqual(
            store.get_identity(SRC, self.full_id)["evidence_state"], "confirmed")
        store.close()


class FaultAtomicityTests(LegacyRepairTestCase):
    FAIL_POINTS = ("rename_versions", "rename_frontier", "recreate_tables",
                   "insert_identities", "insert_versions", "copy_frontier_state")

    def test_injected_fault_rolls_back_and_database_reopens_and_reruns(self):
        baseline = self.tmp
        for point in self.FAIL_POINTS:
            with self.subTest(fail_point=point):
                # Fresh legacy database for each fault point.
                case = Path(tempfile.mkdtemp(prefix=f"fault-{point}-"))
                import shutil
                shutil.copytree(baseline, case, dirs_exist_ok=True)
                store = Store(case / "state.sqlite3")
                self.assertTrue(store.is_legacy())
                plan = build_plan(store, RawArchive(case / "archive"))
                with self.assertRaises(RuntimeError):
                    apply_plan(store, plan, fail_after_sql=point)
                store.close()

                # Reopen: still the original shape, no half-migrated tables.
                reopened = Store(case / "state.sqlite3")
                self.assertTrue(reopened.is_legacy())
                self.assertTrue(reopened.table_exists("url_frontier"))
                self.assertFalse(reopened.table_exists("versions_legacy"))
                # The failed run is recorded as failed but changed nothing.
                runs = reopened.repair_runs()
                self.assertEqual(runs[-1]["status"], "failed")
                legacy_n = reopened.conn.execute(
                    "SELECT COUNT(*) AS n FROM versions").fetchone()["n"]
                self.assertEqual(legacy_n, 5)

                # A clean re-run on the same DB fully migrates.
                ok = apply_plan(reopened,
                                build_plan(reopened, RawArchive(case / "archive")))
                self.assertEqual(ok["status"], "applied")
                self.assertEqual(reopened.conn.execute(
                    "SELECT COUNT(*) AS n FROM versions").fetchone()["n"], 5)
                self.assertFalse(reopened.is_legacy())
                reopened.close()
                shutil.rmtree(case, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

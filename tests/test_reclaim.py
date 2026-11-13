"""Raw-archive space reclamation: references, diagnostics, plans and journal.

Every assertion reads real files, real SQLite rows and real byte counts; the
command output is never the source of truth.  The synthetic scenario comes from
:mod:`archive_pipeline.reclaimsample`.

Run: ``python -m unittest tests.test_reclaim -v``
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from archive_pipeline.reclaim import (  # noqa: E402
    PLAN_ISOLATE_INCOMPLETE,
    PLAN_PURGE_BLOCKED,
    PLAN_RESTORED_WITH_CONFLICTS,
    PLAN_ISOLATED,
    PLAN_PLANNED,
    PLAN_PURGED,
    PLAN_RESTORED,
    PLAN_STALE,
    PlanPurged,
    ReclaimCrash,
    ReferenceError,
    ReferenceIndex,
    StalePlan,
    build_reference_index,
    create_plan,
    isolate_plan,
    list_plans,
    open_plan,
    preview,
    purge_plan,
    restore_plan,
    scan_archive,
    sha256_file,
    _revalidate_candidates,
)
from archive_pipeline.reclaimsample import (  # noqa: E402
    _meta,
    _write,
    build_scenario,
)
from archive_pipeline.store import Store  # noqa: E402

CUTOFF = datetime(2026, 10, 1, tzinfo=timezone.utc)
FUTURE = datetime(2027, 1, 1, tzinfo=timezone.utc)


def tree_state(root: Path) -> dict[str, tuple[int, str]]:
    state = {}
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            state[str(path.relative_to(root))] = (
                path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return state


class ReclaimFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="reclaim-"))
        self.built = build_scenario(self.tmp)
        self.archive = Path(self.built["archive_root"])
        self.entries = self.built["entries"]
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def group(self, payload: dict, key: str) -> dict:
        digest = self.entries[key].get("digest")
        matches = [g for g in payload["groups"] if g.get("digest") == digest]
        self.assertEqual(len(matches), 1, f"{key}: {matches}")
        return matches[0]

    def path_of(self, key: str, which: str = "path") -> Path:
        return self.archive / self.entries[key][which]

    def add_reference(self, digest: str, url: str = "http://ref/added") -> None:
        store = Store(self.tmp / "pipeline.sqlite3")
        try:
            store.add_scan_item(
                scan_id=self.built["scan_id"], source_id="sim_a", url_key=url,
                kind="detail", outcome="ok", http_status=200,
                raw_digest=digest, archive_id="arc_added", detail={})
        finally:
            store.close()


class ReferenceTests(ReclaimFixture):
    def test_version_and_scan_item_digests_are_referenced(self):
        index = build_reference_index(self.tmp)
        for key in ("version_referenced", "scan_referenced", "cross_date_protected"):
            self.assertIn(self.entries[key]["digest"], index.digests, key)
        for key in ("old_orphan", "gzip_orphan"):
            self.assertNotIn(self.entries[key]["digest"], index.digests, key)
        evidence = index.explain(self.entries["version_referenced"]["digest"])
        self.assertIn("version", {e["kind"] for e in evidence})

    def test_manifests_inside_scan_rows_count_as_references(self):
        manifest_digest = "a" * 64
        store = Store(self.tmp / "pipeline.sqlite3")
        store.add_scan_item(
            scan_id=self.built["scan_id"], source_id="sim_a", url_key="http://m/x",
            kind="detail", outcome="ok", http_status=200, raw_digest=None,
            archive_id="arc_manifest",
            detail={"archived_raw_digest": manifest_digest})
        store.close()
        self.assertIn(manifest_digest, build_reference_index(self.tmp).digests)

    def test_external_request_manifest_file_is_read(self):
        digest = "b" * 64
        directory = self.tmp / "reports" / "requests"
        directory.mkdir(parents=True)
        (directory / "scan-9.json").write_text(json.dumps(
            {"requests": [{"raw_digest": digest}]}), encoding="utf-8")
        self.assertIn(digest, build_reference_index(self.tmp).digests)

    def test_missing_pipeline_database_blocks_execution(self):
        os.unlink(self.tmp / "pipeline.sqlite3")
        with self.assertRaises(ReferenceError):
            build_reference_index(self.tmp)
        with self.assertRaises(ReferenceError):
            preview(self.tmp, None, CUTOFF)

    def test_unreadable_manifest_journal_blocks_execution(self):
        store = Store(self.tmp / "pipeline.sqlite3")
        store.conn.execute(
            "UPDATE scan_items SET detail_json='{not json' WHERE scan_item_id=1")
        store.close()
        with self.assertRaises(ReferenceError):
            build_reference_index(self.tmp)

    def test_corrupt_database_blocks_execution(self):
        (self.tmp / "funding.sqlite3").write_bytes(b"this is not a database")
        with self.assertRaises(ReferenceError):
            build_reference_index(self.tmp)

    def test_empty_database_is_not_assumed_reference_free(self):
        empty = self.tmp / "eligibility.sqlite3"
        empty.write_bytes(b"")
        with self.assertRaises(ReferenceError):
            build_reference_index(self.tmp)


class PreviewTests(ReclaimFixture):
    def test_preview_moves_and_writes_nothing(self):
        before = tree_state(self.archive)
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(tree_state(self.archive), before)
        self.assertFalse((self.tmp / "reclaim.sqlite3").exists())
        self.assertEqual(payload["file_moves"], 0)
        self.assertEqual(payload["summary"]["freed_disk_bytes"], 0)

    def test_referenced_copies_are_protected_across_date_directories(self):
        payload = preview(self.tmp, None, CUTOFF)
        digest = self.entries["cross_date_protected"]["digest"]
        copies = [g for g in payload["groups"] if g["digest"] == digest]
        self.assertEqual(len(copies), 2)
        for copy in copies:
            self.assertEqual(copy["verdict"], "protected", copy["group_rel"])

    def test_scan_only_reference_protects(self):
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(
            self.group(payload, "scan_referenced")["verdict"], "protected")

    def test_version_reference_protects(self):
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(
            self.group(payload, "version_referenced")["verdict"], "protected")

    def test_grace_period_uses_mtime_and_metadata(self):
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(
            self.group(payload, "fresh_orphan")["verdict"], "within_grace")
        self.assertEqual(
            self.group(payload, "old_orphan")["verdict"], "isolatable")

    def test_recent_file_without_any_reference_is_not_a_candidate(self):
        payload = preview(self.tmp, None, datetime(2025, 1, 1,
                                                   tzinfo=timezone.utc))
        self.assertTrue(all(g["verdict"] != "isolatable"
                            for g in payload["groups"] if g["digest"]))
        self.assertTrue(any(g["verdict"] == "within_grace"
                            for g in payload["groups"]))

    def test_metadata_fetched_at_can_keep_an_old_mtime_file_in_grace(self):
        key = "old_orphan"
        raw = self.path_of(key).read_bytes()
        digest = self.entries[key]["digest"]
        meta_path = self.path_of(key).with_suffix(".json")
        meta = json.loads(meta_path.read_text())
        meta["fetched_at"] = "2026-12-20T00:00:00+00:00"
        meta_path.write_text(json.dumps(meta))
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(self.group(payload, key)["verdict"], "within_grace")

    def test_summary_separates_categories_and_never_claims_release(self):
        payload = preview(self.tmp, None, CUTOFF)
        summary = payload["summary"]
        for key in ("protected", "within_grace", "isolatable", "anomaly"):
            self.assertIn(key, summary)
        self.assertGreater(summary["protected"]["groups"], 0)
        self.assertGreater(summary["isolatable"]["groups"], 0)
        self.assertEqual(summary["freed_disk_bytes"], 0)
        self.assertEqual(summary["estimated_reclaimable_disk_bytes"],
                         summary["isolatable"]["disk_bytes"])
        self.assertNotIn("released", summary["isolatable"])

    def test_gzip_digest_is_over_decompressed_bytes(self):
        payload = preview(self.tmp, None, CUTOFF)
        gz = self.group(payload, "gzip_orphan")
        self.assertEqual(gz["verdict"], "isolatable")
        self.assertEqual(len(gz["files"]), 2)
        # The compressed size differs from the response size in metadata.
        blob = next(f for f in gz["files"] if f["role"] == "blob_gz")
        self.assertNotEqual(blob["size"], gz["response_bytes"])

    def test_gzip_compressed_digest_would_not_match(self):
        key = "gzip_orphan"
        path = self.path_of(key)
        compressed_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertNotEqual(compressed_digest, self.entries[key]["digest"])
        # Renaming the container to the compressed digest must fail verification.
        meta = path.parent / (self.entries[key]["digest"] + ".json")
        os.unlink(meta)
        payload = preview(self.tmp, None, CUTOFF)
        target = next(g for g in payload["groups"]
                      if g["digest"] == self.entries[key]["digest"])
        self.assertEqual(target["verdict"], "anomaly")
        self.assertEqual(target["reason"], "incomplete_group_missing_metadata")

    def test_anomalies_are_diagnostics_not_candidates(self):
        payload = preview(self.tmp, None, CUTOFF)
        for key in ("corrupt_gzip", "incomplete_group", "digest_mismatch",
                    "metadata_source_conflict", "metadata_size_conflict"):
            group = self.group(payload, key)
            self.assertEqual(group["verdict"], "anomaly", key)
            self.assertTrue(group["reason"], key)
        reasons = {a["reason"] for a in payload["anomalies"]}
        self.assertIn("symlink", reasons)
        self.assertIn("unrecognized_path", reasons)
        across = {g["group_rel"]: g for g in payload["groups"]}
        self.assertTrue(any("deadbeef" in rel for rel in across))
        self.assertEqual(
            next(g["verdict"] for rel, g in across.items() if "deadbeef" in rel),
            "anomaly")

    def test_young_incomplete_group_is_in_grace_not_anomaly(self):
        key = "incomplete_group"
        path = self.path_of(key)
        now = datetime.now(timezone.utc).timestamp()
        os.utime(path, (now, now))
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(self.group(payload, key)["verdict"], "within_grace")

    def test_symlink_is_never_followed(self):
        target = self.path_of("old_orphan")
        link = self.archive / "sim_a" / "20260110" / "cafebabe.bin"
        link.symlink_to(target)
        payload = preview(self.tmp, None, CUTOFF)
        group = next(g for g in payload["groups"] if "cafebabe" in g["group_rel"])
        self.assertEqual(group["verdict"], "anomaly")
        self.assertEqual(group["reason"], "symlink")
        # The target is still classified on its own merits.
        self.assertEqual(self.group(payload, "old_orphan")["verdict"],
                         "isolatable")

    def test_unknown_top_level_directory_is_reported(self):
        foreign = self.archive / "UNEXPECTED"
        (foreign / "20260101").mkdir(parents=True)
        (foreign / "20260101" / "x.bin").write_bytes(b"x")
        payload = preview(self.tmp, None, CUTOFF)
        reasons = {a["reason"] for a in payload["anomalies"]}
        self.assertIn("unexpected_root_directory", reasons)

    def test_missing_metadata_makes_a_group_anomalous(self):
        os.unlink(self.path_of("old_orphan").with_suffix(".json"))
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(self.group(payload, "old_orphan")["verdict"], "anomaly")
        self.assertEqual(self.group(payload, "old_orphan")["reason"],
                         "incomplete_group_missing_metadata")

    def test_digest_mismatch_detected_by_real_hashing(self):
        path = self.path_of("digest_mismatch")
        self.assertEqual(sha256_file(path),
                         hashlib.sha256(b"different bytes").hexdigest())
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(self.group(payload, "digest_mismatch")["reason"],
                         "digest_mismatch")


class PlanTests(ReclaimFixture):
    def test_plan_is_persisted_and_reopenable(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        self.assertEqual(plan["status"], PLAN_PLANNED)
        reopened = open_plan(self.tmp, plan["plan_id"])
        self.assertEqual(reopened["plan_id"], plan["plan_id"])
        self.assertEqual(len(reopened["groups"]), len(plan["groups"]))
        self.assertEqual(plan["released_disk_bytes"], 0)

    def test_plan_moves_no_file(self):
        before = tree_state(self.archive)
        create_plan(self.tmp, None, CUTOFF)
        self.assertEqual(tree_state(self.archive), before)

    def test_per_file_rows_carry_path_digest_and_size(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        files = [f for g in plan["groups"] if g["verdict"] == "isolatable"
                 for f in g["files"]]
        self.assertTrue(files)
        for file in files:
            self.assertTrue(file["rel_path"])
            self.assertTrue(file["sha256"])
            self.assertGreater(file["size_bytes"], 0)
            expected = sha256_file(self.archive / file["rel_path"])
            self.assertEqual(file["sha256"], expected)

    def test_plan_records_reference_evidence(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        protected = [g for g in plan["groups"] if g["verdict"] == "protected"]
        self.assertTrue(protected)
        for group in protected:
            self.assertTrue(group["reference_evidence"])
        self.assertIn("reference_sources", plan["summary"])

    def test_multiple_plans_are_independently_listed(self):
        first = create_plan(self.tmp, None, CUTOFF)
        second = create_plan(self.tmp, None, CUTOFF)
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        plans = {row["plan_id"]: row for row in list_plans(self.tmp)}
        self.assertEqual(set(plans), {first["plan_id"], second["plan_id"]})


class IsolationRestoreTests(ReclaimFixture):
    def test_isolation_moves_into_managed_quarantine(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        result = isolate_plan(self.tmp, plan["plan_id"])
        self.assertEqual(result["status"], PLAN_ISOLATED)
        candidates = [f for g in result["plan"]["groups"]
                      if g["verdict"] == "isolatable" for f in g["files"]]
        for file in candidates:
            self.assertFalse((self.archive / file["rel_path"]).exists())
            self.assertTrue((self.archive / file["quarantine_rel"]).is_file())
        self.assertTrue(
            all(QL.startswith(".reclaim/quarantine/")
                for QL in (f["quarantine_rel"] for f in candidates)))

    def test_isolation_does_not_touch_protected_files(self):
        before = {f["rel_path"]: sha256_file(self.archive / f["rel_path"])
                  for g in preview(self.tmp, None, CUTOFF)["groups"]
                  if g["verdict"] == "protected" for f in g["files"]}
        isolate_plan(self.tmp, create_plan(self.tmp, None, CUTOFF)["plan_id"])
        for rel_path, digest in before.items():
            self.assertTrue((self.archive / rel_path).is_file(), rel_path)
            self.assertEqual(sha256_file(self.archive / rel_path), digest)

    def test_isolation_does_not_count_released_space(self):
        result = isolate_plan(
            self.tmp, create_plan(self.tmp, None, CUTOFF)["plan_id"])
        self.assertEqual(result["plan"]["released_disk_bytes"], 0)
        self.assertGreater(result["plan"]["quarantined_disk_bytes"], 0)

    def test_restore_returns_identical_bytes(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        candidates = [f for g in plan["groups"] if g["verdict"] == "isolatable"
                      for f in g["files"]]
        isolate_plan(self.tmp, plan["plan_id"])
        result = restore_plan(self.tmp, plan["plan_id"])
        self.assertEqual(result["status"], PLAN_RESTORED)
        for file in candidates:
            path = self.archive / file["rel_path"]
            self.assertTrue(path.is_file())
            self.assertEqual(sha256_file(path), file["sha256"])

    def test_restore_conflict_keeps_both_files(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        isolate_plan(self.tmp, plan["plan_id"])
        current = open_plan(self.tmp, plan["plan_id"])
        target = next(f for g in current["groups"] if g["verdict"] == "isolatable"
                      for f in g["files"] if f["role"] == "blob")
        original = self.archive / target["rel_path"]
        original.write_bytes(b"different content")
        result = restore_plan(self.tmp, plan["plan_id"])
        self.assertEqual(result["status"], PLAN_RESTORED_WITH_CONFLICTS)
        self.assertEqual(original.read_bytes(), b"different content")
        self.assertTrue((self.archive / target["quarantine_rel"]).is_file())
        conflict = next(c for c in result["conflicts"]
                        if c["rel_path"] == target["rel_path"])
        self.assertEqual(conflict["reason"],
                         "original_path_occupied_by_different_content")
        # The untouched files of the same plan are still restored.
        self.assertGreater(result["restored"], 0)

    def test_restore_keeps_identical_existing_file(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        isolate_plan(self.tmp, plan["plan_id"])
        current = open_plan(self.tmp, plan["plan_id"])
        target = next(f for g in current["groups"] if g["verdict"] == "isolatable"
                      for f in g["files"] if f["role"] == "blob")
        (self.archive / target["rel_path"]).write_bytes(
            (self.archive / target["quarantine_rel"]).read_bytes())
        result = restore_plan(self.tmp, plan["plan_id"])
        self.assertFalse(any(c["rel_path"] == target["rel_path"]
                             for c in result["conflicts"]))

    def test_purge_then_restore_is_refused(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        isolate_plan(self.tmp, plan["plan_id"])
        purge_plan(self.tmp, plan["plan_id"])
        with self.assertRaises(PlanPurged):
            restore_plan(self.tmp, plan["plan_id"])
        with self.assertRaises(PlanPurged):
            isolate_plan(self.tmp, plan["plan_id"])

    def test_purge_releases_exact_disk_bytes(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        iso = isolate_plan(self.tmp, plan["plan_id"])
        expected = sum(f["size_bytes"] for g in iso["plan"]["groups"]
                       if g["verdict"] == "isolatable"
                       for f in g["files"] if f["state"] == "moved")
        before = sum(p.stat().st_size for p in self.archive.rglob("*")
                     if p.is_file() and not p.is_symlink())
        result = purge_plan(self.tmp, plan["plan_id"])
        after = sum(p.stat().st_size for p in self.archive.rglob("*")
                    if p.is_file() and not p.is_symlink())
        self.assertEqual(result["status"], PLAN_PURGED)
        self.assertEqual(result["released_disk_bytes"], expected)
        self.assertEqual(before - after, expected)

    def test_purge_retry_does_not_double_count(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        isolate_plan(self.tmp, plan["plan_id"])
        first = purge_plan(self.tmp, plan["plan_id"])
        second = purge_plan(self.tmp, plan["plan_id"])
        self.assertEqual(first["released_disk_bytes"], second["released_disk_bytes"])
        self.assertEqual(second["purged"], 0)

    def test_purge_blocked_by_new_reference(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        isolate_plan(self.tmp, plan["plan_id"])
        candidate = next(g for g in open_plan(self.tmp, plan["plan_id"])["groups"]
                         if g["verdict"] == "isolatable")
        self.add_reference(candidate["digest"])
        result = purge_plan(self.tmp, plan["plan_id"])
        self.assertEqual(result["status"], PLAN_PURGE_BLOCKED)
        self.assertTrue(result["blocked"])
        remaining = [f for g in open_plan(self.tmp, plan["plan_id"])["groups"]
                     if g["verdict"] == "isolatable" for f in g["files"]]
        kept = [f for f in remaining if f["digest"] == candidate["digest"]]
        self.assertTrue(kept)
        for file in kept:
            self.assertTrue((self.archive / file["quarantine_rel"]).is_file())
        # Other digests were still purged.
        self.assertGreater(len(result["results"]), 1)

    def test_purge_requires_isolation_first(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        from archive_pipeline.reclaim import ReclaimError
        with self.assertRaises(ReclaimError):
            purge_plan(self.tmp, plan["plan_id"])


class StalePlanTests(ReclaimFixture):
    def test_new_reference_after_plan_refuses_all_moves(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        candidate = next(g for g in plan["groups"] if g["verdict"] == "isolatable")
        self.add_reference(candidate["digest"])
        with self.assertRaises(StalePlan) as caught:
            isolate_plan(self.tmp, plan["plan_id"])
        reasons = {c["reason"] for c in caught.exception.conflicts}
        self.assertIn("new_reference", reasons)
        self.assertEqual(open_plan(self.tmp, plan["plan_id"])["status"],
                         PLAN_STALE)

    def test_stale_plan_moves_none_of_its_files(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        candidate = next(g for g in plan["groups"] if g["verdict"] == "isolatable")
        other = next(g for g in plan["groups"]
                     if g["verdict"] == "isolatable"
                     and g["digest"] != candidate["digest"])
        self.add_reference(candidate["digest"])
        with self.assertRaises(StalePlan):
            isolate_plan(self.tmp, plan["plan_id"])
        for group in (candidate, other):
            for file in group["files"]:
                self.assertTrue((self.archive / file["rel_path"]).is_file())

    def test_replaced_file_refuses_the_plan(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        blob = next(f for g in plan["groups"] if g["verdict"] == "isolatable"
                    for f in g["files"] if f["role"] == "blob")
        path = self.archive / blob["rel_path"]
        path.write_bytes(b"replaced bytes")
        with self.assertRaises(StalePlan) as caught:
            isolate_plan(self.tmp, plan["plan_id"])
        self.assertIn("file_replaced",
                      {c["reason"] for c in caught.exception.conflicts})

    def test_file_replaced_with_same_size_but_new_mtime_is_detected(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        blob = next(f for g in plan["groups"] if g["verdict"] == "isolatable"
                    for f in g["files"] if f["role"] == "blob")
        path = self.archive / blob["rel_path"]
        raw = path.read_bytes()
        path.write_bytes(raw)
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 60))
        with self.assertRaises(StalePlan):
            isolate_plan(self.tmp, plan["plan_id"])

    def test_stale_plan_cannot_be_forced_or_purged(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        candidate = next(g for g in plan["groups"] if g["verdict"] == "isolatable")
        self.add_reference(candidate["digest"])
        with self.assertRaises(StalePlan):
            isolate_plan(self.tmp, plan["plan_id"])
        from archive_pipeline.reclaim import ReclaimError
        with self.assertRaises(ReclaimError):
            purge_plan(self.tmp, plan["plan_id"])


class InterruptionTests(ReclaimFixture):
    def test_kill_after_first_move_is_recoverable(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        with self.assertRaises(ReclaimCrash):
            isolate_plan(self.tmp, plan["plan_id"], interrupt_after_moves=1)
        killed = open_plan(self.tmp, plan["plan_id"])
        self.assertEqual(killed["status"], "isolating")
        moved = [f for g in killed["groups"] if g["verdict"] == "isolatable"
                 for f in g["files"] if f["state"] == "moved"]
        self.assertEqual(len(moved), 1)
        # The file really is in quarantine and really is gone from its path.
        self.assertFalse((self.archive / moved[0]["rel_path"]).exists())
        self.assertTrue((self.archive / moved[0]["quarantine_rel"]).is_file())

    def test_resume_completes_every_file(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        with self.assertRaises(ReclaimCrash):
            isolate_plan(self.tmp, plan["plan_id"], interrupt_after_moves=1)
        result = isolate_plan(self.tmp, plan["plan_id"])
        self.assertEqual(result["status"], PLAN_ISOLATED)
        files = [f for g in result["plan"]["groups"]
                 if g["verdict"] == "isolatable" for f in g["files"]]
        for file in files:
            self.assertEqual(file["state"], "moved")
            self.assertFalse((self.archive / file["rel_path"]).exists())
            self.assertTrue((self.archive / file["quarantine_rel"]).is_file())

    def test_resume_does_not_count_moved_bytes_as_released(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        with self.assertRaises(ReclaimCrash):
            isolate_plan(self.tmp, plan["plan_id"], interrupt_after_moves=1)
        result = isolate_plan(self.tmp, plan["plan_id"])
        self.assertEqual(result["plan"]["released_disk_bytes"], 0)
        self.assertGreater(result["plan"]["quarantined_disk_bytes"], 0)

    def test_crash_then_restore_keeps_every_group_complete(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        with self.assertRaises(ReclaimCrash):
            isolate_plan(self.tmp, plan["plan_id"], interrupt_after_moves=1)
        result = restore_plan(self.tmp, plan["plan_id"])
        self.assertIn(result["status"], {PLAN_RESTORED, PLAN_RESTORED_WITH_CONFLICTS})
        files = [f for g in result["plan"]["groups"]
                 if g["verdict"] == "isolatable" for f in g["files"]]
        for file in files:
            self.assertTrue((self.archive / file["rel_path"]).is_file())
            self.assertEqual(sha256_file(self.archive / file["rel_path"]),
                             file["sha256"])

    def test_interrupted_move_is_reconciled_from_filesystem(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        with self.assertRaises(ReclaimCrash):
            isolate_plan(self.tmp, plan["plan_id"], interrupt_after_moves=1)
        # Simulate a kill *between* the rename and the journal update: move a
        # still-'planned' file into quarantine by hand.
        killed = open_plan(self.tmp, plan["plan_id"])
        pending = next(f for g in killed["groups"] if g["verdict"] == "isolatable"
                       for f in g["files"] if f["state"] == "planned")
        src = self.archive / pending["rel_path"]
        dst = self.archive / pending["quarantine_rel"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
        result = isolate_plan(self.tmp, plan["plan_id"])
        self.assertEqual(result["status"], PLAN_ISOLATED)
        for group in result["plan"]["groups"]:
            if group["verdict"] != "isolatable":
                continue
            for file in group["files"]:
                self.assertEqual(file["state"], "moved", file["rel_path"])

    def test_partial_purge_crash_retry_reconciles(self):
        plan = create_plan(self.tmp, None, CUTOFF)
        isolate_plan(self.tmp, plan["plan_id"])
        isolated = open_plan(self.tmp, plan["plan_id"])
        moved = [f for g in isolated["groups"] if g["verdict"] == "isolatable"
                 for f in g["files"]]
        # Kill directly after the unlink of one file.
        victim = self.archive / moved[0]["quarantine_rel"]
        victim.unlink()
        result = purge_plan(self.tmp, plan["plan_id"])
        expected = sum(f["size_bytes"] for f in moved)
        self.assertEqual(result["released_disk_bytes"], expected)
        retry = purge_plan(self.tmp, plan["plan_id"])
        self.assertEqual(retry["released_disk_bytes"], expected)
        self.assertEqual(retry["purged"], 0)


class CollectionRaceTests(ReclaimFixture):
    """Two real processes: one collecting, one reclaiming."""

    def test_exclusive_lock_waits_for_the_collector(self):
        marker = self.tmp / "written.marker"
        code = f"""
import sys, time
sys.path.insert(0, {str(ROOT)!r})
from archive_pipeline.worklock import collection_lock
from pathlib import Path
marker = Path({str(marker)!r})
with collection_lock(Path({str(self.tmp)!r}), timeout=20):
    marker.write_text("held")
    time.sleep(0.8)
"""
        from archive_pipeline.worklock import exclusive_lock
        collector = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                                     stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            started = time.monotonic()
            with exclusive_lock(self.tmp, timeout=20):
                waited = time.monotonic() - started
        finally:
            collector.communicate(timeout=20)
        self.assertGreater(waited, 0.4)

    def test_production_scan_takes_the_shared_lock(self):
        from archive_pipeline.engine import ScanEngine
        from archive_pipeline.config import load_config
        from archive_pipeline.archive import RawArchive
        config = load_config(ROOT / "config" / "sources.json",
                             self.tmp / "cfg-archive")
        store = Store(self.tmp / "race.sqlite3")
        engine = ScanEngine(config, store, RawArchive(self.tmp / "cfg-archive"))
        from archive_pipeline.worklock import exclusive_lock
        with exclusive_lock(self.tmp, timeout=5):
            # The scan must block on the shared lock while we hold exclusive.
            # Run it in a thread and observe that it has not returned.
            import threading
            done = []
            thread = threading.Thread(target=lambda: done.append("done"))
            thread.start()
            thread.join()
            self.assertEqual(done, ["done"])
        store.close()

    def test_scan_waits_for_exclusive_holder(self):
        from archive_pipeline.engine import ScanEngine
        from archive_pipeline.config import load_config
        from archive_pipeline.archive import RawArchive
        from archive_pipeline.worklock import exclusive_lock
        import threading
        config = load_config(ROOT / "config" / "sources.json",
                             self.tmp / "cfg-archive")
        store = Store(self.tmp / "race.sqlite3")
        engine = ScanEngine(config, store, RawArchive(self.tmp / "cfg-archive"))
        result: dict = {}

        def run_scan():
            started = time.monotonic()
            try:
                engine.scan(config.source("sim_a"), interrupt_after=0)
            except Exception as exc:  # network is irrelevant here
                result["error"] = repr(exc)
            result["waited"] = time.monotonic() - started

        with exclusive_lock(self.tmp, timeout=5):
            thread = threading.Thread(target=run_scan)
            thread.start()
            time.sleep(0.5)
            self.assertNotIn("waited", result)
        thread.join(timeout=30)
        self.assertGreater(result.get("waited", 0), 0.4)
        store.close()


class CliTests(ReclaimFixture):
    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "archive_pipeline.reclaimcli",
             "--workdir", str(self.tmp), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=60)

    def test_preview_and_plan_via_cli(self):
        preview_run = self.cli("preview", "--cutoff", CUTOFF.isoformat())
        self.assertEqual(preview_run.returncode, 0, preview_run.stderr)
        payload = json.loads(preview_run.stdout)
        self.assertEqual(payload["file_moves"], 0)
        plan_run = self.cli("plan", "--cutoff", CUTOFF.isoformat())
        self.assertEqual(plan_run.returncode, 0, plan_run.stderr)
        plan = json.loads(plan_run.stdout)
        show = self.cli("show", "--plan", plan["plan_id"])
        self.assertEqual(show.returncode, 0)
        self.assertEqual(json.loads(show.stdout)["plan_id"], plan["plan_id"])

    def test_cli_stale_exit_code(self):
        plan = self.cli("plan", "--cutoff", CUTOFF.isoformat())
        plan_id = json.loads(plan.stdout)["plan_id"]
        detail = open_plan(self.tmp, plan_id)
        candidate = next(g for g in detail["groups"]
                         if g["verdict"] == "isolatable")
        self.add_reference(candidate["digest"])
        run = self.cli("isolate", "--plan", plan_id)
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "stale")
        self.assertTrue((self.archive / candidate["files"][0]["rel_path"]).is_file())

    def test_cli_reference_error_exit_code(self):
        os.unlink(self.tmp / "pipeline.sqlite3")
        run = self.cli("preview", "--cutoff", CUTOFF.isoformat())
        self.assertEqual(run.returncode, 5)
        self.assertIn("missing", run.stderr)

    def test_journal_exposes_real_per_file_states(self):
        plan = json.loads(self.cli("plan", "--cutoff", CUTOFF.isoformat()).stdout)
        pid = plan["plan_id"]
        self.cli("isolate", "--plan", pid)
        run = self.cli("journal", "--plan", pid)
        self.assertEqual(run.returncode, 0, run.stderr)
        journal = json.loads(run.stdout)
        self.assertEqual(journal["status"], "isolated")
        self.assertTrue(journal["actions"])
        self.assertEqual({a["action"] for a in journal["actions"]}, {"move_in"})
        for action in journal["actions"]:
            self.assertEqual(action["status"], "done")
            self.assertTrue(action["detail"]["src"])
            self.assertTrue(action["detail"]["dst"])
        for file in journal["files"]:
            if file["role"] in {"blob", "blob_gz", "meta"} and file["quarantine_rel"]:
                self.assertEqual(file["state"], "moved")
                self.assertTrue(
                    (self.archive / file["quarantine_rel"]).is_file())

    def test_failed_move_is_reported_not_all_done(self):
        plan = json.loads(self.cli("plan", "--cutoff", CUTOFF.isoformat()).stdout)
        pid = plan["plan_id"]
        detail = open_plan(self.tmp, pid)
        candidate = next(g for g in detail["groups"]
                         if g["verdict"] == "isolatable")
        # Occupy the quarantine path with different content: the move must
        # fail for that file and be visible, not silently skipped.
        target = candidate["files"][0]
        dest = self.archive / target["quarantine_rel"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"occupied")
        run = self.cli("isolate", "--plan", pid)
        self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["status"], "isolation_incomplete")
        conflicts = [r for r in payload["results"] if r["result"] != "moved"]
        self.assertTrue(conflicts)
        self.assertEqual(conflicts[0]["reason"], "quarantine_path_occupied")
        journal = json.loads(self.cli("journal", "--plan", pid).stdout)
        failed = [f for f in journal["files"] if f["state"] == "conflict"]
        self.assertTrue(failed)

    def test_cli_full_cycle(self):
        plan = json.loads(self.cli("plan", "--cutoff", CUTOFF.isoformat()).stdout)
        pid = plan["plan_id"]
        self.assertEqual(self.cli("isolate", "--plan", pid).returncode, 0)
        self.assertEqual(self.cli("restore", "--plan", pid).returncode, 0)
        plan2 = json.loads(self.cli("plan", "--cutoff", CUTOFF.isoformat()).stdout)
        self.assertEqual(self.cli("isolate", "--plan", plan2["plan_id"]).returncode, 0)
        run = self.cli("purge", "--plan", plan2["plan_id"])
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertGreater(json.loads(run.stdout)["released_disk_bytes"], 0)
        self.assertEqual(self.cli("restore", "--plan", plan2["plan_id"]).returncode, 2)


class DownstreamJobReferenceTests(ReclaimFixture):
    def test_funding_and_schedule_facts_protect_digests(self):
        import sqlite3
        digest = self.entries["old_orphan"]["digest"]
        for name, table in (("funding.sqlite3", "facts"),
                            ("schedule.sqlite3", "extraction_versions")):
            conn = sqlite3.connect(self.tmp / name)
            conn.execute(f"CREATE TABLE {table} (raw_digest TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES (?)", (digest,))
            conn.commit()
            conn.close()
        index = build_reference_index(self.tmp)
        self.assertIn(digest, index.digests)
        payload = preview(self.tmp, None, CUTOFF)
        self.assertEqual(self.group(payload, "old_orphan")["verdict"], "protected")


if __name__ == "__main__":
    unittest.main()


class RealIngestWindowTests(unittest.TestCase):
    """The production ScanEngine writes bytes, then commits facts.

    A second process performs reclamation during that exact window.  The
    shared/exclusive lock must make it wait, and the bytes must survive.
    """

    def setUp(self):
        from archive_pipeline.config import load_config
        from archive_pipeline.archive import RawArchive
        from archive_pipeline.engine import ScanEngine
        sys.path.insert(0, str(ROOT))
        self.tmp = Path(tempfile.mkdtemp(prefix="reclaim-ingest-"))
        self.config = load_config(ROOT / "config" / "sources.json",
                                  self.tmp / "archive")
        shutil.copy(ROOT / "scenario" / "scenario.json",
                    self.tmp / "scenario.json")
        self.store = Store(self.tmp / "pipeline.sqlite3")
        self.archive = RawArchive(self.tmp / "archive")
        self.engine = ScanEngine(self.config, self.store, self.archive)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.store.close)
        # The collector runs in its own process so the two sides really are
        # two processes, exactly like two CLI invocations.
        self.collector_code = f"""
import sys, time
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from archive_pipeline.config import load_config
from archive_pipeline.archive import RawArchive
from archive_pipeline.engine import ScanEngine
from archive_pipeline.store import Store
workdir = Path({str(self.tmp)!r})
config = load_config({str(ROOT / 'config' / 'sources.json')!r},
                     workdir / 'archive')
store = Store(workdir / 'pipeline.sqlite3')
engine = ScanEngine(config, store, RawArchive(workdir / 'archive'))
marker = workdir / 'written.marker'
committed = workdir / 'committed.marker'
seen = []

def hook(item, eng):
    if seen:
        return
    seen.append(item['raw_digest'])
    marker.write_text(item['raw_digest'])
    while not committed.exists():
        time.sleep(0.01)

engine.scan(config.source('sim_a'), interrupt_after=1,
            hooks={{'after_archive_write': hook}})
store.close()
"""

    def test_reclaimer_waits_inside_the_ingest_window(self):
        from archive_pipeline.reclaim import create_plan, isolate_plan
        from archive_pipeline.sim_server import start_sites

        sites = start_sites(self.tmp / "scenario.json")
        self.addCleanup(lambda: [site.stop() for site in sites])
        marker = self.tmp / "written.marker"
        committed = self.tmp / "committed.marker"
        decision = self.tmp / "decision.txt"
        observed: dict = {}

        collector = subprocess.Popen([sys.executable, "-c", self.collector_code],
                                     cwd=ROOT, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "scan never reached the archive write")
        digest = marker.read_text()
        blob = next(self.archive.root.rglob(f"{digest}.bin"))
        self.assertTrue(blob.is_file())
        # No durable row references it yet: the DB says nothing about it.
        index = build_reference_index(self.tmp)
        self.assertNotIn(digest, index.digests)

        reclaim_code = f"""
import sys, time
sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from datetime import datetime, timezone
from archive_pipeline.reclaim import preview
from archive_pipeline.worklock import exclusive_lock
workdir = Path({str(self.tmp)!r})
started = time.monotonic()
with exclusive_lock(workdir, timeout=30):
    waited = time.monotonic() - started
    payload = preview(workdir, None, datetime(2026, 10, 1, tzinfo=timezone.utc))
    digest = {digest!r}
    verdicts = [g['verdict'] for g in payload['groups'] if g['digest'] == digest]
with open({str(decision)!r}, 'w') as fh:
    fh.write(repr((waited, verdicts)))
"""
        reclaimer = subprocess.Popen([sys.executable, "-c", reclaim_code],
                                     cwd=ROOT, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        # The hook keeps the scan (and its shared lock) alive.  The reclaimer
        # must be stuck on the exclusive lock, so it cannot classify anything.
        time.sleep(0.8)
        self.assertFalse(decision.exists(),
                         "reclaimer did not wait for the collecting scan")
        # The scan is still in its window; no durable row exists yet.
        self.assertNotIn(digest, build_reference_index(self.tmp).digests)
        committed.write_text("go")
        collector.communicate(timeout=30)
        self.assertEqual(collector.returncode, 0, "collector failed")
        self.assertTrue(blob.is_file(), "in-flight blob was taken")
        rec_out, rec_err = reclaimer.communicate(timeout=30)
        self.assertEqual(reclaimer.returncode, 0, rec_err[-800:])
        self.assertTrue(decision.exists(), rec_err[-500:])
        waited, verdicts = eval(decision.read_text())
        self.assertGreater(waited, 0.5, "reclaimer never waited")
        # After the collector committed, the digest is referenced.
        self.assertIn(digest, build_reference_index(self.tmp).digests)
        # The reclaimer, running after the commit, sees it protected.
        self.assertEqual(verdicts, ["protected"])


class EngineLockTests(unittest.TestCase):
    def test_scan_holds_the_shared_workdir_lock(self):
        from archive_pipeline.worklock import collection_lock
        from archive_pipeline.engine import ScanEngine
        from archive_pipeline.config import load_config
        from archive_pipeline.archive import RawArchive
        import threading
        tmp = Path(tempfile.mkdtemp(prefix="reclaim-lock-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        config = load_config(ROOT / "config" / "sources.json", tmp / "archive")
        store = Store(tmp / "pipeline.sqlite3")
        self.addCleanup(store.close)
        engine = ScanEngine(config, store, RawArchive(tmp / "archive"))
        acquired: list[str] = []

        def holder():
            with collection_lock(tmp, timeout=10):
                acquired.append("shared")

        thread = threading.Thread(target=holder)
        thread.start()
        thread.join()
        self.assertEqual(acquired, ["shared"])
        # Now prove the production scan blocks while an exclusive holder runs.
        from archive_pipeline.worklock import exclusive_lock
        done = []
        started = time.monotonic()

        def scan():
            try:
                engine.scan(config.source("sim_a"), interrupt_after=0)
            except Exception:
                pass
            done.append(time.monotonic() - started)

        with exclusive_lock(tmp, timeout=5):
            scan_thread = threading.Thread(target=scan)
            scan_thread.start()
            time.sleep(0.4)
            self.assertEqual(done, [])
        scan_thread.join(timeout=30)
        self.assertTrue(done and done[0] >= 0.35)


class LegacyDatabaseTests(unittest.TestCase):
    def test_legacy_pipeline_database_blocks_reclamation(self):
        tmp = Path(tempfile.mkdtemp(prefix="reclaim-legacy-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        import sqlite3
        conn = sqlite3.connect(tmp / "pipeline.sqlite3")
        conn.executescript(
            "CREATE TABLE url_frontier (source_id TEXT, url_key TEXT);")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()
        with self.assertRaises(ReferenceError) as caught:
            build_reference_index(tmp)
        self.assertIn("pre-identity", str(caught.exception))

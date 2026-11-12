"""Integration tests over real HTTP against both local simulators.

Every test starts the two simulators on their fixed ports, runs the real engine
(with real sockets), and asserts observable behaviour: request manifests,
HTTP statuses, archive blobs, versions, round verdicts.

Run: ``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from archive_pipeline.archive import RawArchive
from archive_pipeline.config import load_config
from archive_pipeline.engine import ScanEngine
from archive_pipeline.sim_server import start_sites
from archive_pipeline.store import Store
from archive_pipeline.urlnorm import normalize_url

BASELINE = ROOT / "scenario" / "scenario.json"


class SimTestCase(unittest.TestCase):
    """Starts simulators with a temp copy of the scenario and a fresh store."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="archive-it-"))
        cls.scenario = cls.tmp / "scenario.json"
        shutil.copy(BASELINE, cls.scenario)
        cls.sites = start_sites(cls.scenario)
        cls.config = load_config(ROOT / "config" / "sources.json", cls.tmp / "data")
        cls.tracking = {}

    @classmethod
    def tearDownClass(cls):
        for site in cls.sites:
            site.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.case_dir = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        self.store = Store(self.case_dir / "state.sqlite3")
        self.archive = RawArchive(self.case_dir / "archive")
        self.engine = ScanEngine(self.config, self.store, self.archive)
        shutil.copy(BASELINE, self.scenario)
        for site in self.sites:
            site.reset_page_cache()
        self.addCleanup(self.store.close)

    # -- helpers ------------------------------------------------------
    def mutate(self, changes: list[dict]) -> None:
        data = json.loads(self.scenario.read_text(encoding="utf-8"))
        for change in changes:
            site = data["sites"][change["site"]]
            if "set" in change:
                site.update(change["set"])
            if "notice_key" in change:
                for notice in site.get("notices", []):
                    if notice["key"] == change["notice_key"]:
                        notice.update(change.get("fields", {}))
                        break
        self.scenario.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        for site in self.sites:
            site.reset_page_cache()

    def scan(self, source_id: str, **kwargs):
        return self.engine.scan(self.config.source(source_id), **kwargs)

    def item(self, result, needle: str) -> dict:
        matches = [i for i in result.detail_items if needle in i["url_key"]]
        self.assertEqual(len(matches), 1, f"expected exactly one item for {needle}, "
                                          f"got {[m['url_key'] for m in matches]}")
        return matches[0]

    def request_entries(self, result) -> list[dict]:
        return result.requests

    def served_paths(self, layout: str) -> list[str]:
        site = next(s for s in self.sites if s.layout == layout)
        return [entry["path"] for entry in site.served_log()]


class DirectoryAndNormalizationTests(SimTestCase):
    def test_baseline_scan_parses_both_layouts_to_same_shape(self):
        result_a = self.scan("sim_a")
        result_b = self.scan("sim_b")
        self.assertTrue(result_a.complete)
        self.assertTrue(result_b.complete)
        for record in self.store.all_versions("sim_a") + self.store.all_versions("sim_b"):
            for field in ("source_id", "document_key", "title", "issued_on", "body"):
                self.assertTrue(record[field], f"empty {field} in {record['document_key']}")
        keys_a = {v["document_key"] for v in self.store.all_versions("sim_a")}
        self.assertEqual(keys_a, {"A-1001", "A-1002", "A-1002P", "A-1003", "A-1044",
                                  "A-1055"})

    def test_equivalent_link_requested_once(self):
        result = self.scan("sim_a")
        matching = [i for i in result.detail_items if "A-1001" in i["url_key"]]
        self.assertEqual(len(matching), 1, "the equivalent A-1001 link must be deduped")
        # The directory carries two equivalent A-1001 links; only one request.
        detail_requests = [r for r in self.request_entries(result)
                           if r["kind"] == "detail" and "A-1001" in r["url"]]
        self.assertEqual(len(detail_requests), 1)

    def test_business_parameter_yields_distinct_document(self):
        result = self.scan("sim_a")
        plain = self.item(result, "A-1002?sort=asc&view=full")
        public = self.item(result, "A-1002?edition=public")
        self.assertEqual(plain["document_key"], "A-1002")
        self.assertEqual(public["document_key"], "A-1002P")
        self.assertNotEqual(plain["content_digest"], public["content_digest"])
        versions = self.store.versions_for("sim_a", "A-1002P")
        self.assertEqual(len(versions), 1)

    def test_relative_links_resolve_against_directory_url(self):
        result = self.scan("sim_b")
        normalized = [i["url_key"] for i in result.detail_items
                      if "water-quality" in i["url_key"]]
        self.assertTrue(all(u.startswith("http://127.0.0.1:8812/") for u in normalized))
        self.assertEqual(len(normalized), 1)

    def test_site_off_limit_links_never_reach_the_server(self):
        result = self.scan("sim_a")
        served = self.served_paths("layout_a")
        for entry in result.blocked:
            if entry["reason"] == "origin_not_allowed":
                self.assertFalse(any("example.invalid" in path for path in served))
            if entry["reason"] == "robots_disallowed":
                path = entry["url"].split("127.0.0.1:8811", 1)[-1]
                self.assertNotIn(path, served)

    def test_offsite_canonical_recorded_but_not_fetched(self):
        result = self.scan("sim_a")
        item = self.item(result, "crossed-canonical")
        self.assertEqual(item["canonical_blocked"],
                         "https://mirror.example.net/notices/crossed-canonical/A-1003")
        self.assertFalse(any("mirror.example.net" in r["url"] for r in result.requests))
        served = self.served_paths("layout_a")
        self.assertFalse(any("mirror" in path for path in served))

    def test_all_requests_stay_within_allowed_origins(self):
        for source_id, origin in (("sim_a", "http://127.0.0.1:8811"),
                                  ("sim_b", "http://127.0.0.1:8812")):
            result = self.scan(source_id)
            for request in result.requests:
                if request["requested"]:
                    self.assertTrue(request["url"].startswith(origin), request["url"])
                else:
                    # Refused-before-request entries may name an off-origin URL;
                    # the point is that they were never contacted.
                    self.assertIn(request["outcome"],
                                  {"origin_blocked", "redirect_blocked",
                                   "robots_disallowed", "redirect_loop",
                                   "redirect_budget_exhausted"})


class RedirectTests(SimTestCase):
    def test_in_scope_redirect_followed_and_recorded(self):
        result = self.scan("sim_b")
        item = self.item(result, "water-quality")
        self.assertEqual(item["http_status"], 200)
        self.assertTrue(item["redirected_to"].startswith(
            "http://127.0.0.1:8812/archive/water-quality"))
        chain_entries = [r for r in result.requests
                         if r["kind"] == "detail" and "water-quality" in r["url"]]
        self.assertEqual([r["status"] for r in chain_entries], [302, 200])

    def test_offsite_redirect_blocked_without_request(self):
        result = self.scan("sim_b")
        item = self.item(result, "offsite-hop")
        self.assertEqual(item["outcome"], "redirect_blocked")
        refused = [r for r in result.requests if not r["requested"]]
        self.assertTrue(any("outside.example.org" in r["url"] for r in refused))
        served = self.served_paths("layout_b")
        self.assertFalse(any("outside.example.org" in path for path in served))
        # The item stays pending so a later round retries it.
        row = self.store.get_url("sim_b", item["url_key"])
        self.assertEqual(row["state"], "pending")


class ConditionalAndVersionTests(SimTestCase):
    def test_second_scan_uses_conditional_get_and_304(self):
        self.scan("sim_a")
        result = self.scan("sim_a")
        for item in result.detail_items:
            self.assertEqual(item["outcome"], "not_modified", item["url_key"])
            self.assertEqual(item["http_status"], 304)
        conditional = [r for r in result.requests
                       if r["kind"] == "detail" and r["conditional_headers"]]
        self.assertTrue(conditional, "no If-None-Match/If-Modified-Since sent")
        for entry in conditional:
            self.assertIn("If-None-Match", entry["conditional_headers"])

    def test_304_does_not_create_version_or_empty_body(self):
        first = self.scan("sim_a")
        versions_before = {v["document_key"] for v in self.store.all_versions("sim_a")}
        second = self.scan("sim_a")
        item = self.item(second, "A-1001")
        self.assertEqual(item["version_no"], 1)
        self.assertFalse(item["version_created"])
        version = self.store.versions_for("sim_a", "A-1001")[0]
        self.assertNotEqual(version["body"], "")
        self.assertIn("First" if False else "经研究决定", version["body"])
        # 304 body (empty) is archived under its own digest.
        digest = item["raw_digest"]
        self.assertEqual(len(self.archive.load(digest)), 0)
        meta = self.archive.metadata(digest)
        self.assertEqual(meta["status"], 304)
        self.assertEqual({v["document_key"] for v in self.store.all_versions("sim_a")},
                         versions_before)

    def test_footer_only_change_no_version(self):
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "set": {"footer_version": 2}}])
        result = self.scan("sim_a")
        item = self.item(result, "A-1001")
        self.assertFalse(item["version_created"])
        self.assertTrue(item["raw_bytes_changed"])
        self.assertEqual(item["version_reason"], "business_fields_unchanged")
        self.assertEqual(len(self.store.versions_for("sim_a", "A-1001")), 1)
        self.assertEqual(
            self.store.versions_for("sim_a", "A-1001")[0]["version_no"], 1)

    def test_navigation_only_change_no_version(self):
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "set": {"nav_version": 2}}])
        result = self.scan("sim_a")
        for item in result.detail_items:
            self.assertFalse(item["version_created"])
            self.assertTrue(item["raw_bytes_changed"], item["url_key"])

    def test_body_change_creates_version_with_diff(self):
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "notice_key": "A-1001",
                      "fields": {"paragraphs": [
                          "经研究决定，自本通告发布之日起启动公共数据共享试点，试点期延长为两年。",
                          "试点单位应当按照最小必要原则收集数据，并定期公开共享目录。",
                          "新增：试点评估报告于每季度末发布。"]}}])
        result = self.scan("sim_a")
        item = self.item(result, "A-1001")
        self.assertTrue(item["version_created"])
        self.assertEqual(item["version_no"], 2)
        self.assertIn("body", item["changed_fields"])
        self.assertIn("新增：试点评估报告于每季度末发布。",
                      item["diff"]["paragraphs_added"])
        versions = self.store.versions_for("sim_a", "A-1001")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[1]["body"], item["body_paragraphs"] and
                         "\n\n".join(item["body_paragraphs"]))
        self.assertNotEqual(versions[0]["raw_digest"], versions[1]["raw_digest"])

    def test_title_change_creates_version(self):
        self.scan("sim_b")
        self.mutate([{"site": "layout_b", "notice_key": "B-2001",
                      "fields": {"title": "Water quality notice (revised)"}}])
        result = self.scan("sim_b")
        item = self.item(result, "water-quality")
        self.assertTrue(item["version_created"])
        self.assertIn("title", item["changed_fields"])

    def test_unchanged_second_scan_creates_no_versions_anywhere(self):
        self.scan("sim_a")
        before = len(self.store.all_versions("sim_a"))
        self.scan("sim_a")
        self.assertEqual(len(self.store.all_versions("sim_a")), before)


class RetractionTests(SimTestCase):
    def test_single_404_is_temporarily_unreachable(self):
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "notice_key": "A-1044",
                      "fields": {"available": False}}])
        result = self.scan("sim_a")
        doc = self.store.get_document("sim_a", "A-1044")
        self.assertEqual(doc["state"], "temporarily_unreachable")
        self.assertEqual(doc["missing_streak"], 1)
        self.assertIsNone(doc["retracted_at"])
        # Evidence table rows are only for complete scans; this one is complete.
        self.assertTrue(self.store.complete_scans_missing("sim_a", "A-1044"))

    def test_two_consecutive_complete_404s_retract_with_versions_preserved(self):
        baseline = self.scan("sim_a")
        self.assertEqual(len(self.store.versions_for("sim_a", "A-1044")), 1)
        self.mutate([{"site": "layout_a", "notice_key": "A-1044",
                      "fields": {"available": False}}])
        first_miss = self.scan("sim_a")
        self.assertEqual(self.store.get_document("sim_a", "A-1044")["state"],
                         "temporarily_unreachable")
        second_miss = self.scan("sim_a")
        doc = self.store.get_document("sim_a", "A-1044")
        self.assertEqual(doc["state"], "with_retracted")
        self.assertEqual(doc["missing_streak"], 2)
        self.assertIsNotNone(doc["retracted_at"])
        verdict = second_miss.retraction_verdicts[0]
        self.assertTrue(verdict["certified"])
        self.assertEqual(verdict["archived_versions_preserved"], 1)
        # History still readable after retraction.
        self.assertEqual(len(self.store.versions_for("sim_a", "A-1044")), 1)

    def test_503_is_not_a_round_and_does_not_advance_counter(self):
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "notice_key": "A-1055",
                      "fields": {"status_override": 503}}])
        result = self.scan("sim_a")
        self.assertFalse(result.complete)
        doc = self.store.get_document("sim_a", "A-1055")
        self.assertIn(doc["state"], {"active", "temporarily_unreachable"})
        self.assertEqual(doc["missing_streak"], 0)
        self.assertEqual(self.store.complete_scans_missing("sim_a", "A-1055"), [])

    def test_503_between_404s_does_not_reset_evidence(self):
        """Sequence: complete 404, incomplete 503, complete 404 -> retract.

        Incomplete scans are skipped rather than counted, and they do not wipe
        the previously observed 404, so the two complete rounds are still
        consecutive rounds in the rule's sense.
        """
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "notice_key": "A-1044",
                      "fields": {"available": False}}])
        first_404 = self.scan("sim_a")
        self.assertTrue(first_404.complete)
        # Interleave an incomplete scan on the same source by breaking A-1055.
        self.mutate([{"site": "layout_a", "notice_key": "A-1055",
                      "fields": {"status_override": 503}}])
        middle = self.scan("sim_a")
        self.assertFalse(middle.complete)
        self.assertIsNone(self.store.get_document("sim_a", "A-1044")["retracted_at"])
        # Now a complete scan with A-1055 healthy and A-1044 still 404.
        self.mutate([{"site": "layout_a", "notice_key": "A-1055",
                      "fields": {"status_override": None}}])
        third = self.scan("sim_a")
        self.assertTrue(third.complete)
        doc = self.store.get_document("sim_a", "A-1044")
        self.assertEqual(doc["state"], "with_retracted")
        verdict = third.retraction_verdicts[0]
        self.assertEqual(verdict["previous_complete_scan_id"], first_404.scan_id)
        self.assertEqual(verdict["skipped_incomplete_scans"], [middle.scan_id])

    def test_success_in_complete_round_breaks_chain(self):
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "notice_key": "A-1044",
                      "fields": {"available": False}}])
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "notice_key": "A-1044",
                      "fields": {"available": True}}])
        recovery = self.scan("sim_a")
        self.assertEqual(self.store.get_document("sim_a", "A-1044")["state"], "active")
        self.mutate([{"site": "layout_a", "notice_key": "A-1044",
                      "fields": {"available": False}}])
        again = self.scan("sim_a")
        # Only one 404 since recovery: not certified.
        doc = self.store.get_document("sim_a", "A-1044")
        self.assertEqual(doc["state"], "temporarily_unreachable")
        self.assertIsNone(doc["retracted_at"])

    def test_retracted_document_reactivates_if_it_returns(self):
        self.scan("sim_a")
        self.mutate([{"site": "layout_a", "notice_key": "A-1044",
                      "fields": {"available": False}}])
        self.scan("sim_a")
        self.scan("sim_a")
        self.assertEqual(self.store.get_document("sim_a", "A-1044")["state"],
                         "with_retracted")
        self.mutate([{"site": "layout_a", "notice_key": "A-1044",
                      "fields": {"available": True}}])
        result = self.scan("sim_a")
        self.assertEqual(self.store.get_document("sim_a", "A-1044")["state"], "active")


class PaginationTests(SimTestCase):
    def test_loop_terminates_scan_and_is_reported(self):
        self.mutate([{"site": "layout_b", "set": {"pagination_loop": True,
                                                 "loop_target_page": 2}}])
        result = self.scan("sim_b")
        self.assertFalse(result.complete)
        self.assertTrue(result.paging_loop)
        self.assertTrue(result.stop_reason.startswith("paging_loop"))
        page_urls = [p.url for p in result.pages]
        self.assertEqual(len(page_urls), len(set(page_urls)),
                         "a loop must not cause a page to be refetched")
        directory_requests = [r for r in result.requests if r["kind"] == "directory"]
        self.assertEqual(len(directory_requests), len(set(r["url"] for r in directory_requests)))

    def test_robots_blocked_directory_page_marks_incomplete(self):
        self.mutate([{"site": "layout_a", "set": {"robots_embargo_page": 3}}])
        result = self.scan("sim_a")
        self.assertFalse(result.complete)
        self.assertTrue(result.stop_reason.startswith("paging_blocked"))
        refused_candidates = [
            entry for entry in result.blocked
            if "notices/page/3" in entry["url"] and entry["reason"] == "robots_disallowed"
        ]
        self.assertTrue(refused_candidates, result.blocked)
        served = self.served_paths("layout_a")
        self.assertFalse(any("/notices/page/3?" in path for path in served))


class PersistenceTests(SimTestCase):
    def test_restart_resumes_pending_urls(self):
        source = self.config.source("sim_b")
        partial = self.engine.scan(source, interrupt_after=2)
        self.assertFalse(partial.complete)
        pending = [row["url_key"] for row in self.store.pending_urls("sim_b")]
        self.assertTrue(pending, "interrupted scan must leave pending frontier rows")
        self.assertEqual(self.store.scan(partial.scan_id)["status"], "abandoned")

        # Fresh engine, fresh connection to the same database.
        self.store.close()
        self.store = Store(self.case_dir / "state.sqlite3")
        self.addCleanup(self.store.close)
        resumed_engine = ScanEngine(self.config, self.store, self.archive)
        resumed = resumed_engine.scan(source)
        self.assertTrue(resumed.complete)
        self.assertTrue(resumed.resumed_urls, "resumed scan must pick up pending URLs")
        # Only retriable failures (blocked off-site redirect) may remain pending;
        # everything the directory listed was processed.
        leftover = [row["url_key"] for row in self.store.pending_urls("sim_b")]
        self.assertTrue(all("offsite-hop" in key for key in leftover), leftover)
        # No duplicate versions were produced for already-seen documents.
        for doc in self.store.documents("sim_b"):
            self.assertEqual(
                len(self.store.versions_for("sim_b", doc["document_key"])) , 1,
                doc["document_key"])

    def test_frontier_survives_reopen_and_conditional_get_still_works(self):
        self.scan("sim_a")
        self.store.close()
        self.store = Store(self.case_dir / "state.sqlite3")
        self.addCleanup(self.store.close)
        engine = ScanEngine(self.config, self.store, self.archive)
        result = engine.scan(self.config.source("sim_a"))
        self.assertTrue(all(i["outcome"] == "not_modified" for i in result.detail_items))

    def test_archive_blobs_are_content_addressed(self):
        result = self.scan("sim_a")
        digests = {i["raw_digest"] for i in result.detail_items}
        for digest in digests:
            raw = self.archive.load(digest)
            import hashlib
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            meta = self.archive.metadata(digest)
            self.assertEqual(meta["status"], 200)


class RequestManifestTests(SimTestCase):
    def test_manifest_covers_every_request_and_enforces_spacing(self):
        result = self.scan("sim_a")
        entries = result.requests
        kinds = {entry["kind"] for entry in entries}
        self.assertEqual(kinds, {"robots", "directory", "detail"})
        # Minimum interval is configured as 120ms; every request after the first
        # records the enforced wait, which must be > 0.
        waits = [entry["spacing_wait_ms"] for entry in entries]
        self.assertTrue(any(wait > 0 for wait in waits))
        self.assertGreaterEqual(min(waits[1:]), 0.0)
        self.assertEqual(len([e for e in entries if e["kind"] == "robots"]), 1,
                         "robots.txt is fetched once per scan")

    def test_each_scan_has_its_own_manifest_rows(self):
        first = self.scan("sim_a")
        second = self.scan("sim_a")
        first_rows = self.store.scan_items(first.scan_id)
        second_rows = self.store.scan_items(second.scan_id)
        self.assertEqual(len(first_rows), len(first.detail_items))
        self.assertEqual(len(second_rows), len(second.detail_items))
        for row in second_rows:
            requests = json.loads(row["requests_json"])
            self.assertTrue(requests, f"missing request log for {row['url_key']}")


if __name__ == "__main__":
    unittest.main()

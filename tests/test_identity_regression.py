"""Regression fixture for the cross-variant identity (串档) defect.

These tests use the real local HTTP simulators (no mocks) and reproduce the
exact defect from the issue: when the public edition page publishes the **same
notice number** as the full edition (both print ``A-1002``) while keeping
different bodies and different business URLs, the two variants must still get
independent, stable archive identities.

Coverage, each asserting the *persisted* contents of the SQLite store rather
than just success output:

1. same-number variants appear and update independently — separate identity
   rows, separate version chains, the second scan never overwrites the other's
   current content;
2. when one variant answers ``304`` its stored body is reused and the sibling
   variant's body can never leak into it;
3. one variant returning 404 for two consecutive complete scans is retracted
   on its own while the still-healthy sibling stays active and keeps its
   versions;
4. tracking-parameter aliases and real HTTP redirects create no extra
   documents or versions, and a declared ``rel=canonical`` never merges
   variants distinguished by business parameters.

Run: ``python -m unittest tests.test_identity_regression -v``
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_integration import SimTestCase  # noqa: E402


def same_number_scenario(scenario_path: Path) -> None:
    """Make the public edition print the SAME number as the full edition."""
    import json
    data = json.loads(scenario_path.read_text(encoding="utf-8"))
    for notice in data["sites"]["layout_a"]["notices"]:
        if notice["key"] == "A-1002":
            notice["public_edition"] = {
                # Deliberately identical published number; different body/URL.
                "key": "A-1002",
                "title": "关于发布年度研究报告（公众版）的通知",
                "issued": "2026-02-18",
                "paragraphs": [
                    "公众版摘编已发布，与完整版在披露范围上存在差异。",
                    "公众版由平台编辑委员会审定。",
                ],
            }
    scenario_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


class SameNumberVariantTests(SimTestCase):
    """Both editions print A-1002; they must never share a version chain."""

    def setUp(self):
        super().setUp()
        same_number_scenario(self.scenario)
        for site in self.sites:
            site.reset_page_cache()
        self.full_url_fragment = "A-1002?sort=asc&view=full"
        self.public_url_fragment = "A-1002?edition=public"

    # -- helpers ------------------------------------------------------
    def _both_items(self, result):
        full = self.item(result, self.full_url_fragment)
        public = self.item(result, self.public_url_fragment)
        return full, public

    def _bodies(self):
        full_id = self.identity_for_url("sim_a", self.full_url_fragment)
        public_id = self.identity_for_url("sim_a", self.public_url_fragment)
        full_body = self.store.last_version("sim_a", full_id)["body"]
        public_body = self.store.last_version("sim_a", public_id)["body"]
        return full_id, public_id, full_body, public_body

    # -----------------------------------------------------------------
    def test_same_published_number_yields_two_independent_identities(self):
        result = self.scan("sim_a")
        full, public = self._both_items(result)
        # The page-published number is retained on both records...
        self.assertEqual(full["document_key"], "A-1002")
        self.assertEqual(public["document_key"], "A-1002")
        # ...but the archive identities differ and are stable/queryable.
        self.assertNotEqual(full["archive_id"], public["archive_id"])
        identities = self.store.identities_for_notice("sim_a", "A-1002")
        self.assertEqual({i["archive_id"] for i in identities},
                         {full["archive_id"], public["archive_id"]})
        # Two separate version chains, each at version 1 with its own body.
        full_id, public_id, full_body, public_body = self._bodies()
        self.assertEqual([v["version_no"] for v in
                          self.store.versions_for("sim_a", full_id)], [1])
        self.assertEqual([v["version_no"] for v in
                          self.store.versions_for("sim_a", public_id)], [1])
        self.assertNotEqual(full_body, public_body)
        self.assertIn("报告全文", full_body)
        self.assertIn("公众版摘编", public_body)

    def test_variants_update_independently_without_overwriting_each_other(self):
        first = self.scan("sim_a")
        _, public_first = self._both_items(first)
        public_body_v1 = self.store.last_version(
            "sim_a", public_first["archive_id"])["body"]

        # Edit the FULL edition only.
        self.mutate([{
            "site": "layout_a", "notice_key": "A-1002",
            "fields": {"paragraphs": [
                "《年度研究报告（2026）》现已发布，修订版补充了经费明细。",
                "报告全文可在本平台资料栏查阅。"]}}])
        second = self.scan("sim_a")
        full, public = self._both_items(second)
        full_id, public_id, full_body, public_body = self._bodies()

        # Full edition advanced to v2; public edition stayed at v1 unchanged.
        self.assertTrue(full["version_created"])
        self.assertEqual(full["version_no"], 2)
        self.assertFalse(public["version_created"])
        self.assertEqual(public["version_no"], 1)
        self.assertEqual(
            len(self.store.versions_for("sim_a", full_id)), 2)
        self.assertEqual(
            len(self.store.versions_for("sim_a", public_id)), 1)
        # The public variant's current content is still its own v1 body — the
        # exact "current content replaced" symptom must be impossible.
        self.assertEqual(public_body, public_body_v1)
        self.assertIn("修订版", full_body)
        self.assertNotIn("修订版", public_body)

    def test_304_of_one_variant_never_returns_the_other_variants_body(self):
        self.scan("sim_a")
        # Change the full edition so it answers 200 on the next scan; leave the
        # public edition untouched so its validators yield a real 304.
        self.mutate([{
            "site": "layout_a", "notice_key": "A-1002",
            "fields": {"paragraphs": [
                "《年度研究报告（2026）》修订版已发布。",
                "报告全文可在本平台资料栏查阅。"]}}])
        result = self.scan("sim_a")
        full, public = self._both_items(result)
        self.assertEqual(full["http_status"], 200)
        self.assertEqual(public["http_status"], 304)
        self.assertEqual(public["outcome"], "not_modified")

        full_id, public_id, full_body, public_body = self._bodies()
        # The 304 reused the public variant's OWN archived version; it cannot
        # contain the full edition's revised paragraph.
        self.assertIn("公众版摘编", public_body)
        self.assertNotIn("修订版", public_body)
        self.assertIn("修订版", full_body)
        # Public version count unchanged; full edition got its own v2.
        self.assertEqual(len(self.store.versions_for("sim_a", public_id)), 1)
        self.assertEqual(len(self.store.versions_for("sim_a", full_id)), 2)
        # Validators live on distinct frontier rows (per-URL).
        public_row = self.store.get_url("sim_a",
                                        self.item(result, self.public_url_fragment)
                                        ["url_key"])
        full_row = self.store.get_url("sim_a",
                                      self.item(result, self.full_url_fragment)
                                      ["url_key"])
        self.assertNotEqual(public_row["archive_id"], full_row["archive_id"])
        self.assertTrue(public_row["etag"])
        self.assertTrue(full_row["etag"])

    def test_four_oh_four_of_one_variant_does_not_retract_the_other(self):
        self.scan("sim_a")
        # Withdraw ONLY the public edition; the full edition stays healthy.
        import json
        data = json.loads(self.scenario.read_text(encoding="utf-8"))
        for notice in data["sites"]["layout_a"]["notices"]:
            if notice["key"] == "A-1002":
                notice["public_edition"]["available"] = False
        self.scenario.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        for site in self.sites:
            site.reset_page_cache()

        miss_one = self.scan("sim_a")
        full_1, public_1 = self._both_items(miss_one)
        self.assertEqual(public_1["http_status"], 404)
        self.assertEqual(full_1["http_status"], 304)

        # Two consecutive complete 404 scans for the public variant only.
        miss_two = self.scan("sim_a")
        full_2, public_2 = self._both_items(miss_two)
        self.assertEqual(public_2["http_status"], 404)
        self.assertEqual(full_2["http_status"], 304)

        full_id, public_id, full_body, public_body = self._bodies()
        public_identity = self.store.get_identity("sim_a", public_id)
        full_identity = self.store.get_identity("sim_a", full_id)
        self.assertEqual(public_identity["state"], "with_retracted")
        self.assertEqual(full_identity["state"], "active")
        # Retraction is per identity: versions of both remain readable.
        self.assertTrue(self.store.versions_for("sim_a", public_id))
        self.assertTrue(self.store.versions_for("sim_a", full_id))
        self.assertIn("报告全文", full_body)
        self.assertIn("公众版摘编", public_body)
        # The retraction verdict names the public identity only.
        verdict_ids = {v["archive_id"] for v in miss_two.retraction_verdicts
                       if v["certified"]}
        self.assertEqual(verdict_ids, {public_id})

    def test_canonical_declaration_does_not_merge_business_variants(self):
        result = self.scan("sim_a")
        full, public = self._both_items(result)
        # The simulator emits a canonical pointing at the number-only URL for
        # both variants; it must be recorded but ignored for identity.
        for item in (full, public):
            self.assertTrue(item.get("canonical_url"))
        self.assertNotEqual(full["archive_id"], public["archive_id"])
        # A canonical never creates an extra frontier identity either.
        arc_ids = {r["archive_id"] for r in self.store.list_frontier("sim_a")
                   if r["archive_id"]}
        self.assertIn(full["archive_id"], arc_ids)
        self.assertIn(public["archive_id"], arc_ids)


class AliasAndRedirectIdentityTests(SimTestCase):
    """Tracking aliases and real redirects must not manufacture documents."""

    def test_tracking_parameter_alias_creates_no_extra_version(self):
        result = self.scan("sim_a")
        # Directory lists A-1001 twice: plain and with utm_* + fragment; after
        # normalization they are one frontier key and produce one request.
        items = [i for i in result.detail_items if "A-1001" in i["url_key"]]
        self.assertEqual(len(items), 1)
        identity = items[0]["archive_id"]
        self.assertEqual(
            len(self.store.versions_for("sim_a", identity)), 1)
        # Re-scan with an additional tracking-only alias for the same resource.
        self.mutate([{
            "site": "layout_a", "set": {"extra_links": [
                {"href": "/notices/same-url/A-1001?view=full&sort=asc&gclid=zzz",
                 "label": "tracking alias", "kind": "equivalent"}]}}])
        second = self.scan("sim_a")
        items = [i for i in second.detail_items if "A-1001" in i["url_key"]]
        self.assertEqual(len(items), 1)
        self.assertEqual(
            len(self.store.versions_for("sim_a", identity)), 1)
        # No second identity was created for the tracking alias.
        a1001_ids = {r["archive_id"] for r in self.store.list_frontier("sim_a")
                     if r["archive_id"] and "A-1001" in r["url_key"]}
        self.assertEqual(a1001_ids, {identity})

    def test_real_redirect_alias_shares_target_identity(self):
        result = self.scan("sim_b")
        item = self.item(result, "water-quality")
        self.assertEqual(item["http_status"], 200)
        target_id = item["archive_id"]
        # The short link row and the long target row map to one identity.
        rows = [r for r in self.store.list_frontier("sim_b")
                if "water-quality" in r["url_key"]]
        self.assertTrue(rows)
        self.assertEqual({r["archive_id"] for r in rows}, {target_id})
        alias_rows = [r for r in rows if r["is_alias"]]
        self.assertTrue(alias_rows, "the short link must be flagged as an alias")
        # Exactly one version for the document, regardless of which URL listed.
        self.assertEqual(
            len(self.store.versions_for("sim_b", target_id)), 1)
        self.assertEqual(
            len([i for i in result.detail_items if "water-quality" in i["url_key"]]),
            1)


if __name__ == "__main__":
    unittest.main()

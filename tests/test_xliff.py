"""XLIFF 1.2 intake job: bindings, inline codes, drafts, conflicts, atomicity.

Expected values are listed independently of the fixtures (the archive bodies
are read back from the sample pipeline database), then checked against the
actual ``xliff.sqlite3`` rows and provenance relations.

Run: ``python -m unittest discover -s tests -v`` from the project root.
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

from archive_pipeline.xliffcli import main as cli_main
from archive_pipeline.xliffintake import (
    IntakeInterrupted,
    IntakeJob,
    IntakeRejected,
    MappingError,
    XliffError,
    XliffStore,
    load_mapping,
)
from archive_pipeline.xliffparse import (
    normalize_language,
    parse_xliff,
    XliffSyntaxError,
)
from archive_pipeline.xliffsample import (
    build_sample_workdir,
    span_of,
    write_pipeline_manifest,
)

REVIEWS = ROOT / "scenario" / "xliff" / "reviews"
MAPPINGS = ROOT / "scenario" / "xliff" / "mappings"
FULL_AID = "arc_df6fcccf27a8993dc8ffc23c1e60c5d2"
PUBLIC_AID = "arc_88e49bc08495d34458e9370634834de8"
SUPP_AID = "arc_cc1aabf730dec3f8c59bf8f0c67dc8e6"


class XliffTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="xliff-"))
        cls.built = build_sample_workdir(cls.tmp)
        write_pipeline_manifest(cls.tmp, cls.built)
        cls.body = {v["file"]: v["body"] for v in cls.built["versions"]}
        cls.full = cls.body["X-4001-application.html"]
        cls.public = cls.body["X-4001-public.html"]
        cls.v1 = cls.body["X-4002-supplement-v1.html"]
        cls.v2 = cls.body["X-4002-supplement-v2.html"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="xliff-w-", dir=self.tmp))

    # -- helpers ---------------------------------------------------------
    def run_intake(self, review, mapping, **kwargs):
        job = IntakeJob(self.work, self.tmp / "pipeline.sqlite3")
        try:
            return job.intake(REVIEWS / review, MAPPINGS / mapping, **kwargs)
        finally:
            job.close()

    def store(self):
        return XliffStore(self.work / "xliff.sqlite3")

    def unit(self, store, unit_id, delivery_id=None):
        rows = store.unit_by_id(unit_id, delivery_id)
        self.assertTrue(rows, unit_id)
        return rows[0]

    # ------------------------------------------------------------------
    def test_01_candidates_and_character_spans(self):
        outcome = self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.exit_code, 0)
        store = self.store()
        try:
            u = self.unit(store, "u-01")
            self.assertEqual(u["status"], "candidate")
            self.assertEqual((u["span_start"], u["span_end"]), (31, 97))
            self.assertEqual(u["source_text"], self.full[31:97])
            self.assertEqual(u["source_language"], "zh-cn")
            self.assertEqual(u["target_language"], "en-us")
            # multi-byte characters: offsets are string indices, not bytes
            self.assertIn("申报时间为", u["source_text"])
            self.assertNotEqual(
                len(self.full[31:97].encode("utf-8")), 97 - 31)
            groups = store.group_state(state="available")
            self.assertEqual(len(groups), 2)
            self.assertTrue(all(g["source_language"] == "zh-cn" for g in groups))
            sources = store.candidate_sources(u["candidate_id"])
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["delivery_id"], outcome.delivery_id)
        finally:
            store.close()

    def test_02_nested_groups_and_sibling_reorder(self):
        outcome = self.run_intake("02-nested-en-gb.xlf", "02-nested-en-gb.json")
        self.assertEqual(outcome.status, "completed")
        store = self.store()
        try:
            u = self.unit(store, "u-nest-1")
            self.assertEqual(u["status"], "candidate")
            self.assertEqual(json.loads(u["group_path_json"]), ["outer", "inner"])
            swap = self.unit(store, "u-swap")
            self.assertEqual(swap["status"], "candidate")
            codes = json.loads(swap["target_codes_json"])
            self.assertEqual([c["id"] for c in codes], ["p2", "p1"])
            self.assertEqual(swap["target_language"], "en-gb")
            # en-GB and en-US stay separate groups for the same span
            groups = store.group_state()
            self.assertEqual({g["target_language"] for g in groups},
                             {"en-gb"})
        finally:
            store.close()

    def test_03_contract_violations_are_rejected_individually(self):
        outcome = self.run_intake("03-mixed-rejections.xlf",
                                  "03-mixed-rejections.json")
        self.assertEqual(outcome.exit_code, 4)
        self.assertEqual(outcome.status, "partial")
        store = self.store()
        try:
            expected = {
                "u-bad-id": "target_missing_code",
                "u-bad-kind": "target_code_type_changed",
                "u-bad-level": "target_code_hierarchy_changed",
                "u-bad-equiv": "target_equiv_changed",
                "u-unsupported": "unsupported_inline",
                "u-missing-target": "target_count",
                "u-public-misbound": "binding_mismatch",
                "u-version": "binding_mismatch",
            }
            for unit_id, code in expected.items():
                row = self.unit(store, unit_id)
                self.assertEqual(row["status"], "rejected", unit_id)
                self.assertEqual(row["reason_code"], code, unit_id)
            self.assertEqual(self.unit(store, "u-ok")["status"], "candidate")
            self.assertEqual(self.unit(store, "u-public-ok")["status"], "candidate")
            self.assertEqual(self.unit(store, "u-window")["status"], "candidate")
            # rejected material keeps source/target text and codes for review
            bad = self.unit(store, "u-bad-id")
            self.assertTrue(bad["source_text"])
            self.assertTrue(json.loads(bad["source_codes_json"]))
            self.assertTrue(json.loads(bad["target_codes_json"]))
            self.assertIsNotNone(bad["source_line"])
            # the misbound full-edition paragraph is not silently borrowed
            misbound = self.unit(store, "u-public-misbound")
            self.assertIn(PUBLIC_AID, misbound["reason_detail"])
            # version mismatch says v1 while the file is bound to v2
            self.assertIn("v1", self.unit(store, "u-version")["reason_detail"])
            # the broken span does not create any candidate
            # the misbound paragraph is not stored as a candidate even though
            # the span is legal for the *other* variant of the same number
            self.assertEqual(
                [c for c in store.candidates(archive_id=PUBLIC_AID)
                 if c["span_start"] == 99], [])
        finally:
            store.close()

    def test_04_drafts_never_queryable(self):
        self.run_intake("03-mixed-rejections.xlf", "03-mixed-rejections.json")
        store = self.store()
        try:
            for unit_id in ("u-draft", "u-approved-draft"):
                row = self.unit(store, unit_id)
                self.assertEqual(row["status"], "draft")
                self.assertEqual(row["reason_code"], "draft_not_approved")
                self.assertIsNone(row["candidate_id"])
            self.assertIn("approved='no'",
                          self.unit(store, "u-draft")["reason_detail"])
            self.assertIn("translated",
                          self.unit(store, "u-approved-draft")["reason_detail"])
            spans = {(g["span_start"], g["span_end"])
                     for g in store.group_state()}
            self.assertNotIn((262, 283), spans)
            self.assertNotIn(283, [g["span_end"] for g in
                                   store.group_state()])
        finally:
            store.close()

    def test_05_duplicate_unit_id_invalidates_all_in_file(self):
        outcome = self.run_intake("04-duplicate-id.xlf", "04-duplicate-id.json")
        self.assertEqual(outcome.exit_code, 4)
        store = self.store()
        try:
            rows = store.unit_by_id("u-dup")
            self.assertEqual(len(rows), 3)
            for row in rows:
                files = store.files(row["delivery_id"])
                original = [f["original"] for f in files
                            if f["file_pk"] == row["file_pk"]][0]
                if original.endswith("#dup"):
                    continue
                self.assertEqual(row["status"], "rejected")
                self.assertEqual(row["reason_code"], "duplicate_unit_id")
            # every earlier occurrence in the same file is invalid too
            first_file = store.files(1)
            self.assertEqual(first_file[0]["original"],
                             "notices/X-4001-application.html")
            self.assertEqual(self.unit(store, "u-clean")["status"], "candidate")
        finally:
            store.close()

    def test_06_idempotent_redelivery_and_alias_merge(self):
        first = self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        store = self.store()
        before = (len(store.candidates()), len(store.candidate_sources(1)),
                  len(store.deliveries()))
        store.close()
        again = self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(again.delivery_id, first.delivery_id)
        store = self.store()
        after = (len(store.candidates()), len(store.candidate_sources(1)),
                 len(store.deliveries()))
        self.assertEqual(before, after)
        store.close()
        # a different package (namespace prefix) with the same translation
        # adds a delivery source but no candidate
        alias = self.run_intake("05-prefixed-alias.xlf", "05-prefixed-alias.json")
        self.assertEqual(alias.status, "completed")
        store = self.store()
        candidates = store.candidates()
        self.assertEqual(len(candidates), 2)
        for candidate in candidates:
            sources = store.candidate_sources(candidate["candidate_id"])
            self.assertEqual(len(sources), 2)
            self.assertEqual({s["delivery_id"] for s in sources},
                             {first.delivery_id, alias.delivery_id})
            self.assertEqual(len(store.group_state(state="available")), 2)
        store.close()

    def test_07_conflict_after_unique_translation(self):
        self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        store = self.store()
        group_before = [g for g in store.group_state()
                        if g["span_start"] == 31][0]
        self.assertEqual(group_before["state"], "available")
        self.assertEqual(group_before["available_candidate_id"], 1)
        store.close()
        conflict = self.run_intake("06-conflict-enus.xlf",
                                   "06-conflict-enus.json")
        self.assertEqual(conflict.status, "completed")
        store = self.store()
        try:
            group = [g for g in store.group_state()
                     if g["span_start"] == 31][0]
            self.assertEqual(group["state"], "conflict")
            self.assertIsNone(group["available_candidate_id"])
            self.assertEqual(group["candidate_count"], 2)
            self.assertIn("awaiting human review", group["reason"])
            active = [c for c in store.candidates(archive_id=FULL_AID)
                      if c["span_start"] == 31 and c["span_end"] == 97]
            self.assertEqual(len(active), 2)
            for candidate in store.candidates():
                self.assertEqual(len(store.candidate_sources(
                    candidate["candidate_id"])), 1)
            usable = store.group_state(state="available")
            self.assertNotIn(31, [g["span_start"] for g in usable])
        finally:
            store.close()

    def test_08_identical_translation_different_wrapper_is_not_conflict(self):
        self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        self.run_intake("05-prefixed-alias.xlf", "05-prefixed-alias.json")
        store = self.store()
        try:
            self.assertFalse(store.group_state(state="conflict"))
            group = [g for g in store.group_state()
                     if g["span_start"] == 31][0]
            self.assertEqual(group["state"], "available")
        finally:
            store.close()

    def test_09_malformed_xml_leaves_corpus_untouched(self):
        self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        db = self.work / "xliff.sqlite3"
        before = db.read_bytes()
        with self.assertRaises(IntakeRejected) as ctx:
            self.run_intake("07-broken.xlf", "01-full-enus.json")
        self.assertEqual(ctx.exception.code, "malformed_xml")
        self.assertEqual(db.read_bytes(), before)

    def test_10_dtd_and_external_entity_refused(self):
        with self.assertRaises(IntakeRejected) as ctx:
            self.run_intake("08-dtd-entity.xlf", "01-full-enus.json")
        self.assertEqual(ctx.exception.code, "doctype_refused")
        self.assertFalse((self.work / "xliff.sqlite3").exists())
        with self.assertRaises(XliffSyntaxError):
            parse_xliff((REVIEWS / "08-dtd-entity.xlf").read_bytes())

    def test_11_forced_exit_before_commit_keeps_previous_state(self):
        first = self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        store = self.store()
        before = ([dict(g) for g in store.group_state()],
                  len(store.candidates()), len(store.deliveries()))
        store.close()
        with self.assertRaises(IntakeInterrupted):
            self.run_intake("06-conflict-enus.xlf", "06-conflict-enus.json",
                            fail_point="before_commit")
        store = self.store()
        try:
            after = ([dict(g) for g in store.group_state()],
                     len(store.candidates()), len(store.deliveries()))
            self.assertEqual(len(after[0]), len(before[0]))
            self.assertEqual(after[1], before[1])
            self.assertEqual(after[2], before[2])
            self.assertFalse(store.group_state(state="conflict"))
            runs = store.runs()
            self.assertEqual(runs[-1]["status"], "interrupted")
            # retrying the same delivery now succeeds and creates the conflict
            store.close()
            retry = self.run_intake("06-conflict-enus.xlf",
                                    "06-conflict-enus.json")
            self.assertEqual(retry.status, "completed")
            store = self.store()
            self.assertTrue(store.group_state(state="conflict"))
            self.assertEqual(store.deliveries()[-1]["delivery_id"],
                             retry.delivery_id)
        finally:
            store.close()

    def test_12_readback_survives_reopen(self):
        self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        self.run_intake("06-conflict-enus.xlf", "06-conflict-enus.json")
        store = self.store()
        store.close()
        store = XliffStore(self.work / "xliff.sqlite3")
        try:
            conflict = store.group_state(state="conflict")
            self.assertEqual(len(conflict), 1)
            self.assertEqual(conflict[0]["candidate_count"], 2)
            unit = store.unit_by_id("u-head")[0]
            self.assertEqual(unit["status"], "candidate")
            self.assertEqual(store.deliveries()[0]["review_sha256"],
                             self.run_intake("01-full-enus.xlf",
                                             "01-full-enus.json").summary[
                                                 "review_sha256"])
        finally:
            store.close()

    def test_13_language_tags(self):
        self.assertEqual(normalize_language("zh"), "zh")
        self.assertEqual(normalize_language("zh-CN"), "zh-cn")
        self.assertEqual(normalize_language("EN-gb"), "en-gb")
        self.assertIsNone(normalize_language("zh--CN"))
        self.assertIsNone(normalize_language("-zh"))
        self.assertIsNone(normalize_language("zh-"))
        self.assertIsNone(normalize_language("zh-123456789"))
        self.assertIsNone(normalize_language("t"))
        self.assertEqual(normalize_language("xx-Y1"), "xx-y1")
        self.assertNotEqual(normalize_language("en-US"),
                            normalize_language("en-GB"))

    def test_14_same_language_file_refused(self):
        mapping = json.loads(
            (MAPPINGS / "01-full-enus.json").read_text(encoding="utf-8"))
        review_text = (REVIEWS / "01-full-enus.xlf").read_text(encoding="utf-8")
        bad = review_text.replace('target-language="en-US"',
                                  'target-language="zh-CN"', 1)
        review = self.work / "bad.xlf"
        review.write_text(bad, encoding="utf-8")
        bind = self.work / "bad.json"
        bind.write_text(json.dumps(mapping, ensure_ascii=False),
                        encoding="utf-8")
        job = IntakeJob(self.work, self.tmp / "pipeline.sqlite3")
        try:
            outcome = job.intake(review, bind)
        finally:
            job.close()
        self.assertEqual(outcome.exit_code, 4)
        results = outcome.summary["file_results"]
        self.assertEqual(results[0]["reason_code"], "same_language")

    def test_15_unknown_archive_version(self):
        mapping = json.loads(
            (MAPPINGS / "09-wrong-version.json").read_text(encoding="utf-8"))
        mapping["files"][0]["version_no"] = 9
        path = self.work / "unknown.json"
        path.write_text(json.dumps(mapping, ensure_ascii=False),
                        encoding="utf-8")
        job = IntakeJob(self.work, self.tmp / "pipeline.sqlite3")
        try:
            outcome = job.intake(REVIEWS / "09-wrong-version.xlf", path)
        finally:
            job.close()
        self.assertEqual(outcome.exit_code, 4)
        self.assertEqual(outcome.summary["file_results"][0]["reason_code"],
                         "unknown_version")

    def test_16_no_translation_written_to_notice_body(self):
        before = (self.tmp / "pipeline.sqlite3").read_bytes()
        self.run_intake("01-full-enus.xlf", "01-full-enus.json")
        self.assertEqual((self.tmp / "pipeline.sqlite3").read_bytes(), before)
        store = self.store()
        try:
            self.assertEqual(
                store.unit_by_id("u-01")[0]["source_text"], self.full[31:97])
        finally:
            store.close()


    def test_17_mapping_duplicate_original_refused(self):
        mapping = json.loads(
            (MAPPINGS / "01-full-enus.json").read_text(encoding="utf-8"))
        mapping["files"][1]["original"] = mapping["files"][0]["original"]
        path = self.work / "dup.json"
        path.write_text(json.dumps(mapping, ensure_ascii=False),
                        encoding="utf-8")
        with self.assertRaises(MappingError):
            load_mapping(path)

    def test_18_declared_language_and_inline_errors(self):
        review_text = (REVIEWS / "01-full-enus.xlf").read_text(encoding="utf-8")
        # source xml:lang disagrees with the file language
        bad = review_text.replace('xml:lang="zh-CN"', 'xml:lang="ja-JP"', 1)
        review = self.work / "lang.xlf"
        review.write_text(bad, encoding="utf-8")
        job = IntakeJob(self.work, self.tmp / "pipeline.sqlite3")
        try:
            outcome = job.intake(review, MAPPINGS / "01-full-enus.json")
        finally:
            job.close()
        self.assertEqual(outcome.exit_code, 4)
        self.assertEqual(outcome.summary["unit_results"][0]["reason_code"],
                         "unit_language_mismatch")
        # an x without equiv-text
        bad2 = review_text.replace(
            '<x id="2" equiv-text="〔KX-2026-01〕"/>', '<x id="2"/>', 1)
        review2 = self.work / "equiv.xlf"
        review2.write_text(bad2, encoding="utf-8")
        job = IntakeJob(self.work, self.tmp / "pipeline.sqlite3")
        try:
            outcome2 = job.intake(review2, MAPPINGS / "01-full-enus.json")
        finally:
            job.close()
        self.assertEqual(outcome2.summary["unit_results"][0]["reason_code"],
                         "missing_equiv_text")
        # a whitespace-only target is never filled from the source
        bad3 = review_text.replace(
            '<target state="final" xml:lang="en-US">To all relevant units: the '
            '2026 research cooperation project application arrangements are '
            'announced below.</target>',
            '<target state="final" xml:lang="en-US">   </target>', 1)
        review3 = self.work / "blank.xlf"
        review3.write_text(bad3, encoding="utf-8")
        job = IntakeJob(self.work, self.tmp / "pipeline.sqlite3")
        try:
            outcome3 = job.intake(review3, MAPPINGS / "01-full-enus.json")
        finally:
            job.close()
        head = [u for u in outcome3.summary["unit_results"]
                if u["unit_id"] == "u-head"][0]
        self.assertEqual(head["reason_code"], "empty_target")
        self.assertEqual(self.store().unit_by_id(
            "u-head", outcome3.delivery_id)[0]["status"], "rejected")

    def test_19_cli_exit_codes_and_workdir(self):
        work = self.work
        code = cli_main(["--workdir", str(work),
                         "--pipeline-db", str(self.tmp / "pipeline.sqlite3"),
                         "intake", str(REVIEWS / "01-full-enus.xlf"),
                         "--bindings", str(MAPPINGS / "01-full-enus.json")])
        self.assertEqual(code, 0)
        code = cli_main(["--workdir", str(work),
                         "--pipeline-db", str(self.tmp / "pipeline.sqlite3"),
                         "intake",
                         str(REVIEWS / "03-mixed-rejections.xlf"),
                         "--bindings",
                         str(MAPPINGS / "03-mixed-rejections.json")])
        self.assertEqual(code, 4)
        code = cli_main(["--workdir", str(work),
                         "--pipeline-db", str(self.tmp / "pipeline.sqlite3"),
                         "intake", str(REVIEWS / "07-broken.xlf"),
                         "--bindings", str(MAPPINGS / "01-full-enus.json")])
        self.assertEqual(code, 3)
        code = cli_main(["--workdir", str(work),
                         "--pipeline-db", str(self.tmp / "pipeline.sqlite3"),
                         "intake",
                         str(REVIEWS / "06-conflict-enus.xlf"),
                         "--bindings", str(MAPPINGS / "06-conflict-enus.json"),
                         "--fail-point", "before_commit"])
        self.assertEqual(code, 5)
        self.assertEqual(cli_main(["--workdir", str(work), "usable"]), 0)
        self.assertEqual(cli_main(["--workdir", str(work), "groups",
                                   "--state", "available"]), 0)
        self.assertEqual(cli_main(["--workdir", str(work), "issues",
                                   "--unit-status", "rejected"]), 0)

    def test_20_duplicate_original_in_document(self):
        text = (REVIEWS / "01-full-enus.xlf").read_text(encoding="utf-8")
        # both file elements now carry the same original
        bad = text.replace(
            'original="notices/X-4001-application.html#head"',
            'original="notices/X-4001-application.html"')
        review = self.work / "dupfile.xlf"
        review.write_text(bad, encoding="utf-8")
        job = IntakeJob(self.work, self.tmp / "pipeline.sqlite3")
        try:
            outcome = job.intake(review, MAPPINGS / "01-full-enus.json")
        finally:
            job.close()
        self.assertEqual(outcome.exit_code, 4)
        for result in outcome.summary["file_results"]:
            self.assertEqual(result["reason_code"], "duplicate_original")
        store = self.store()
        try:
            self.assertFalse(store.candidates())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()

"""Schedule-organizing job: date parsing, provenance, replay, cancellations.

Expected dates, paragraph/character locations and cross-notice effects are
listed independently of the scenario HTML, then checked against the actual
database: two-round notices, cross-year windows, year-less dates filled from
the issue date, deadline-only extensions with history, whole-round vs
single-node cancellation, external numbering, ambiguous references,
idempotent re-organization, crash recovery and vocabulary changes.

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

from archive_pipeline.schedule import (  # noqa: E402
    ScheduleInterrupted,
    ScheduleJob,
    ScheduleStore,
)
from archive_pipeline.scheduleconfig import (  # noqa: E402
    ScheduleConfigError,
    load_schedule_vocab,
)
from archive_pipeline.scheduledate import (  # noqa: E402
    DateParseError,
    find_dates,
    find_ranges,
)
from archive_pipeline.scheduleextract import (  # noqa: E402
    extract_notice,
    split_paragraphs,
)
from archive_pipeline.schedulesample import (  # noqa: E402
    aid_for,
    build_sample_workdir,
)

DEFAULT_VOCAB = ROOT / "config" / "schedule_vocab.json"


# ---------------------------------------------------------------------------
# Date parser unit tests
# ---------------------------------------------------------------------------
class DateParserTests(unittest.TestCase):
    def test_full_forms(self):
        mentions = find_dates(
            "2026年3月10日 2026-03-10 2026/3/10 2026.3.10", "2026-03-01")
        self.assertEqual(
            [m.iso for m in mentions],
            ["2026-03-10", "2026-03-10", "2026-03-10", "2026-03-10"])
        self.assertTrue(all(m.had_year for m in mentions))
        self.assertEqual(
            [m.text for m in mentions],
            ["2026年3月10日", "2026-03-10", "2026/3/10", "2026.3.10"])

    def test_chinese_numerals(self):
        mentions = find_dates("二〇二六年十月十二日", "2026-10-01")
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].iso, "2026-10-12")
        mentions = find_dates("截止到十一月五日", "2026-10-01")
        self.assertEqual(mentions[0].iso, "2026-11-05")

    def test_year_less_uses_issue_year(self):
        mentions = find_dates("7月1日至7月20日", "2026-06-18")
        self.assertEqual([m.iso for m in mentions],
                         ["2026-07-01", "2026-07-20"])
        self.assertTrue(all(not m.had_year for m in mentions))
        # never guess across a year boundary
        mentions = find_dates("1月5日", "2026-12-30")
        self.assertEqual(mentions[0].iso, "2026-01-05")

    def test_invalid_calendar_date_rejected(self):
        with self.assertRaises(DateParseError):
            find_dates("2026年13月40日", "2026-01-01")

    def test_full_width_digits(self):
        mentions = find_dates("２０２６年３月１０日", "2026-01-01")
        self.assertEqual(mentions[0].iso, "2026-03-10")
        self.assertEqual(mentions[0].text, "２０２６年３月１０日")

    def test_ranges(self):
        standalone, ranges = find_ranges("3月10日至3月25日", "2026-01-01")
        self.assertEqual(standalone, [])
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0].start.iso, "2026-03-10")
        self.assertEqual(ranges[0].end.iso, "2026-03-25")
        self.assertEqual(ranges[0].connector, "至")

    def test_spans_point_back_to_source(self):
        text = "开头2026年3月10日结尾"
        m = find_dates(text, "2026-01-01")[0]
        self.assertEqual(text[m.start:m.end], "2026年3月10日")


# ---------------------------------------------------------------------------
# Extractor unit tests
# ---------------------------------------------------------------------------
class ExtractorTests(unittest.TestCase):
    def setUp(self):
        self.vocab = load_schedule_vocab(DEFAULT_VOCAB)

    def test_paragraph_and_sentence_spans(self):
        body = ("第一段没有日期。\n\n"
                "报名开放时间为2026年3月10日，材料提交截止时间为2026年3月25日。")
        paragraphs = split_paragraphs(body)
        self.assertEqual(len(paragraphs), 2)
        extraction = extract_notice(
            title="项目申报通知", notice_key="T-1", issued_on="2026-03-01",
            body=body, vocab=self.vocab)
        self.assertEqual(extraction.notice_type, "application")
        self.assertEqual(len(extraction.dates), 2)
        for ev in extraction.dates:
            self.assertEqual(ev.paragraph_index, 1)
            self.assertEqual(body[ev.date_start:ev.date_end], ev.raw_date)
            self.assertEqual(body[ev.sentence_start:ev.sentence_end],
                             ev.sentence_text)

    def test_two_rounds_get_separate_round_numbers(self):
        body = ("第一轮报名开放时间为2026年3月10日。\n\n"
                "第二轮报名开放时间为2026年5月8日。")
        extraction = extract_notice(
            title="申报通知", notice_key="T-2", issued_on="2026-03-01",
            body=body, vocab=self.vocab)
        rounds = sorted((ev.round_no, ev.normalized_date)
                        for ev in extraction.dates)
        self.assertEqual(rounds, [(1, "2026-03-10"), (2, "2026-05-08")])

    def test_unmapped_date_is_pending_not_guessed(self):
        body = "项目宣讲会日期为2026年11月6日。"
        extraction = extract_notice(
            title="申报通知", notice_key="T-3", issued_on="2026-11-01",
            body=body, vocab=self.vocab)
        self.assertEqual(extraction.dates, [])
        self.assertEqual([p.code for p in extraction.pending],
                         ["unmapped_date"])

    def test_cancellation_sentence_makes_no_date_node(self):
        body = ("专家评审开始时间为2026年8月10日。\n\n"
                "经研究取消评审，受理安排不受影响。")
        extraction = extract_notice(
            title="取消评审的通知", notice_key="T-4", issued_on="2026-08-01",
            body=body, vocab=self.vocab)
        self.assertEqual(extraction.notice_type, "withdrawal")
        self.assertEqual(len(extraction.cancels_node), 1)
        self.assertEqual(len(extraction.dates), 1)  # only the real review date

    def test_reference_parsing_ascii_and_cjk_boundary(self):
        body = "现就编号：S-1006通知中的时间安排更正如下。"
        extraction = extract_notice(
            title="更正通知", notice_key="T-5", issued_on="2026-07-01",
            body=body, vocab=self.vocab)
        refs = [h for h in extraction.target_hints if h["kind"] == "explicit"]
        self.assertEqual(refs[0]["target_key"], "S-1006")
        self.assertIsNone(refs[0]["target_source"])

    def test_reference_with_explicit_source(self):
        body = "现就来源：schedsim2，编号：X-2001通知涉及的安排补充如下。"
        extraction = extract_notice(
            title="补充通知", notice_key="T-6", issued_on="2026-09-01",
            body=body, vocab=self.vocab)
        refs = [h for h in extraction.target_hints if h["kind"] == "explicit"]
        self.assertEqual(refs[0]["target_key"], "X-2001")
        self.assertEqual(refs[0]["target_source"], "schedsim2")


# ---------------------------------------------------------------------------
# End-to-end tests on the reproducible sample
# ---------------------------------------------------------------------------
# Independently hand-computed expected current schedule, keyed by
# (project, round, role): normalized date / status.
EXPECTED_CURRENT = {
    ("schedsim::S-1001", 1, "apply_open"): ("2026-03-10", "active"),
    ("schedsim::S-1001", 1, "submit_deadline"): ("2026-04-08", "active"),
    ("schedsim::S-1001", 1, "review_start"): ("2026-04-05", "active"),
    ("schedsim::S-1002", 1, "apply_open"): ("2026-12-25", "active"),
    ("schedsim::S-1002", 1, "submit_deadline"): ("2027-01-15", "active"),
    ("schedsim::S-1002", 1, "review_start"): ("2027-02-10", "active"),
    ("schedsim::S-1005", 1, "apply_open"): ("2026-07-01", "active"),
    ("schedsim::S-1005", 1, "submit_deadline"): ("2026-07-20", "active"),
    ("schedsim::S-1005", 1, "review_start"): ("2026-08-03", "active"),
    ("schedsim::S-1006", 1, "apply_open"): ("2026-07-15", "active"),
    ("schedsim::S-1006", 1, "submit_deadline"): ("2026-07-30", "active"),
    ("schedsim::S-1006", 1, "review_start"): (None, "cancelled"),
    ("schedsim2::X-2001", 1, "apply_open"): ("2026-09-01", "active"),
    ("schedsim2::X-2001", 1, "submit_deadline"): ("2026-09-25", "active"),
    ("schedsim::S-1007", 1, "apply_open"): ("2026-08-05", "active"),
    ("schedsim::S-1007", 1, "submit_deadline"): ("2026-08-18", "active"),
    ("schedsim::S-1011", 1, "apply_open"): ("2026-10-12", "active"),
    ("schedsim::S-1011", 1, "submit_deadline"): ("2026-10-28", "active"),
    ("schedsim::S-1012", 1, "apply_open"): ("2026-11-10", "active"),
    ("schedsim::S-1012", 1, "submit_deadline"): ("2026-11-25", "active"),
}


class ScheduleJobTests(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp())
        self.built = build_sample_workdir(self.workdir)
        self.job = ScheduleJob(self.workdir, DEFAULT_VOCAB)
        self.summary = self.job.organize()

    def tearDown(self):
        self.job.close()
        shutil.rmtree(self.workdir)

    def _node(self, project, round_no, role):
        rows = self.job.store.current_nodes(project, round_no)
        hits = [r for r in rows if r["role_name"] == role]
        self.assertEqual(len(hits), 1, (project, round_no, role))
        return hits[0]

    def test_expected_current_schedule(self):
        nodes = self.job.store.current_nodes()
        actual = {(n["project_key"], n["round_no"], n["role_name"]):
                  (n["normalized_date"], n["status"]) for n in nodes}
        self.assertEqual(actual, EXPECTED_CURRENT)

    def test_two_rounds_built_from_one_notice(self):
        rounds = {(r["round_no"], r["status"])
                  for r in self.job.store.rounds("schedsim::S-1001")}
        self.assertEqual(rounds, {(1, "open"), (2, "round_cancelled")})

    def test_cross_year_dates_keep_real_year(self):
        self.assertEqual(
            self._node("schedsim::S-1002", 1, "apply_open")["normalized_date"],
            "2026-12-25")
        self.assertEqual(
            self._node("schedsim::S-1002", 1, "review_start")["normalized_date"],
            "2027-02-10")

    def test_year_less_dates_filled_from_issue_date(self):
        evidence = [e for e in self.job.store.evidence(
            "schedsim", aid_for(self.built, "schedsim", "S-1005"), 1,
            self.job.vocab.digest)]
        self.assertTrue(evidence)
        self.assertTrue(all(e["had_year"] == 1 for e in evidence))
        self.assertEqual(
            self._node("schedsim::S-1005", 1, "apply_open")["normalized_date"],
            "2026-07-01")

    def test_deadline_extension_keeps_history_hides_old_date(self):
        ledger = [r for r in self.job.store.ledger("schedsim::S-1001")
                  if r["role_name"] == "submit_deadline" and r["round_no"] == 1
                  and r["action"] == "set"]
        dated = [(r["normalized_date"], r["notice_key"], r["version_no"])
                 for r in ledger]
        self.assertIn(("2026-03-25", "S-1001", 1), dated)       # old date
        self.assertIn(("2026-04-08", "S-1003", 2), dated)       # new date
        current = self._node("schedsim::S-1001", 1,
                             "submit_deadline")["normalized_date"]
        self.assertEqual(current, "2026-04-08")
        # other round-1 nodes stay untouched
        self.assertEqual(
            self._node("schedsim::S-1001", 1, "apply_open")["normalized_date"],
            "2026-03-10")

    def test_whole_round_cancellation_scope(self):
        # round 2 disappears from the current node query
        self.assertEqual(
            self.job.store.current_nodes("schedsim::S-1001", 2), [])
        cancelled = [r for r in self.job.store.ledger("schedsim::S-1001")
                     if r["action"] == "cancel_round"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["round_no"], 2)
        # round 1 remains fully open
        self.assertTrue(all(n["status"] == "active" for n in
                            self.job.store.current_nodes("schedsim::S-1001", 1)))

    def test_single_node_cancel_does_not_cancel_round(self):
        node = self._node("schedsim::S-1006", 1, "review_start")
        self.assertEqual(node["status"], "cancelled")
        self.assertIsNone(node["normalized_date"])
        round_row = [r for r in self.job.store.rounds("schedsim::S-1006")][0]
        self.assertEqual(round_row["status"], "open")
        # the other two nodes stay active
        self.assertEqual(
            self._node("schedsim::S-1006", 1, "apply_open")["status"], "active")
        cancel = [r for r in self.job.store.ledger("schedsim::S-1006")
                  if r["action"] == "cancel_node"]
        self.assertEqual(len(cancel), 1)
        self.assertEqual(cancel[0]["role_name"], "review_start")

    def test_correction_changes_only_targeted_role(self):
        # S-1008 corrects S-1006's review date to 08-20; the later S-1013
        # cancellation then removes that node.  The correction itself is in
        # the ledger and did not touch apply_open / submit_deadline.
        corrections = [r for r in self.job.store.ledger("schedsim::S-1006")
                       if r["notice_key"] == "S-1008"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["role_name"], "review_start")
        self.assertEqual(corrections[0]["normalized_date"], "2026-08-20")
        self.assertEqual(
            self._node("schedsim::S-1006", 1, "submit_deadline")
            ["normalized_date"], "2026-07-30")

    def test_explicit_cross_source_reference_resolves(self):
        node = self._node("schedsim2::X-2001", 1, "submit_deadline")
        self.assertEqual(node["normalized_date"], "2026-09-25")
        ledger = [r for r in self.job.store.ledger("schedsim2::X-2001")
                  if r["notice_key"] == "S-1009"]
        self.assertEqual(len(ledger), 1)

    def test_pending_items_are_never_merged(self):
        codes = {(p["notice_key"], p["code"])
                 for p in self.job.store.pending()}
        self.assertIn(("S-1010", "target_unspecified"), codes)
        self.assertIn(("S-1010", "vague_statement"), codes)
        self.assertIn(("S-1012", "unmapped_date"), codes)
        # S-1014 cites the duplicated S-1007 number: two identities match,
        # so nothing is merged and an operator must disambiguate.
        ambiguous = [p for p in self.job.store.pending()
                     if p["notice_key"] == "S-1014"]
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["code"], "target_ambiguous")
        self.assertEqual(ambiguous[0]["target_key"], "S-1007")
        # neither S-1007 edition was modified by the unresolved supplement
        for node in self.job.store.current_nodes("schedsim::S-1007"):
            self.assertEqual(node["status"], "active")
        # pending notices created no nodes
        keys = {n["project_key"] for n in self.job.store.current_nodes()}
        self.assertNotIn("schedsim::S-1010", keys)
        self.assertNotIn("schedsim::S-1014", keys)

    def test_on_date_query_and_explain_provenance(self):
        rows = self.job.store.current_nodes(on_date="2026-04-08")
        self.assertEqual(len(rows), 1)
        detail = self.job.store.evidence_for_ledger(rows[0]["ledger_id"])
        evidence = json.loads(detail["evidence_json"])
        self.assertEqual(evidence["raw_date"], "2026年4月8日")
        self.assertEqual(evidence["normalized_date"], "2026-04-08")
        self.assertEqual(detail["notice_key"], "S-1003")
        self.assertEqual(detail["version_no"], 2)
        self.assertGreaterEqual(evidence["date_end"], evidence["date_start"])

    def test_idempotent_reorganize_adds_nothing(self):
        before = len(self.job.store.ledger())
        second = self.job.organize()
        self.assertEqual(second["ledger_rows_added"], 0)
        self.assertEqual(len(self.job.store.ledger()), before)
        self.assertEqual(
            len(self.job.store.current_nodes()),
            len(EXPECTED_CURRENT))

    def test_interrupt_leaves_no_partial_publish_and_restarts(self):
        workdir2 = Path(tempfile.mkdtemp())
        try:
            built2 = build_sample_workdir(workdir2)
            target = aid_for(built2, "schedsim", "S-1003")
            job2 = ScheduleJob(workdir2, DEFAULT_VOCAB)
            with self.assertRaises(ScheduleInterrupted):
                job2.organize(fail_point=(target, 2))
            self.assertEqual(job2.store.runs()[-1]["status"], "interrupted")
            # nothing published, no half-visible schedule
            self.assertEqual(job2.store.current_nodes(), [])
            self.assertEqual(job2.store.rounds(), [])
            # the interrupted version's own transaction rolled back
            self.assertFalse(any(
                e["archive_id"] == target for e in job2.store.extractions()))
            job2.close()
            job3 = ScheduleJob(workdir2, DEFAULT_VOCAB)
            summary = job3.organize()
            self.assertEqual(summary["pending_open"], 4)
            self.assertEqual(len(job3.store.current_nodes()),
                             len(EXPECTED_CURRENT))
            job3.close()
        finally:
            shutil.rmtree(workdir2)

    def test_vocab_change_reorganizes_but_keeps_old_judgment(self):
        old_digest = self.job.vocab.digest
        fixed_path = self.workdir / "vocab-fixed.json"
        cfg = json.loads(DEFAULT_VOCAB.read_text("utf-8"))
        cfg["roles"]["briefing"] = {
            "kind": "open", "aliases": ["宣讲会日期", "项目宣讲会"]}
        fixed_path.write_text(json.dumps(cfg, ensure_ascii=False))
        self.job.close()
        job2 = ScheduleJob(self.workdir, fixed_path)
        self.assertNotEqual(job2.vocab.digest, old_digest)
        job2.organize()
        node = [n for n in job2.store.current_nodes("schedsim::S-1012")
                if n["role_name"] == "briefing"]
        self.assertEqual(len(node), 1)
        self.assertEqual(node[0]["normalized_date"], "2026-11-06")
        # old judgment stays in history with the old digest
        history = job2.store.pending(current_only=False)
        old_rows = [h for h in history if h["config_digest"] == old_digest]
        self.assertTrue(any(h["notice_key"] == "S-1012" for h in old_rows))
        # and is closed in the current pending view
        self.assertFalse(any(
            p["notice_key"] == "S-1012" for p in job2.store.pending()))
        job2.close()

    def test_pipeline_and_archive_remain_untouched(self):
        import hashlib
        pipe = self.workdir / "pipeline.sqlite3"
        before = hashlib.sha256(pipe.read_bytes()).hexdigest()
        archive_before = {
            str(p.relative_to(self.workdir)):
                hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.workdir / "archive").rglob("*") if p.is_file()}
        self.job.organize()
        self.assertEqual(
            hashlib.sha256(pipe.read_bytes()).hexdigest(), before)
        archive_after = {
            str(p.relative_to(self.workdir)):
                hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.workdir / "archive").rglob("*") if p.is_file()}
        self.assertEqual(archive_after, archive_before)

    def test_legacy_pipeline_database_is_refused(self):
        legacy = Path(tempfile.mkdtemp())
        try:
            import sqlite3
            db = legacy / "pipeline.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA user_version=1")
            conn.execute("CREATE TABLE versions (x TEXT)")
            conn.commit()
            conn.close()
            with self.assertRaises(Exception):
                ScheduleJob(legacy, DEFAULT_VOCAB)
        finally:
            shutil.rmtree(legacy)


class ConfigValidationTests(unittest.TestCase):
    def test_overlapping_aliases_rejected(self):
        workdir = Path(tempfile.mkdtemp())
        try:
            cfg = json.loads(DEFAULT_VOCAB.read_text("utf-8"))
            cfg["roles"]["review_start"]["aliases"].append("报名开放")
            bad = workdir / "bad.json"
            bad.write_text(json.dumps(cfg, ensure_ascii=False))
            with self.assertRaises(ScheduleConfigError):
                load_schedule_vocab(bad)
        finally:
            shutil.rmtree(workdir)


if __name__ == "__main__":
    unittest.main()

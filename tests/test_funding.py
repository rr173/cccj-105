"""Funding-table structuring job: grid expansion, amounts, end-to-end facts.

Expected facts are listed independently of the raw tables (hand-computed
from the sample HTML), then checked against the actual database: amounts in
exact yuan, provenance cells, rejection isolation, idempotent reprocessing,
mid-table interruption, and config-fix reprocessing.

Run: ``python -m unittest discover -s tests -v`` from the project root.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from archive_pipeline.fundamount import (
    extract_table_units,
    format_yuan,
    parse_amount,
    split_unit_annotation,
    to_yuan,
)
from archive_pipeline.fundconfig import load_funding_tables_config
from archive_pipeline.fundgrid import TableRejection, build_grid
from archive_pipeline.funding import (
    FundingError,
    FundingInterrupted,
    FundingJob,
)
from archive_pipeline.fundsample import aid_for, build_sample_workdir
from archive_pipeline.fundtable import classify_columns, interpret
from archive_pipeline.htmlparse import parse_html
from archive_pipeline.store import Store

DEFAULT_CFG_PATH = ROOT / "config" / "funding_tables.json"


def grid_of(html_text: str):
    root, _ = parse_html(html_text)
    table = root.first("table")
    return build_grid(table)


def rejection_of(html_text: str) -> TableRejection:
    root, _ = parse_html(html_text)
    try:
        build_grid(root.first("table"))
    except TableRejection as rej:
        return rej
    raise AssertionError("expected a TableRejection")


class GridTests(unittest.TestCase):
    def test_basic_expansion_and_inheritance(self):
        grid = grid_of(
            "<table><tr><th rowspan='2'>机构</th><th>2024年度</th></tr>"
            "<tr><th>资助金额</th></tr>"
            "<tr><td rowspan='2'>机构A</td><td>100</td></tr>"
            "<tr><td>200</td></tr></table>")
        self.assertEqual((grid.n_rows, grid.width, grid.header_rows), (4, 2, 2))
        # The rowspan institution cell covers both data rows.
        self.assertIs(grid.cell_at(2, 0), grid.cell_at(3, 0))
        self.assertEqual(grid.cell_at(3, 0).row, 2)
        # The colspan-free year header sits at (0,1); sub-header at (1,1).
        self.assertEqual(grid.cell_at(0, 1).text, "2024年度")
        self.assertEqual(grid.cell_at(1, 1).text, "资助金额")

    def test_colspan_expansion(self):
        grid = grid_of(
            "<table><tr><th>机构</th><th colspan='2'>2024年度</th></tr>"
            "<tr><td>A</td><td>1</td><td>2</td></tr></table>")
        self.assertEqual(grid.width, 3)
        top = grid.cell_at(0, 1)
        self.assertIs(top, grid.cell_at(0, 2))
        self.assertEqual(top.colspan, 2)

    def test_overlap_merge_rejected(self):
        rej = rejection_of(
            "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>x</td><td rowspan='2'>shared</td></tr>"
            "<tr><td colspan='2'>merged</td></tr></table>")
        self.assertEqual(rej.code, "overlap_merge")
        self.assertIn("r2c0", rej.detail)

    def test_rowspan_out_of_bounds(self):
        rej = rejection_of(
            "<table><tr><th>A</th></tr><tr><td rowspan='3'>x</td></tr></table>")
        self.assertEqual(rej.code, "rowspan_out_of_bounds")

    def test_grid_hole_rejected(self):
        rej = rejection_of(
            "<table><tr><th>A</th><th>B</th><th>C</th></tr>"
            "<tr><td>1</td><td>2</td></tr></table>")
        self.assertEqual(rej.code, "grid_hole")

    def test_nested_table_rejected_both_directions(self):
        rej = rejection_of(
            "<table><tr><th>A</th></tr>"
            "<tr><td><table><tr><td>inner</td></tr></table></td></tr></table>")
        self.assertEqual(rej.code, "nested_table")

    def test_invalid_span_rejected(self):
        rej = rejection_of(
            "<table><tr><th>A</th></tr><tr><td rowspan='0'>x</td></tr></table>")
        self.assertEqual(rej.code, "invalid_span")

    def test_misplaced_cell_rejected(self):
        rej = rejection_of(
            "<table><tr><th>A</th></tr><td>loose</td></table>")
        self.assertEqual(rej.code, "misplaced_cell")

    def test_header_detection(self):
        rej = rejection_of("<table><tr><td>1</td><td>2</td></tr></table>")
        self.assertEqual(rej.code, "no_header")
        rej = rejection_of(
            "<table><tr><th>a</th></tr><tr><th>b</th></tr>"
            "<tr><th>c</th></tr><tr><td>1</td></tr></table>")
        self.assertEqual(rej.code, "unsupported_header_depth")


class AmountTests(unittest.TestCase):
    def test_plain_and_grouped(self):
        self.assertEqual(parse_amount("1234"), Decimal("1234"))
        self.assertEqual(parse_amount("1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_amount("1，234"), Decimal("1234"))  # full-width comma
        self.assertEqual(parse_amount(" 500 "), Decimal("500"))

    def test_fullwidth_digits_and_decimal_point(self):
        self.assertEqual(parse_amount("２０００"), Decimal("2000"))
        self.assertEqual(parse_amount("１，２３４．５"), Decimal("1234.5"))

    def test_bad_grouping_and_prose_rejected(self):
        for bad in ("12,34", "1,23,456", "abc", "1.2.3", "约100", "100元"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_amount(bad))

    def test_placeholders_are_not_zero(self):
        for token in ("", "—", "–", "-", "--", "待定", "暂无", "N/A"):
            with self.subTest(token=token):
                self.assertIsNone(parse_amount(token))

    def test_exact_decimal_conversion_no_float(self):
        # 0.1 cannot be represented in binary floating point; Decimal is exact.
        self.assertEqual(format_yuan(to_yuan(parse_amount("0.1"), "wan")), "1000")
        self.assertEqual(format_yuan(to_yuan(parse_amount("0.1235"), "wan")), "1235")
        self.assertEqual(format_yuan(to_yuan(parse_amount("1.25"), "thousand")), "1250")
        self.assertEqual(format_yuan(to_yuan(parse_amount("1,234.56"), "yuan")),
                         "1234.56")


class UnitTests(unittest.TestCase):
    def test_caption_forms(self):
        self.assertEqual(extract_table_units("重点机构资助明细（单位：元）"), (["yuan"], []))
        self.assertEqual(extract_table_units("单位：人民币万元"), (["wan"], []))
        self.assertEqual(extract_table_units("明细（千元）"), (["thousand"], []))

    def test_unknown_currency_detected(self):
        units, unknowns = extract_table_units("外币资助表（单位：美元）")
        self.assertEqual(units, [])
        self.assertEqual(unknowns, ["美元"])

    def test_conflicting_caption_units(self):
        units, _ = extract_table_units("单位：元 另见（万元）")
        self.assertEqual(sorted(set(units)), ["wan", "yuan"])

    def test_header_unit_annotation(self):
        core, units, unknowns = split_unit_annotation("资助金额（万元）")
        self.assertEqual((core, units, unknowns), ("资助金额", ["wan"], []))
        core, units, unknowns = split_unit_annotation("资助金额（美元）")
        self.assertEqual((core, units), ("资助金额", []))
        self.assertEqual(unknowns, ["美元"])


HEADER = ("<tr><th rowspan='2'>机构</th><th rowspan='2'>资助项目</th>"
          "<th>2024年度</th></tr><tr><th>资助金额</th></tr>")


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_funding_tables_config(DEFAULT_CFG_PATH)

    def classify_html(self, html_text: str, cfg=None):
        return classify_columns(grid_of(html_text), cfg or self.cfg)

    def test_unmapped_column_rejected(self):
        grid = grid_of(
            "<table><tr><th>机构</th><th>负责人</th></tr>"
            "<tr><td>A</td><td>B</td></tr></table>")
        with self.assertRaises(TableRejection) as ctx:
            classify_columns(grid, self.cfg)
        self.assertEqual(ctx.exception.code, "unmapped_column")

    def test_ambiguous_year_group_rejected(self):
        grid = grid_of(
            "<table><tr><th rowspan='2'>机构</th><th rowspan='2'>资助项目</th>"
            "<th colspan='2'>2024年度</th></tr>"
            "<tr><th>资助金额</th><th>金额</th></tr>"
            "<tr><td>A</td><td>P</td><td>1</td><td>2</td></tr></table>")
        with self.assertRaises(TableRejection) as ctx:
            classify_columns(grid, self.cfg)
        self.assertEqual(ctx.exception.code, "column_mapping")

    def test_duplicate_year_rejected(self):
        grid = grid_of(
            "<table><tr><th rowspan='2'>机构</th><th rowspan='2'>资助项目</th>"
            "<th>2024年度</th><th>2024年度</th></tr>"
            "<tr><th>资助金额</th><th>资助金额</th></tr>"
            "<tr><td>A</td><td>P</td><td>1</td><td>2</td></tr></table>")
        with self.assertRaises(TableRejection) as ctx:
            classify_columns(grid, self.cfg)
        self.assertEqual(ctx.exception.code, "duplicate_year")

    def test_column_unit_overrides_table_unit(self):
        grid = grid_of(
            "<table>" + HEADER.replace("资助金额</th>", "资助金额（万元）</th>")
            + "<tr><td>A</td><td>P</td><td>1.5</td></tr></table>")
        colmap = classify_columns(grid, self.cfg)
        result = interpret(grid, colmap, "明细（单位：元）", self.cfg)
        cand = result.candidates[0].amounts[0]
        self.assertEqual(cand.unit, "wan")
        self.assertEqual(result.unit_sources[2024], "column")
        self.assertEqual(format_yuan(cand.amount_yuan), "15000")

    def test_missing_unit_rejected(self):
        grid = grid_of("<table>" + HEADER + "<tr><td>A</td><td>P</td><td>1</td></tr></table>")
        colmap = classify_columns(grid, self.cfg)
        with self.assertRaises(TableRejection) as ctx:
            interpret(grid, colmap, "无单位说明", self.cfg)
        self.assertEqual(ctx.exception.code, "missing_unit")

    def test_amount_spanning_rows_rejected(self):
        grid = grid_of(
            "<table>" + HEADER +
            "<tr><td rowspan='2'>A</td><td>P1</td><td rowspan='2'>100</td></tr>"
            "<tr><td>P2</td></tr></table>")
        colmap = classify_columns(grid, self.cfg)
        with self.assertRaises(TableRejection) as ctx:
            interpret(grid, colmap, "单位：元", self.cfg)
        self.assertEqual(ctx.exception.code, "amount_spans_rows")

    def test_total_row_not_booked_but_reconciled(self):
        grid = grid_of(
            "<table>" + HEADER +
            "<tr><td>A</td><td>P</td><td>100</td></tr>"
            "<tr><th colspan='2'>合计</th><td>100</td></tr></table>")
        colmap = classify_columns(grid, self.cfg)
        result = interpret(grid, colmap, "单位：元", self.cfg)
        self.assertEqual(len(result.candidates), 1)  # only the detail row
        self.assertEqual(result.totals[2024], Decimal("100"))


class FundingJobTestCase(unittest.TestCase):
    """Builds the reproducible sample workdir once per class, copied per test."""

    @classmethod
    def setUpClass(cls):
        cls._class_tmp = Path(tempfile.mkdtemp(prefix="fund-class-"))
        cls._template = cls._class_tmp / "template"
        cls.built = build_sample_workdir(cls._template)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._class_tmp, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fund-it-"))
        self.workdir = self.tmp / "work"
        shutil.copytree(self._template, self.workdir)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def aid(self, key: str, file: str | None) -> str:
        return aid_for(self.built, key, file)

    def run_job(self, **kwargs) -> dict:
        job = FundingJob(self.workdir, kwargs.pop("tables_config", None))
        try:
            summary = job.process(**kwargs)
            self.store = job.store
            return summary
        finally:
            # Keep the store open for assertions; close pipeline only.
            job.pipeline.close()

    def fixed_config(self) -> Path:
        raw = json.loads(DEFAULT_CFG_PATH.read_text(encoding="utf-8"))
        raw["headers"]["institution"].append("承担单位")
        raw["headers"]["project"].append("拨付项目")
        raw["headers"]["amount"].append("拨付经费")
        path = self.tmp / "fixed.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return path


# Hand-computed expectations, listed independently of the raw HTML.
EXPECTED_FULL = [
    # (row, institution, project, year, amount_yuan, amount_cell, inst_cell)
    (2, "中科院数学与系统科学研究院", "基础理论专项", 2024, "1234.56", (2, 2), (2, 0)),
    (2, "中科院数学与系统科学研究院", "基础理论专项", 2025, "15000", (2, 4), (2, 0)),
    (3, "中科院数学与系统科学研究院", "应用转化项目", 2024, "2000", (3, 2), (2, 0)),
    (3, "中科院数学与系统科学研究院", "应用转化项目", 2025, "2500", (3, 4), (2, 0)),
    (4, "江南大学", "联合实验室", 2024, "500", (4, 2), (4, 0)),
    (4, "江南大学", "联合实验室", 2025, "20000", (4, 4), (4, 0)),
]

EXPECTED_PUBLIC = [
    (1, "中科院数学与系统科学研究院", "基础理论专项", 2024, "1235", (1, 2), (1, 0)),
    (1, "中科院数学与系统科学研究院", "基础理论专项", 2025, "15000", (1, 3), (1, 0)),
    (2, "江南大学", "联合实验室", 2024, "500", (2, 2), (2, 0)),
    (2, "江南大学", "联合实验室", 2025, "20000", (2, 3), (2, 0)),
]

EXPECTED_REASONS = {
    "F-3002": "overlap_merge",
    "F-3003": "amount_spans_years",
    "F-3004": "total_mismatch",
    "F-3007": "unmapped_column",
    "F-3008": "unparseable_amount",
    "F-3009": "nested_table",
    "F-3010": "unknown_currency",
    "F-3011": "missing_unit",
}


class EndToEndTests(FundingJobTestCase):
    def test_summary_counts(self):
        summary = self.run_job()
        self.assertEqual(summary["versions_seen"], 12)
        self.assertEqual(summary["booked"], 2)          # F-3001 full + public
        self.assertEqual(summary["rejected_structural"], 4)  # 3002/3003/3007/3009
        self.assertEqual(summary["rejected_data"], 4)        # 3004/3008/3010/3011
        self.assertEqual(summary["version_errors"], 2)       # 3005/3006
        self.assertEqual(summary["facts_written"], 10)

    def test_full_edition_facts_match_expectations(self):
        self.run_job()
        facts = self.store.facts(archive_id=self.aid("F-3001", "F-3001-full.html"))
        actual = [(f["row_index"], f["institution"], f["project"], f["year"],
                   f["amount_yuan"], (f["amount_cell_row"], f["amount_cell_col"]),
                   (f["institution_cell_row"], f["institution_cell_col"]))
                  for f in facts]
        self.assertEqual(actual, EXPECTED_FULL)

    def test_public_edition_facts_match_expectations(self):
        self.run_job()
        facts = self.store.facts(archive_id=self.aid("F-3001", "F-3001-public.html"))
        actual = [(f["row_index"], f["institution"], f["project"], f["year"],
                   f["amount_yuan"], (f["amount_cell_row"], f["amount_cell_col"]),
                   (f["institution_cell_row"], f["institution_cell_col"]))
                  for f in facts]
        self.assertEqual(actual, EXPECTED_PUBLIC)

    def test_same_notice_number_variants_do_not_overwrite(self):
        self.run_job()
        full_aid = self.aid("F-3001", "F-3001-full.html")
        public_aid = self.aid("F-3001", "F-3001-public.html")
        self.assertNotEqual(full_aid, public_aid)
        full = self.store.facts(archive_id=full_aid)
        public = self.store.facts(archive_id=public_aid)
        self.assertEqual({f["notice_key"] for f in full + public}, {"F-3001"})
        self.assertEqual((len(full), len(public)), (6, 4))

    def test_provenance_cells_and_header_inheritance(self):
        self.run_job()
        facts = self.store.facts(archive_id=self.aid("F-3001", "F-3001-full.html"),
                                 row_index=3, year=2024)
        self.assertEqual(len(facts), 1)
        fact = facts[0]
        # Institution inherited from the rowspan cell two rows up.
        self.assertEqual((fact["institution_cell_row"], fact["institution_cell_col"]),
                         (2, 0))
        header_path = json.loads(fact["header_path_json"])
        self.assertEqual([h["text"] for h in header_path], ["2024年度", "资助金额"])
        row_headers = json.loads(fact["row_header_path_json"])
        self.assertEqual(row_headers[0]["rowspan"], 2)
        self.assertEqual(row_headers[0]["role"], "institution")
        self.assertEqual(fact["amount_text"], "２０００")  # original text kept
        self.assertEqual(fact["unit_source"], "table")
        fact2025 = self.store.facts(archive_id=self.aid("F-3001", "F-3001-full.html"),
                                    row_index=2, year=2025)[0]
        self.assertEqual(fact2025["unit_source"], "column")  # 万元 overrides 元
        self.assertEqual(fact2025["amount_yuan"], "15000")

    def test_rejection_reasons_and_isolation(self):
        self.run_job()
        outcomes = self.store.outcomes(status="rejected")
        reasons = {o["notice_key"]: o["reason_code"] for o in outcomes}
        self.assertEqual(reasons, EXPECTED_REASONS)
        # Rejected tables booked nothing, good tables in the same run did.
        self.assertEqual(self.store.fact_count(), 10)
        for o in outcomes:
            self.assertEqual(self.store.facts(archive_id=o["archive_id"]), [])
        # Post-grid failures keep the expanded grid for audit; pre-grid
        # structural failures (overlap, nested) keep the reason and cells.
        with_grid = {"F-3003", "F-3004", "F-3007", "F-3008", "F-3010", "F-3011"}
        for o in outcomes:
            if o["notice_key"] in with_grid:
                self.assertTrue(o["grid_json"], f"grid kept for {o['notice_key']}")
            self.assertTrue(o["reason_detail"])

    def test_total_mismatch_keeps_candidates(self):
        self.run_job()
        aid = self.aid("F-3004", "F-3004-mismatch.html")
        outcome = self.store.outcome("fundsim", aid, 1, 0)
        self.assertEqual(outcome["reason_code"], "total_mismatch")
        detail = json.loads(outcome["reason_detail"])
        self.assertEqual(detail, [{"year": 2024, "details_sum_yuan": "400",
                                   "total_yuan": "500", "difference_yuan": "100"}])
        candidates = json.loads(outcome["candidates_json"])
        self.assertEqual(len(candidates), 2)  # candidate details preserved
        totals = json.loads(outcome["totals_json"])
        self.assertEqual(totals, {})

    def test_dash_amounts_are_pending_not_zero(self):
        self.run_job()
        aid = self.aid("F-3008", "F-3008-dash.html")
        outcome = self.store.outcome("fundsim", aid, 1, 0)
        self.assertEqual(outcome["reason_code"], "unparseable_amount")
        problems = json.loads(outcome["reason_detail"])
        self.assertEqual({(p["row"], p["year"]) for p in problems},
                         {(2, 2025), (3, 2024)})
        candidates = json.loads(outcome["candidates_json"])
        errors = [a["error"] for c in candidates for a in c["amounts"]]
        self.assertEqual(errors.count("unparseable_amount"), 2)
        self.assertEqual(self.store.facts(archive_id=aid), [])

    def test_missing_and_mismatched_raw_fail_explicitly(self):
        self.run_job()
        errors = {e["notice_key"]: e["error_code"]
                  for e in self.store.version_errors()}
        self.assertEqual(errors, {"F-3005": "missing_raw",
                                  "F-3006": "digest_mismatch"})

    def test_reprocessing_is_idempotent(self):
        first = self.run_job()
        count1 = self.store.fact_count()
        job = FundingJob(self.workdir)
        second = job.process()
        count2 = job.store.fact_count()
        job.close()
        self.assertEqual(count1, count2)
        self.assertEqual(second["skipped_booked"], first["booked"])
        self.assertEqual(second["facts_written"], 0)

    def test_mid_table_interruption_leaves_no_partial_facts(self):
        full_aid = self.aid("F-3001", "F-3001-full.html")
        job = FundingJob(self.workdir)
        with self.assertRaises(FundingInterrupted):
            job.process(fail_point=(full_aid, 0))
        self.assertEqual(job.store.facts(archive_id=full_aid), [])
        self.assertIsNone(job.store.outcome("fundsim", full_aid, 1, 0))
        self.assertEqual(job.store.runs()[-1]["status"], "interrupted")
        job.pipeline.close(); job.store.close()
        # Reopening is safe: the table completes and books all 6 facts.
        job = FundingJob(self.workdir)
        job.process()
        self.assertEqual(len(job.store.facts(archive_id=full_aid)), 6)
        self.assertEqual(job.store.fact_count(), 10)
        job.close()

    def test_config_fix_reprocesses_rejected_table(self):
        self.run_job()
        alias_aid = self.aid("F-3007", "F-3007-alias.html")
        before = self.store.outcome("fundsim", alias_aid, 1, 0)
        self.assertEqual(before["reason_code"], "unmapped_column")
        job = FundingJob(self.workdir, self.fixed_config())
        job.process()
        after = job.store.outcome("fundsim", alias_aid, 1, 0)
        self.assertEqual(after["status"], "booked")
        facts = job.store.facts(archive_id=alias_aid)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["amount_yuan"], "1250")  # 1.25 千元
        self.assertEqual(facts[0]["unit"], "thousand")
        # Old diagnostic stays traceable in the history.
        history = [h for h in job.store.history(archive_id=alias_aid)]
        self.assertEqual([h["status"] for h in history], ["rejected", "booked"])
        self.assertEqual(history[0]["reason_code"], "unmapped_column")
        # No duplicates anywhere after the config change reprocessed all tables.
        self.assertEqual(job.store.fact_count(), 11)
        job.close()

    def test_legacy_pipeline_database_refused(self):
        legacy_dir = self.tmp / "legacy"
        store = Store(legacy_dir / "pipeline.sqlite3")
        store.conn.execute("CREATE TABLE url_frontier (url TEXT)")
        store.conn.execute("PRAGMA user_version = 0")
        store.close()
        with self.assertRaises(FundingError) as ctx:
            FundingJob(legacy_dir)
        self.assertIn("pre-identity", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

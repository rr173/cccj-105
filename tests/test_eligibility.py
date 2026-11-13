"""Offline eligibility pre-screen: clause binding, three-valued logic, file safety.

Expected verdicts and key explanation facts are stated **independently** of
the implementation (hand-derived from the sample clause and applications),
then checked against the stored results:

* three-valued all/any/not rules, unknowns never treated as satisfied;
* complete empty member list vs. an incomplete list;
* the member count interval [t, t+u] / [t, ∞), threshold boundary, k=0;
* per-person predicates (degree and years on the same member);
* exact decimal boundaries, no floats;
* forward references, cycles, cross-scope references;
* citation mismatch, wrong variant of the same notice number, missing version;
* structurally damaged input files are non-zero, line numbered, incomplete,
  with a preserved prefix and no success summary — distinct from a field-level
  unknown;
* results reopen and remain traceable; pipeline database stays read-only.

Run: ``python -m unittest discover -s tests -v`` from the project root.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from archive_pipeline.eligclause import ClauseError, load_clause  # noqa: E402
from archive_pipeline.eligdemo import v1_clause  # noqa: E402
from archive_pipeline.eligengine import (  # noqa: E402
    Evaluator,
    explain_result,
    validate_application,
)
from archive_pipeline.eligibility import (  # noqa: E402
    EligibilityJob,
    EligibilityStore,
    explain_application,
)
from archive_pipeline.eligsample import (  # noqa: E402
    build_sample_workdir,
    version_for,
)
from archive_pipeline.eligvalue import parse_decimal_text, read_field  # noqa: E402

CLAUSE_DIR = ROOT / "scenario" / "eligibility" / "clauses"
BASE_INPUT = (ROOT / "scenario" / "eligibility" / "applications"
              / "applications-basic.jsonl")


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


class SampleCase(unittest.TestCase):
    """Builds one sample workdir per test module and the v1 clause."""

    workdir: Path
    built: dict
    clause_path: Path

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="elig-test-")
        cls.workdir = Path(cls._tmp)
        cls.built = build_sample_workdir(cls.workdir)
        cls.clause_payload = v1_clause(cls.built)
        cls.clause_path = _write(cls.workdir / "clause-v1.json",
                                 cls.clause_payload)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def store(self) -> EligibilityStore:
        return EligibilityStore(self.workdir / "eligibility.sqlite3")

    def new_job(self, clause_path: Path | None = None) -> EligibilityJob:
        return EligibilityJob(self.workdir, clause_path or self.clause_path)


# ---------------------------------------------------------------------------
# Field typing
# ---------------------------------------------------------------------------
class FieldTypingTests(unittest.TestCase):
    def test_decimal_is_string_only_and_exact(self):
        read = read_field({"x": "3.10"}, "x", "decimal")
        self.assertTrue(read.known)
        self.assertEqual(read.value, Decimal("3.10"))
        self.assertEqual(read.value, Decimal("3.1"))  # numeric equality
        self.assertNotEqual(read.value, Decimal("3.11"))

    def test_json_number_is_type_mismatch_not_coercion(self):
        read = read_field({"x": 3.0}, "x", "decimal")
        self.assertFalse(read.known)
        self.assertEqual(read.unknown_reason, "type_mismatch")

    def test_missing_null_and_type_reasons_are_distinct(self):
        self.assertEqual(read_field({}, "x", "string").unknown_reason,
                         "missing_field")
        self.assertEqual(read_field({"x": None}, "x", "string").unknown_reason,
                         "null_value")
        self.assertEqual(read_field({"x": 5}, "x", "string").unknown_reason,
                         "type_mismatch")

    def test_invalid_decimal_and_date_strings(self):
        for bad in ("1e5", "1,000", " 3", "三", "", "3."):
            self.assertIsNone(parse_decimal_text(bad), bad)
        self.assertFalse(read_field({"x": "2026-02-30"}, "x", "date").known)
        self.assertFalse(read_field({"x": "2026/02/01"}, "x", "date").known)

    def test_boolean_is_not_truthiness(self):
        self.assertEqual(read_field({"x": "true"}, "x", "boolean").unknown_reason,
                         "type_mismatch")
        self.assertEqual(read_field({"x": 1}, "x", "boolean").unknown_reason,
                         "type_mismatch")


# ---------------------------------------------------------------------------
# Three-valued logic and counts
# ---------------------------------------------------------------------------
GOOD_SUBJECT = {
    "entity_type": "university",
    "registered_years": "4",
    "legal_registered_on": "2022-05-01",
    "has_integrity_issue": False,
}


def member(degree="doctorate", years="4", role="co_investigator"):
    return {"role": role, "degree_level": degree, "years_experience": years}


def application(items, complete=True, subject=None, app_id="T-1"):
    return validate_application({
        "application_id": app_id,
        "subject": dict(subject or GOOD_SUBJECT),
        "items": items,
        "complete": complete,
    }, 1)


class TruthTableTests(SampleCase):
    def evaluate(self, record):
        clause = load_clause(self.workdir / "pipeline.sqlite3",
                             self.clause_path)
        return Evaluator(clause).evaluate(record)

    def root_branch(self, result, target):
        tree = explain_result(result)["tree"]
        for child in tree["children"]:
            if child.get("target") == target:
                return child["resolved"]["tree"]
        raise AssertionError(f"{target} not under root: "
                             f"{[c.get('target') for c in tree['children']]}")

    def test_complete_empty_team_is_false_not_unknown(self):
        result = self.evaluate(application([]))
        self.assertEqual(result["verdict"], "false")
        branch = self.root_branch(result, "COUNT_DOCTORS")
        self.assertEqual(branch["possible_interval"]["notation"], "[0, 0]")
        self.assertEqual(branch["result"], "false")

    def test_incomplete_but_enough_is_true(self):
        result = self.evaluate(application(
            [member(role="pi", years="5"), member(),
             member(degree="master")], complete=False))
        self.assertEqual(result["verdict"], "true")
        branch = self.root_branch(result, "COUNT_DOCTORS")
        self.assertEqual(branch["possible_interval"]["notation"], "[2, ∞)")
        self.assertEqual(branch["member_counts"],
                         {"true": 2, "false": 1, "unknown": 0})

    def test_unknown_member_on_threshold_is_unknown(self):
        result = self.evaluate(application(
            [member(role="pi", years="5"), member(degree="master"),
             member(years=None)]))
        self.assertEqual(result["verdict"], "unknown")
        branch = self.root_branch(result, "COUNT_DOCTORS")
        self.assertEqual(branch["possible_interval"]["notation"], "[1, 2]")
        self.assertEqual(branch["result"], "unknown")
        # All branches are still described (no short-circuit).
        self.assertEqual([m["result"] for m in branch["members"]],
                         ["true", "false", "unknown"])

    def test_two_people_each_half_never_satisfy_one_member(self):
        result = self.evaluate(application([
            member(degree="doctorate", years="1"),
            member(degree="master", years="8"),
        ]))
        self.assertEqual(result["verdict"], "false")
        branch = self.root_branch(result, "COUNT_DOCTORS")
        self.assertEqual(branch["member_counts"]["true"], 0)
        self.assertEqual(branch["possible_interval"]["notation"], "[0, 0]")
        # the PI condition is also false: no member has role=pi
        self.assertEqual(self.root_branch(result, "PI_CONDITION")["result"],
                         "false")

    def test_explicit_counterexample_beats_unknown_siblings(self):
        subject = dict(GOOD_SUBJECT, has_integrity_issue=True)
        del subject["legal_registered_on"]
        result = self.evaluate(application(
            [member(role="pi", years="5"), member()], subject=subject))
        self.assertEqual(result["verdict"], "false")
        self.assertEqual(result["false_conditions"][1]["condition_id"],
                         "INTEGRITY_OK")
        unknowns = {u["condition_id"] for u in result["unknown_values"]}
        self.assertIn("LEGAL_DATE", unknowns)

    def test_decimal_boundaries_are_inclusive(self):
        subject = dict(GOOD_SUBJECT, registered_years="3.0")
        result = self.evaluate(application(
            [member(role="pi", years="5.00"), member(years="3.0")],
            subject=subject))
        self.assertEqual(result["verdict"], "true")

    def test_wrong_typed_decimal_becomes_unknown(self):
        subject = dict(GOOD_SUBJECT, registered_years=3)
        result = self.evaluate(application(
            [member(role="pi", years="5"), member()], subject=subject))
        self.assertEqual(result["verdict"], "unknown")
        unknown = [u for u in result["unknown_values"]
                   if u["field"] == "registered_years"]
        self.assertEqual(unknown[0]["reason"], "type_mismatch")
        self.assertEqual(unknown[0]["json_pointer"], "/subject/registered_years")

    def test_enumeration_false_is_explicit(self):
        subject = dict(GOOD_SUBJECT, entity_type="individual")
        result = self.evaluate(application(
            [member(role="pi"), member()], subject=subject))
        self.assertEqual(result["verdict"], "false")
        self.assertIn("ENTITY_KIND",
                      [f["condition_id"] for f in result["false_conditions"]])

    def test_any_and_not_truth_rules(self):
        # empty all is true, empty any is false, not preserves unknown
        from archive_pipeline.eligengine import _rule_result
        self.assertEqual(_rule_result([], "all")[0], "true")
        self.assertEqual(_rule_result([], "any")[0], "false")
        self.assertEqual(_rule_result(["true", "unknown", "false"], "all")[0],
                         "false")
        self.assertEqual(_rule_result(["true", "unknown"], "all")[0], "unknown")
        self.assertEqual(_rule_result(["false", "unknown"], "any")[0], "unknown")
        self.assertEqual(_rule_result(["false", "false"], "any")[0], "false")

    def test_count_zero_always_satisfied(self):
        # A clause-local check: build an in-memory clause variant with k=0.
        payload = json.loads(json.dumps(self.clause_payload))
        for condition in payload["conditions"]:
            if condition["id"] == "COUNT_DOCTORS":
                condition["expr"]["count"]["at_least"] = 0
        path = _write(self.workdir / "clause-k0.json", payload)
        clause = load_clause(self.workdir / "pipeline.sqlite3", path)
        result = Evaluator(clause).evaluate(
            application([member(role="pi", years="5")]))
        self.assertEqual(result["verdict"], "true")
        branch = self.root_branch(result, "COUNT_DOCTORS")
        self.assertEqual(branch["result"], "true")
        self.assertIn("0", branch["reason"])

    def test_every_condition_has_a_verified_citation(self):
        result = self.evaluate(application([member(role="pi"), member()]))
        citation_ids = [c["condition_id"] for c in result["citations"]]
        self.assertEqual(citation_ids,
                         [c["id"] for c in self.clause_payload["conditions"]])
        for citation in result["citations"]:
            self.assertEqual(citation["verified"], "body[start:end] == quote")
            self.assertGreaterEqual(citation["end"], citation["start"])


# ---------------------------------------------------------------------------
# Clause authoring / binding rejections
# ---------------------------------------------------------------------------
class ClauseRejectionTests(SampleCase):
    def reject(self, payload, name="bad") -> ClauseError:
        path = _write(self.workdir / f"{name}.json", payload)
        with self.assertRaises(ClauseError) as ctx:
            load_clause(self.workdir / "pipeline.sqlite3", path)
        return ctx.exception

    def codes(self, error: ClauseError) -> set[str]:
        return {issue["code"] for issue in error.issues}

    def test_shifted_span_is_rejected(self):
        payload = json.loads(json.dumps(self.clause_payload))
        team = next(c for c in payload["conditions"]
                    if c["id"] == "COUNT_DOCTORS")
        team["citation"]["start"] += 1
        team["citation"]["end"] += 1
        error = self.reject(payload, "shifted")
        self.assertIn("citation_mismatch", self.codes(error))
        self.assertIn("[153,184)", error.pretty())

    def test_citation_from_other_variant_is_rejected(self):
        public = version_for(self.built, "E-9001", "E-9001-public.html")
        payload = json.loads(json.dumps(self.clause_payload))
        payload["archive_id"] = public["archive_id"]
        payload["version_no"] = 1
        error = self.reject(payload, "public")
        self.assertIn("citation_in_other_variant", self.codes(error))
        self.assertIn(public["archive_id"], error.pretty())

    def test_missing_version_is_rejected(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["version_no"] = 2
        error = self.reject(payload, "v2")
        # v2 exists but the v1 team sentence does not: either way, rejected.
        self.assertTrue({"citation_not_found", "citation_mismatch"}
                        & self.codes(error))

    def test_unknown_archive_id_is_rejected(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["archive_id"] = "arc_does_not_exist"
        error = self.reject(payload, "missing-arc")
        self.assertIn("unknown_version", self.codes(error))

    def test_spelling_error_is_not_downgraded(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][2]["expr"] = {
            "field": "registered_years", "operator": "ge", "value": "3"}
        error = self.reject(payload, "spelling")
        self.assertIn("missing_key", self.codes(error))
        self.assertIn("conditions[2].expr", error.pretty())

    def test_undeclared_field_is_rejected(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][2]["expr"] = {
            "field": "registered_year", "op": "ge", "value": "3"}
        self.assertIn("unknown_field", self.codes(self.reject(payload, "field")))

    def test_constant_type_errors(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][2]["expr"]["value"] = 3
        self.assertIn("constant_type",
                      self.codes(self.reject(payload, "const-num")))
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][5]["expr"]["value"] = "false"
        self.assertIn("constant_type",
                      self.codes(self.reject(payload, "const-bool")))

    def test_unordered_comparison_rejected(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][1]["expr"] = {
            "field": "entity_type", "op": "gt", "value": "a"}
        self.assertIn("invalid_comparison",
                      self.codes(self.reject(payload, "gt-str")))

    def test_cross_scope_reference_is_rejected(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][2]["expr"] = {"ref": "PI_DEGREE"}
        error = self.reject(payload, "cross-scope")
        self.assertIn("scope_mismatch", self.codes(error))
        self.assertIn("conditions[2].expr", error.pretty())

    def test_undefined_reference(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][2]["expr"] = {"ref": "NOPE"}
        self.assertIn("undefined_reference",
                      self.codes(self.reject(payload, "undefined")))

    def test_reference_cycle_reported_at_use_site(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][2]["expr"] = {"ref": "COUNT_DOCTORS"}
        payload["conditions"][7]["expr"] = {"ref": "ENTITY_YEARS"}
        error = self.reject(payload, "cycle")
        self.assertIn("reference_cycle", self.codes(error))
        message = error.pretty()
        self.assertIn("ENTITY_YEARS", message)
        self.assertIn("COUNT_DOCTORS", message)

    def test_count_scope_and_threshold_validation(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][4]["expr"] = {
            "count": {"predicate": {"field": "degree_level", "op": "eq",
                                    "value": "doctorate"}, "at_least": 1}}
        self.assertIn("count_scope",
                      self.codes(self.reject(payload, "count-scope")))
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][7]["expr"]["count"]["at_least"] = True
        self.assertIn("invalid_count",
                      self.codes(self.reject(payload, "count-bool")))
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"][7]["expr"]["count"]["at_least"] = -1
        self.assertIn("invalid_count",
                      self.codes(self.reject(payload, "count-neg")))

    def test_root_must_be_subject_scoped_and_defined(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["root"] = "PI_DEGREE"
        self.assertIn("root_scope",
                      self.codes(self.reject(payload, "root-member")))
        payload = json.loads(json.dumps(self.clause_payload))
        payload["root"] = "NOPE"
        self.assertIn("undefined_reference",
                      self.codes(self.reject(payload, "root-undef")))

    def test_duplicate_condition_ids(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["conditions"].append(dict(payload["conditions"][1]))
        self.assertIn("duplicate_condition",
                      self.codes(self.reject(payload, "dup")))

    def test_clause_digest_changes_with_edition(self):
        payload = json.loads(json.dumps(self.clause_payload))
        payload["description"] = "second edition"
        first = load_clause(self.workdir / "pipeline.sqlite3", self.clause_path)
        edited = _write(self.workdir / "clause-v1b.json", payload)
        second = load_clause(self.workdir / "pipeline.sqlite3", edited)
        self.assertNotEqual(first.digest, second.digest)


# ---------------------------------------------------------------------------
# Batch safety and durability
# ---------------------------------------------------------------------------
class BatchTests(SampleCase):
    def write_lines(self, name: str, lines: list[str]) -> Path:
        path = self.workdir / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def run_job(self, lines, name="input.jsonl"):
        job = self.new_job()
        outcome = job.run(self.write_lines(name, lines))
        return job, outcome

    def test_input_order_and_summary_are_preserved(self):
        job, outcome = self.run_job(BASE_INPUT.read_text("utf-8").splitlines())
        self.assertEqual(outcome.status, "completed")
        summary = outcome.summary
        self.assertEqual(summary["applications_total"], 12)
        self.assertEqual(summary["counts"],
                         {"satisfied": 4, "not_satisfied": 5,
                          "needs_evidence": 3})
        self.assertEqual(summary["ordered_application_ids"],
                         [f"APP-{n}" for n in range(1001, 1013)])
        sequencing = [(row["seq"], row["line_no"])
                      for row in job.store.applications(outcome.run_id)]
        self.assertEqual(sequencing, [(i, i + 1) for i in range(12)])
        self.assertIsNotNone(summary["input"]["sha256"])
        self.assertIsNotNone(summary["clause_digest"])
        job.close()

    def test_bad_json_in_second_half_is_incomplete(self):
        lines = BASE_INPUT.read_text("utf-8").splitlines()[:6]
        lines += ['{"application_id": "APP-9001", "subject": ',
                  BASE_INPUT.read_text("utf-8").splitlines()[7]]
        job, outcome = self.run_job(lines, "bad-json.jsonl")
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(outcome.error["code"], "bad_json")
        self.assertEqual(outcome.error["line_no"], 7)
        self.assertEqual(outcome.applications_written, 6)
        run = job.store.run(outcome.run_id)
        self.assertEqual(run["status"], "incomplete")
        self.assertIsNone(run["summary_json"])
        self.assertEqual([row["application_id"]
                          for row in job.store.applications(outcome.run_id)],
                         [f"APP-{n}" for n in range(1001, 1007)])
        self.assertEqual(len(job.store.input_errors(outcome.run_id)), 1)
        job.close()

    def test_duplicate_application_id_in_second_half(self):
        records = [json.loads(line) for line in
                   BASE_INPUT.read_text("utf-8").splitlines()[:5]]
        records.append(records[1])
        lines = [json.dumps(record, ensure_ascii=False) for record in records]
        job, outcome = self.run_job(lines, "dup.jsonl")
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(outcome.error["code"], "duplicate_application_id")
        self.assertEqual(outcome.error["line_no"], 6)
        self.assertEqual(outcome.applications_written, 5)
        job.close()

    def test_broken_member_structure_in_second_half(self):
        lines = BASE_INPUT.read_text("utf-8").splitlines()[:4]
        bad = json.loads(lines[0])
        bad["application_id"] = "APP-8001"
        bad["items"] = ["not-an-object"]
        lines.append(json.dumps(bad, ensure_ascii=False))
        lines.append(BASE_INPUT.read_text("utf-8").splitlines()[5])
        job, outcome = self.run_job(lines, "broken-item.jsonl")
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(outcome.error["code"], "invalid_item")
        self.assertEqual(outcome.error["line_no"], 5)
        self.assertEqual(outcome.applications_written, 4)
        self.assertIsNone(job.store.run(outcome.run_id)["summary_json"])
        job.close()

    def test_invalid_clause_produces_no_judgment(self):
        payload = json.loads(json.dumps(self.clause_payload))
        team = next(c for c in payload["conditions"]
                    if c["id"] == "COUNT_DOCTORS")
        team["citation"]["start"] += 1
        team["citation"]["end"] += 1
        bad_clause = _write(self.workdir / "bad-clause.json", payload)
        # Clause validation happens in the constructor, before any
        # application or result database is opened; the constructor raises.
        isolated = self.workdir / "invalid-clause-workdir"
        isolated.mkdir(exist_ok=True)
        shutil.copy2(self.workdir / "pipeline.sqlite3",
                     isolated / "pipeline.sqlite3")
        with self.assertRaises(ClauseError) as ctx:
            EligibilityJob(isolated, bad_clause)
        self.assertIn("citation_mismatch",
                      {issue["code"] for issue in ctx.exception.issues})
        self.assertFalse((isolated / "eligibility.sqlite3").exists())

    def test_application_unknown_is_not_a_file_error(self):
        record = {
            "application_id": "APP-7001",
            "subject": {"entity_type": None, "registered_years": 3,
                        "legal_registered_on": "not-a-date"},
            "items": [{"role": "pi", "degree_level": "doctorate",
                       "years_experience": "5"}],
            "complete": True,
        }
        job, outcome = self.run_job(
            [json.dumps(record, ensure_ascii=False)], "one.jsonl")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.applications_written, 1)
        explanation = explain_application(self.workdir, "APP-7001")
        reasons = {item["reason"]
                   for item in explanation["explanation"]["unknown_values"]}
        self.assertTrue({"null_value", "type_mismatch"} <= reasons,
                        reasons)
        job.close()

    def test_stale_running_run_is_marked_incomplete(self):
        store = self.store()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO runs(started_at, status, clause_path, "
                "clause_file_sha256, clause_digest, clause_summary_json, "
                "input_path) VALUES ('2026-01-01T00:00:00+00:00','running',"
                "'x','y','z','{}','in')")
        store.close()
        job, outcome = self.run_job(
            BASE_INPUT.read_text("utf-8").splitlines()[:2], "small.jsonl")
        stale_runs = [run for run in job.store.runs()
                      if run["status"] == "incomplete"
                      and run["clause_path"] == "x"]
        self.assertEqual(len(stale_runs), 1)
        self.assertIn("process_interrupted", stale_runs[0]["error_json"])
        self.assertEqual(outcome.status, "completed")
        job.close()


# ---------------------------------------------------------------------------
# Read-only pipeline and reopen traceability
# ---------------------------------------------------------------------------
class DurabilityTests(SampleCase):
    def test_reopen_keeps_full_explanation_and_citations(self):
        job = self.new_job()
        outcome = job.run(BASE_INPUT)
        job.close()
        reopened = explain_application(self.workdir, "APP-1010")
        self.assertIsNotNone(reopened)
        explanation = reopened["explanation"]
        self.assertEqual(explanation["clause_digest"],
                         outcome.summary["clause_digest"])
        self.assertTrue(explanation["citations"])
        for citation in explanation["citations"]:
            self.assertEqual(citation["verified"],
                             "body[start:end] == quote")
        tree = explain_result(explanation)["tree"]
        self.assertEqual(tree["node"], "all")
        # The full unknown set survives the round trip.
        self.assertGreaterEqual(len(explanation["unknown_values"]), 8)
        self.assertEqual(reopened["run_status"], "completed")

    def test_readback_for_older_run(self):
        job = self.new_job()
        first = job.run(BASE_INPUT)
        second = job.run(BASE_INPUT)
        job.close()
        older = explain_application(self.workdir, "APP-1004",
                                    run_id=first.run_id)
        self.assertEqual(older["run_id"], first.run_id)
        self.assertNotEqual(first.run_id, second.run_id)

    def test_pipeline_database_is_not_modified(self):
        before = (self.workdir / "pipeline.sqlite3").read_bytes()
        before_stat = os.stat(self.workdir / "pipeline.sqlite3")
        job = self.new_job()
        job.run(BASE_INPUT)
        job.close()
        after = (self.workdir / "pipeline.sqlite3").read_bytes()
        after_stat = os.stat(self.workdir / "pipeline.sqlite3")
        self.assertEqual(before, after)
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

    def test_read_only_open_would_refuse_writes(self):
        # The reader uses mode=ro; a write attempt must fail even if someone
        # pointed it at a writable file.
        db = self.workdir / "pipeline.sqlite3"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO scans(source_id, started_at) "
                             "VALUES ('x','y')")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

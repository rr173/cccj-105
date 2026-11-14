"""Offline relation rules: validation, semi-naive recursion, stratification.

Expected tuples below are written by hand from the fixture inputs (and from
the archived sample bodies read back independently); they are never produced
by the production evaluator.  The brute-force oracle in
``archive_pipeline/reloracle.py`` is a second, independent evaluator used for
finite random inputs.

Run: ``python -m unittest discover -s tests -v`` from the project root.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from archive_pipeline.relcli import main as cli_main
from archive_pipeline.reldemo import (
    EXPECTED_ANCESTOR,
    EXPECTED_ORG_SELF,
    EXPECTED_ANCESTOR_EXCLUDED,
    EXPECTED_COOP_REACH,
    EXPECTED_NOTICE_ALLOWED,
    EXPECTED_NOTICE_ORG,
    EXPECTED_NOTICE_VISIBLE,
    EXPECTED_REACH_EVEN,
    EXPECTED_REACH_ODD,
)
from archive_pipeline.relationjob import RelationJob, check_rules, read_relation
from archive_pipeline.relfacts import FactError
from archive_pipeline.reloracle import Oracle
from archive_pipeline.relrule import RuleError, load_rules
from archive_pipeline.relsample import build_sample_workdir
from archive_pipeline.store import Store

FACTS = ROOT / "scenario" / "relations" / "facts"
RULES = ROOT / "scenario" / "relations" / "rules" / "org.json"
SOURCE = "relsim"
FULL_AID = "arc_25a6d2f01627d245df9ba57bf2e5831e"
PUBLIC_AID = "arc_6af784ed31085359fffe393e61bd4579"
EVAL_AID = "arc_ed728d5bb344f8aa476bef08729add98"
MAINT_AID = "arc_309df132b62995e8a86c08f47f2c3228"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_rule_document(path: Path, relations: dict, rules: list,
                        budget: int = 5000) -> Path:
    path.write_text(json.dumps({"format": "relation-rules/1",
                                "budget": budget, "relations": relations,
                                "rules": rules}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def relationship(columns: list[tuple[str, str]], path: str | None = None) -> dict:
    spec: dict = {"columns": [[name, kind] for name, kind in columns]}
    if path is not None:
        spec["source"] = {"kind": "jsonl", "path": path}
    return spec


def tuples_of(store, run_id: int, relation: str,
              columns: list[str]) -> set[tuple]:
    rows = store.relation_rows(run_id, relation, limit=10000)
    return {tuple(row[column] for column in columns) for row in rows}


def write_lines(path: Path, records: list[dict]) -> None:
    """An explicitly empty relation is an empty file (no blank line)."""
    if not records:
        path.write_text("", encoding="utf-8")
        return
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False)
                              for record in records) + "\n", encoding="utf-8")


class RelationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rel-test-"))
        cls.built = build_sample_workdir(cls.tmp)
        cls.archive_ids = {v["file"]: v["archive_id"]
                           for v in cls.built["versions"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="rel-case-", dir=self.tmp))
        shutil.copytree(FACTS, self.work / "facts")
        shutil.copytree(RULES.parent, self.work / "rules")
        shutil.copy(self.tmp / "pipeline.sqlite3",
                    self.work / "pipeline.sqlite3")

    def run_job(self, rules: Path | None = None, **kwargs):
        with RelationJob(self.work) as job:
            outcome = job.run(rules or (self.work / "rules" / "org.json"),
                              **kwargs)
            self.assertIsNotNone(job.store)
            return outcome

    def store_after(self, rules: Path | None = None, **kwargs):
        job = RelationJob(self.work)
        outcome = job.run(rules or (self.work / "rules" / "org.json"), **kwargs)
        return job, outcome


# ---------------------------------------------------------------------------
# Rule validation before any fact is read
# ---------------------------------------------------------------------------


class ValidationTests(RelationCase):
    def check(self, relations, rules, code, location, **kwargs):
        path = write_rule_document(self.work / "rules" / "case.json",
                                   relations, rules, **kwargs)
        with self.assertRaises(RuleError) as ctx:
            load_rules(path)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.location, location)

    def test_unknown_relation(self):
        self.check(
            {"d": relationship([("x", "string")])},
            [{"head": {"relation": "d", "args": ["?x"]},
              "body": [{"relation": "ghost", "args": ["?x"]}]}],
            "unknown_relation", "rules[0].body[0]")

    def test_arity_mismatch(self):
        self.check(
            {"p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl"),
             "d": relationship([("x", "string")])},
            [{"head": {"relation": "d", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x"]}]}],
            "arity_mismatch", "rules[0].body[0]")

    def test_base_relation_cannot_be_rule_head(self):
        self.check(
            {"p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "p", "args": ["?u", "?v"]},
              "body": [{"relation": "p", "args": ["?u", "?v"]}]}],
            "base_relation_as_head", "rules[0]")

    def test_undefined_derived_relation(self):
        self.check(
            {"a": relationship([("x", "string")]),
             "b": relationship([("x", "string")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "?x"]}]}],
            "undefined_derived_relation", "$.relations['b']")

    def test_unsafe_variable_in_negation(self):
        self.check(
            {"a": relationship([("x", "string")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "?y"]},
                       {"not": {"relation": "p", "args": ["?z", "?y"]}}]}],
            "unsafe_variable", "rules[0].body[1].not")

    def test_unsafe_variable_in_comparison(self):
        self.check(
            {"a": relationship([("x", "string")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "?y"]},
                       {"eq": ["?x", "?z"]}]}],
            "unsafe_variable", "rules[0].body[1]")

    def test_unsafe_head_variable(self):
        self.check(
            {"a": relationship([("x", "string")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "a", "args": ["?missing"]},
              "body": [{"relation": "p", "args": ["?x", "?y"]}]}],
            "unsafe_head_variable", "rules[0].head")

    def test_comparison_across_types(self):
        self.check(
            {"a": relationship([("x", "string")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "?y"]},
                       {"eq": ["?x", {"const": 3}]}]}],
            "comparison_type_mismatch", "rules[0].body[1]")

    def test_constant_type_must_match_column(self):
        self.check(
            {"a": relationship([("x", "string")]),
             "p": relationship([("u", "string"), ("v", "int")], "../facts/org.jsonl")},
            [{"head": {"relation": "a", "args": ["?u"]},
              "body": [{"relation": "p", "args": ["?u", {"const": "not-an-int"}]}]}],
            "constant_type_mismatch", "rules[0].body[0].args[1]")

    def test_head_constant_type_must_match_column(self):
        self.check(
            {"a": relationship([("n", "int")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "a", "args": [{"const": "x"}]},
              "body": [{"relation": "p", "args": ["?u", "?v"]}]}],
            "constant_type_mismatch", "rules[0].head.args[0]")

    def test_bare_string_is_not_guessed_as_constant(self):
        self.check(
            {"a": relationship([("x", "string")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "literal"]}]}],
            "bare_string_term", "rules[0].body[0].args[1]")

    def test_boolean_constant_rejected(self):
        self.check(
            {"d": relationship([("n", "int")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "d", "args": [{"const": True}]},
              "body": [{"relation": "p", "args": ["?u", "?v"]}]}],
            "bool_not_int", "rules[0].head.args[0]")

    def test_int_constant_out_of_range(self):
        self.check(
            {"d": relationship([("n", "int")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/org.jsonl")},
            [{"head": {"relation": "d", "args": [{"const": 2 ** 63}]},
              "body": [{"relation": "p", "args": ["?u", "?v"]}]}],
            "int_out_of_range", "rules[0].head.args[0]")

    def test_negative_cycle_rejected_with_empty_input(self):
        # relations are declared, but the fact files are empty/nonexistent:
        # the rejection must come from the rule text alone.
        self.check(
            {"a": relationship([("x", "string")]),
             "b": relationship([("x", "string")]),
             "p": relationship([("u", "string"), ("v", "string")], "../facts/missing.jsonl")},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "?x"]}]},
             {"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "b", "args": ["?x"]},
                       {"not": {"relation": "a", "args": ["?x"]}}]},
             {"head": {"relation": "b", "args": ["?x"]},
              "body": [{"relation": "a", "args": ["?x"]}]}],
            "negative_cycle", "rules[1]")

    def test_negation_of_a_higher_stratum_is_a_cycle(self):
        # a -> not b, b -> a : a negative edge inside the SCC.
        self.check(
            {"a": relationship([("x", "string")]),
             "b": relationship([("x", "string")])},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "b", "args": ["?x"]},
                       {"not": {"relation": "b", "args": ["?x"]}}]},
             {"head": {"relation": "b", "args": ["?x"]},
              "body": [{"relation": "a", "args": ["?x"]}]}],
            "negative_cycle", "rules[0]")

    def test_occurrences_of_two_recursive_relations_are_both_edges(self):
        path = write_rule_document(self.work / "rules" / "two.json", {
            "p": relationship([("u", "string"), ("v", "string")],
                              "../facts/org.jsonl"),
            "a": relationship([("x", "string")]),
            "b": relationship([("x", "string")])},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "?x"]}]},
             {"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "a", "args": ["?x"]},
                       {"relation": "b", "args": ["?x"]}]},
             {"head": {"relation": "b", "args": ["?x"]},
              "body": [{"relation": "a", "args": ["?x"]}]}])
        ruleset = load_rules(path)
        edges = [(e.head, e.target, e.negative) for e in ruleset.plan.edges]
        self.assertIn(("a", "a", False), edges)
        self.assertIn(("a", "b", False), edges)
        self.assertIn(("b", "a", False), edges)

    def test_header_requires_simple_structure(self):
        for bad, code in [
            ({"format": "relation-rules/2", "relations": {}, "rules": []},
             "bad_format"),
            ({"format": "relation-rules/1", "relations": {}, "rules": []},
             "bad_relations"),
        ]:
            path = self.work / "rules" / "shape.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(RuleError) as ctx:
                load_rules(path)
            self.assertEqual(ctx.exception.code, code)

    def test_check_reads_no_facts(self):
        # The fact file path does not exist; check still succeeds.
        path = write_rule_document(self.work / "rules" / "nofacts.json", {
            "a": relationship([("x", "string")]),
            "p": relationship([("u", "string"), ("v", "string")],
                              "../facts/does-not-exist.jsonl")},
            [{"head": {"relation": "a", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "?x"]}]}])
        result = check_rules(path)
        self.assertEqual(result["status"], "accepted")
        self.assertFalse((self.work / "facts" / "does-not-exist.jsonl").exists())


# ---------------------------------------------------------------------------
# The full organization exercise
# ---------------------------------------------------------------------------


class OrganizationExerciseTests(RelationCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.work = Path(tempfile.mkdtemp(prefix="rel-exercise-", dir=cls.tmp))
        shutil.copytree(FACTS, cls.work / "facts")
        shutil.copytree(RULES.parent, cls.work / "rules")
        shutil.copy(cls.tmp / "pipeline.sqlite3", cls.work / "pipeline.sqlite3")
        cls.job = RelationJob(cls.work)
        cls.outcome = cls.job.run(RULES)

    @classmethod
    def tearDownClass(cls):
        cls.job.close()
        super().tearDownClass()

    def test_run_completed(self):
        self.assertEqual(self.outcome.status, "completed")
        self.assertEqual(self.outcome.exit_code, 0)
        self.assertIsNotNone(self.outcome.summary)

    def test_ancestor_recursion_matches_hand_written_set(self):
        actual = tuples_of(self.job.store, self.outcome.run_id, "ancestor",
                           ["org_id", "ancestor"])
        self.assertEqual(actual, EXPECTED_ANCESTOR)

    def test_positive_cycle_terminates_with_a_cycle_in_the_input(self):
        # P<->Q in parent_org: ancestor contains P->P and Q->Q exactly once.
        actual = tuples_of(self.job.store, self.outcome.run_id, "ancestor",
                           ["org_id", "ancestor"])
        self.assertEqual(1, sum(1 for pair in actual if pair == ("P", "P")))
        self.assertEqual(1, sum(1 for pair in actual if pair == ("Q", "Q")))
        self.assertIn(("P", "Q"), actual)
        self.assertIn(("Q", "P"), actual)

    def test_mutual_recursion(self):
        even = tuples_of(self.job.store, self.outcome.run_id, "reach_even",
                         ["org_id"])
        odd = tuples_of(self.job.store, self.outcome.run_id, "reach_odd",
                        ["org_id"])
        self.assertEqual(even, EXPECTED_REACH_EVEN)
        self.assertEqual(odd, EXPECTED_REACH_ODD)

    def test_cooperation_reachability(self):
        actual = tuples_of(self.job.store, self.outcome.run_id, "coop_reach",
                           ["org_a", "org_b"])
        self.assertEqual(actual, EXPECTED_COOP_REACH)

    def test_repeated_variable_self_join(self):
        self.assertEqual(
            tuples_of(self.job.store, self.outcome.run_id, "org_self",
                      ["org_id"]), EXPECTED_ORG_SELF)
        parent_count = self.job.store.relation_count(self.outcome.run_id,
                                                     "parent_org")
        self.assertEqual(parent_count, 11)

    def test_diamond_paths_are_deduplicated(self):
        # A1 reaches C via A1-B1-C and via B1-only; one tuple only.
        rows = self.job.store.relation_rows(self.outcome.run_id, "coop_reach",
                                            limit=1000)
        self.assertEqual(1, sum(1 for row in rows
                                if (row["org_a"], row["org_b"]) == ("A1", "C")))
        # A1 reaches A1 via several cycles; still one tuple.
        self.assertEqual(1, sum(1 for row in rows
                                if (row["org_a"], row["org_b"]) == ("A1", "A1")))

    def test_notice_versions_are_not_cross_used(self):
        actual = tuples_of(self.job.store, self.outcome.run_id, "notice_org",
                           ["source_id", "archive_id", "version_no", "org_id"])
        self.assertEqual(actual, EXPECTED_NOTICE_ORG)
        # mention for FULL v1 never produces a v2 row and vice versa
        self.assertIn((SOURCE, FULL_AID, 1, "A1"), actual)
        self.assertNotIn((SOURCE, FULL_AID, 2, "A1"), actual)
        self.assertIn((SOURCE, FULL_AID, 2, "B1"), actual)
        # public edition is a different archive identity despite same number
        self.assertIn((SOURCE, PUBLIC_AID, 1, "A2"), actual)
        self.assertNotIn((SOURCE, PUBLIC_AID, 1, "C"), actual)

    def test_exclusion_and_lower_stratum_negation(self):
        allowed = tuples_of(self.job.store, self.outcome.run_id,
                            "notice_allowed",
                            ["source_id", "archive_id", "version_no", "org_id"])
        visible = tuples_of(self.job.store, self.outcome.run_id,
                            "notice_visible",
                            ["source_id", "archive_id", "version_no", "org_id"])
        self.assertEqual(allowed, EXPECTED_NOTICE_ALLOWED)
        self.assertEqual(visible, EXPECTED_NOTICE_VISIBLE)
        # X itself is excluded; D is dropped only by the descendant rule.
        self.assertNotIn((SOURCE, EVAL_AID, 1, "X"), allowed)
        self.assertIn((SOURCE, EVAL_AID, 1, "D"), allowed)
        self.assertNotIn((SOURCE, EVAL_AID, 1, "D"), visible)
        self.assertEqual(
            tuples_of(self.job.store, self.outcome.run_id,
                      "ancestor_excluded", ["org_id"]),
            EXPECTED_ANCESTOR_EXCLUDED)

    def test_strata_order_puts_recursion_before_negation(self):
        plan = check_rules(RULES)["rules"]["plan"]
        strata = {tuple(s["relations"]): s for s in plan["strata"]}
        ancestor = next(s for name, s in strata.items() if "ancestor" in name)
        allowed = next(s for name, s in strata.items()
                       if "notice_allowed" in name)
        visible = next(s for name, s in strata.items()
                       if "notice_visible" in name)
        ancestor_excluded = next(s for name, s in strata.items()
                                 if "ancestor_excluded" in name)
        self.assertTrue(ancestor["recursive"])
        self.assertLess(ancestor["stratum"], ancestor_excluded["stratum"])
        self.assertLess(ancestor_excluded["stratum"], visible["stratum"])
        self.assertLess(allowed["stratum"], visible["stratum"])
        # every negative dependency points at a strictly lower stratum
        for edge in plan["dependencies"]:
            if edge["negative"]:
                self.assertLess(plan["edge_strata"][edge["depends_on"]],
                                plan["edge_strata"][edge["head"]]) \
                    if "edge_strata" in plan else None

    def test_round_statistics_show_delta_usage(self):
        rounds = self.job.store.rounds(self.outcome.run_id, "ancestor")
        self.assertGreaterEqual(len(rounds), 2)
        self.assertGreater(rounds[0]["delta_rows"], 0)
        self.assertEqual(rounds[-1]["delta_rows"], 0)
        plans = [json.loads(row["join_plan_json"]) for row in rounds[1:]]
        modes = {item["mode"] for plan in plans for item in plan}
        self.assertIn("delta", modes)
        self.assertIn("full", modes)
        execution = {row["relation"]: row
                     for row in self.job.store.execution(self.outcome.run_id)}
        self.assertGreater(execution["ancestor"]["joins_delta"], 0)
        self.assertGreater(execution["ancestor"]["joins_full"], 0)

    def test_rule_used_twice_in_body_gets_two_delta_variants(self):
        # ancestor's recursive rule has the same relation in two positions; at
        # least one wave round must have executed two variants.
        rounds = self.job.store.rounds(self.outcome.run_id, "ancestor")
        self.assertTrue(any(row["variants"] >= 2 for row in rounds[1:]),
                        [(row["round_no"], row["variants"]) for row in rounds])

    def test_empty_relation_and_constant(self):
        self.assertEqual(self.job.store.relation_count(self.outcome.run_id,
                                                       "blocked_notice"), 0)
        self.assertEqual(
            tuples_of(self.job.store, self.outcome.run_id, "reach_even",
                      ["org_id"]), {("A1",)})
        # constant-only seed did not need a cooperation edge
        self.assertEqual(
            tuples_of(self.job.store, self.outcome.run_id, "reach_odd",
                      ["org_id"]), {("B1",)})

    def test_duplicate_fact_lines_collapse(self):
        # parent_org.jsonl contains {"A1":"A"} twice; 12 lines -> 11 tuples.
        self.assertEqual(self.job.store.relation_count(self.outcome.run_id,
                                                       "parent_org"), 11)

    def test_pagination_is_stable_and_complete(self):
        columns = ["org_id", "ancestor"]
        seen: list[tuple] = []
        offset = 0
        while True:
            page = read_relation(self.job.store, self.outcome.run_id,
                                 "ancestor", offset=offset, limit=5)
            if not page["rows"]:
                break
            seen.extend(tuple(row[c] for c in columns) for row in page["rows"])
            offset += 5
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), EXPECTED_ANCESTOR)

    def test_notice_rows_are_the_archived_versions(self):
        rows = self.job.store.relation_rows(self.outcome.run_id, "notice",
                                            limit=100)
        self.assertEqual(len(rows), 5)
        self.assertEqual({row["archive_id"] for row in rows},
                         {FULL_AID, PUBLIC_AID, EVAL_AID, MAINT_AID})
        full_versions = sorted(row["version_no"] for row in rows
                               if row["archive_id"] == FULL_AID)
        self.assertEqual(full_versions, [1, 2])
        v1 = next(row for row in rows
                  if row["archive_id"] == FULL_AID and row["version_no"] == 1)
        self.assertIn("两个实验方向", v1["body"])


# ---------------------------------------------------------------------------
# Order independence and oracle cross-check
# ---------------------------------------------------------------------------


class IndependenceTests(RelationCase):
    def shuffled_workdir(self) -> Path:
        target = Path(tempfile.mkdtemp(prefix="rel-shuffled-", dir=self.tmp))
        (target / "facts").mkdir()
        for path in FACTS.glob("*.jsonl"):
            lines = [line for line in path.read_text(encoding="utf-8")
                     .splitlines() if line.strip()]
            random.Random("facts+" + path.name).shuffle(lines)
            (target / "facts" / path.name).write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        document = json.loads(RULES.read_text(encoding="utf-8"))
        random.Random("rules").shuffle(document["rules"])
        (target / "rules").mkdir()
        (target / "rules" / "org.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8")
        shutil.copy(self.tmp / "pipeline.sqlite3",
                    target / "pipeline.sqlite3")
        return target

    def test_order_does_not_change_result_sets(self):
        original = self.run_job()
        target = self.shuffled_workdir()
        with RelationJob(target) as job:
            shuffled = job.run(target / "rules" / "org.json")
            self.assertEqual(shuffled.status, "completed")
            for relation, columns in [
                ("ancestor", ["org_id", "ancestor"]),
                ("coop_reach", ["org_a", "org_b"]),
                ("reach_even", ["org_id"]),
                ("reach_odd", ["org_id"]),
                ("notice_org", ["source_id", "archive_id", "version_no", "org_id"]),
                ("notice_allowed", ["source_id", "archive_id", "version_no", "org_id"]),
                ("notice_visible", ["source_id", "archive_id", "version_no", "org_id"]),
            ]:
                left = tuples_of(job.store, shuffled.run_id, relation, columns)
                right_source = RelationJob(self.work)
                right_source.store = None
                with RelationJob(self.work) as base:
                    outcome = base.run(RULES)
                    right = tuples_of(base.store, outcome.run_id, relation,
                                      columns)
                self.assertEqual(left, right, relation)

    def test_oracle_agrees_on_random_finite_inputs(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                work = Path(tempfile.mkdtemp(prefix="rel-oracle-", dir=self.tmp))
                rng = random.Random(seed)
                nodes = [f"n{i}" for i in range(rng.randint(2, 6))]
                edges = sorted({(rng.choice(nodes), rng.choice(nodes))
                                for _ in range(rng.randint(1, 7))})
                excluded = sorted({rng.choice(nodes)
                                   for _ in range(rng.randint(0, 2))})
                (work / "facts").mkdir()
                (work / "rules").mkdir()
                write_lines(work / "facts" / "edge.jsonl",
                            [{"u": u, "v": v} for u, v in edges])
                write_lines(work / "facts" / "excl.jsonl",
                            [{"node": node} for node in excluded])
                document = {
                    "format": "relation-rules/1", "budget": 5000,
                    "relations": {
                        "edge": relationship([("u", "string"), ("v", "string")],
                                             "../facts/edge.jsonl"),
                        "excl": relationship([("node", "string")],
                                             "../facts/excl.jsonl"),
                        "reach": relationship([("u", "string"), ("v", "string")]),
                        "mutual": relationship([("u", "string"), ("v", "string")]),
                        "safe": relationship([("u", "string"), ("v", "string")]),
                    },
                    "rules": [
                        {"head": {"relation": "reach", "args": ["?u", "?v"]},
                         "body": [{"relation": "edge", "args": ["?u", "?v"]}]},
                        {"head": {"relation": "mutual", "args": ["?u", "?v"]},
                         "body": [{"relation": "edge", "args": ["?u", "?v"]}]},
                        {"head": {"relation": "mutual", "args": ["?v", "?u"]},
                         "body": [{"relation": "edge", "args": ["?u", "?v"]}]},
                        {"head": {"relation": "mutual", "args": ["?u", "?v"]},
                         "body": [{"relation": "mutual", "args": ["?u", "?w"]},
                                  {"relation": "mutual", "args": ["?w", "?v"]}]},
                        {"head": {"relation": "safe", "args": ["?u", "?v"]},
                         "body": [{"relation": "reach", "args": ["?u", "?v"]},
                                  {"not": {"relation": "excl",
                                           "args": ["?u"]}}]},
                    ]}
                path = work / "rules" / "random.json"
                path.write_text(json.dumps(document, ensure_ascii=False),
                                encoding="utf-8")
                Store(work / "pipeline.sqlite3").close()
                with RelationJob(work) as job:
                    outcome = job.run(path)
                    self.assertEqual(outcome.status, "completed")
                    reference = Oracle(path, work / "rules",
                                       work / "pipeline.sqlite3").evaluate()
                    for relation in ("reach", "mutual", "safe"):
                        produced = tuples_of(job.store, outcome.run_id,
                                             relation, ["u", "v"])
                        self.assertEqual(produced, set(reference[relation]),
                                         f"seed {seed} {relation}")
                shutil.rmtree(work, ignore_errors=True)

    def test_two_recursive_relations_in_one_body(self):
        work = Path(tempfile.mkdtemp(prefix="rel-two-", dir=self.tmp))
        (work / "facts").mkdir()
        (work / "rules").mkdir()
        write_lines(work / "facts" / "p.jsonl",
                    [{"u": "a", "v": "b"}, {"u": "b", "v": "c"}])
        document = {
            "format": "relation-rules/1", "budget": 500,
            "relations": {
                "p": relationship([("u", "string"), ("v", "string")],
                                  "../facts/p.jsonl"),
                "anc": relationship([("u", "string"), ("v", "string")]),
                "both": relationship([("u", "string"), ("v", "string")]),
            },
            "rules": [
                {"head": {"relation": "anc", "args": ["?u", "?v"]},
                 "body": [{"relation": "p", "args": ["?u", "?v"]}]},
                {"head": {"relation": "anc", "args": ["?u", "?v"]},
                 "body": [{"relation": "anc", "args": ["?u", "?w"]},
                          {"relation": "anc", "args": ["?w", "?v"]}]},
                {"head": {"relation": "both", "args": ["?u", "?v"]},
                 "body": [{"relation": "anc", "args": ["?u", "?w"]},
                          {"relation": "anc", "args": ["?w", "?v"]}]},
            ]}
        path = work / "rules" / "two.json"
        path.write_text(json.dumps(document, ensure_ascii=False),
                        encoding="utf-8")
        Store(work / "pipeline.sqlite3").close()
        with RelationJob(work) as job:
            outcome = job.run(path)
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(
                tuples_of(job.store, outcome.run_id, "anc", ["u", "v"]),
                {("a", "b"), ("b", "c"), ("a", "c")})
            # ``both`` composes two ancestor edges: only a->b->c -> a->c.
            self.assertEqual(
                tuples_of(job.store, outcome.run_id, "both", ["u", "v"]),
                {("a", "c")})
            sums = {row["relation"]: row for row in
                    job.store.execution(outcome.run_id)}
            self.assertGreater(sums["anc"]["joins_delta"], 0)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fact validation
# ---------------------------------------------------------------------------


class FactValidationTests(RelationCase):
    def typed_document(self, fact_lines: list[str]) -> Path:
        (self.work / "facts" / "typed.jsonl").write_text(
            "\n".join(fact_lines) + "\n", encoding="utf-8")
        (self.work / "facts" / "ints.jsonl").write_text(
            '{"k":"a","n":1}\n', encoding="utf-8")
        return write_rule_document(self.work / "rules" / "typed.json", {
            "typed": relationship([("id", "string"), ("num", "int")],
                                  "../facts/typed.jsonl"),
            "ints": relationship([("k", "string"), ("n", "int")],
                                 "../facts/ints.jsonl"),
            "out": relationship([("id", "string"), ("num", "int")])},
            [{"head": {"relation": "out", "args": ["?id", "?num"]},
              "body": [{"relation": "typed", "args": ["?id", "?num"]},
                       {"neq": ["?num", {"const": 0}]}]}])

    def test_bad_fact_after_good_rows_is_located_and_rolled_back(self):
        cases = [
            ('{"id":"ok","num":1}\n{"id":"bad"}', "missing_column", 2),
            ('{"id":"ok","num":1}\n{"id":"bad","num":1,"x":2}',
             "extra_column", 2),
            ('{"id":"ok","num":1}\n{"id":null,"num":2}', "null_value", 2),
            ('{"id":"ok","num":1}\n{"id":2,"num":2}', "type_mismatch", 2),
            ('{"id":"ok","num":1}\n{"id":"bad","num":true}',
             "type_mismatch", 2),
            ('{"id":"ok","num":1}\n{"id":"bad","num":1.5}',
             "type_mismatch", 2),
            ('{"id":"ok","num":1}\nnot json', "bad_json", 2),
        ]
        for text, code, line in cases:
            with self.subTest(code=code, text=text):
                path = self.typed_document(text.splitlines())
                job, outcome = self.store_after(path)
                job.close()
                self.assertEqual(outcome.status, "failed")
                self.assertEqual(outcome.exit_code, 4)
                self.assertEqual(outcome.error["error"]["code"], code)
                self.assertEqual(outcome.error["error"]["line_no"], line)
                self.assertEqual(
                    Path(outcome.error["error"]["path"]).resolve(),
                    (self.work / "facts" / "typed.jsonl").resolve())

    def test_bad_fact_does_not_publish_a_run(self):
        path = self.typed_document(['{"id":"a","num":1}',
                                    '{"id":"b"}'])
        with RelationJob(self.work) as job:
            outcome = job.run(path)
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(job.store.runs(), [])
            self.assertIsNone(job.store.latest_successful_run())

    def test_boolean_is_not_an_integer_fact(self):
        # A bool is not a string either, so the string column check fires too;
        # the point is that no value is silently coerced to 0/1.
        path = self.typed_document(['{"id":true,"num":1}'])
        with RelationJob(self.work) as job:
            outcome = job.run(path)
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.error["error"]["code"], "type_mismatch")

    def test_int_relation_accepts_negative_and_int64_bounds(self):
        path = self.typed_document(
            ['{"id":"a","num":-1}',
             f'{{"id":"b","num":{2 ** 63 - 1}}}',
             f'{{"id":"c","num":{-(2 ** 63)}}}'])
        with RelationJob(self.work) as job:
            outcome = job.run(path)
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(
                tuples_of(job.store, outcome.run_id, "out", ["id", "num"]),
                {("a", -1), ("b", 2 ** 63 - 1), ("c", -(2 ** 63))})

    def test_int64_out_of_range_fact_rejected(self):
        path = self.typed_document([f'{{"id":"a","num":{2 ** 63}}}'])
        with RelationJob(self.work) as job:
            outcome = job.run(path)
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.error["error"]["code"], "int_out_of_range")

    def test_string_comparison_is_exact(self):
        # " A " is not "A", and "a" is not "A".
        (self.work / "facts" / "exact.jsonl").write_text(
            '{"s":" A "}\n{"s":"A"}\n{"s":"a"}\n', encoding="utf-8")
        path = write_rule_document(self.work / "rules" / "exact.json", {
            "src": relationship([("s", "string")], "../facts/exact.jsonl"),
            "out": relationship([("s", "string")])},
            [{"head": {"relation": "out", "args": ["?s"]},
              "body": [{"relation": "src", "args": ["?s"]},
                       {"neq": ["?s", {"const": "A"}]}]}])
        with RelationJob(self.work) as job:
            outcome = job.run(path)
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(
                tuples_of(job.store, outcome.run_id, "out", ["s"]),
                {(" A ",), ("a",)})

    def test_duplicate_facts_are_one_tuple(self):
        (self.work / "facts" / "dupes.jsonl").write_text(
            '{"s":"x"}\n{"s":"x"}\n{"s":"x"}\n', encoding="utf-8")
        path = write_rule_document(self.work / "rules" / "dupes.json", {
            "src": relationship([("s", "string")], "../facts/dupes.jsonl"),
            "out": relationship([("s", "string")])},
            [{"head": {"relation": "out", "args": ["?s"]},
              "body": [{"relation": "src", "args": ["?s"]}]}])
        with RelationJob(self.work) as job:
            outcome = job.run(path)
            self.assertEqual(job.store.relation_count(outcome.run_id, "out"), 1)
            digest = {item["relation"]: item for item in outcome.summary["inputs"]}
            self.assertEqual(digest["src"]["records"], 3)
            self.assertEqual(digest["src"]["duplicate_records"], 2)
            self.assertEqual(digest["src"]["distinct"], 1)


# ---------------------------------------------------------------------------
# Budget, failure and atomicity
# ---------------------------------------------------------------------------


class FailureIsolationTests(RelationCase):
    def test_budget_overflow_fails_without_publishing(self):
        with RelationJob(self.work) as job:
            good = job.run(RULES)
            self.assertEqual(good.status, "completed")
            fingerprint = self.fingerprint(job.store, good.run_id)
            bad = job.run(RULES, budget=3)
            self.assertEqual(bad.status, "failed")
            self.assertEqual(bad.exit_code, 4)
            self.assertEqual(bad.error["kind"], "budget")
            self.assertEqual([row["run_id"] for row in job.store.runs()],
                             [good.run_id])
            self.assertEqual(self.fingerprint(job.store, good.run_id),
                             fingerprint)
            self.assertEqual(job.store.latest_successful_run()["run_id"],
                             good.run_id)

    def test_before_commit_failure_rolls_back(self):
        with RelationJob(self.work) as job:
            good = job.run(RULES)
            fingerprint = self.fingerprint(job.store, good.run_id)
            forced = job.run(RULES, fail_point="before_commit")
            self.assertEqual(forced.status, "failed")
            self.assertEqual(forced.exit_code, 4)
            self.assertEqual(forced.error["kind"], "interrupted")
            self.assertEqual([row["run_id"] for row in job.store.runs()],
                             [good.run_id])
            self.assertEqual(self.fingerprint(job.store, good.run_id),
                             fingerprint)

    def test_hard_process_exit_mid_run(self):
        with RelationJob(self.work) as job:
            good = job.run(RULES)
            fingerprint = self.fingerprint(job.store, good.run_id)
        proc = subprocess.run(
            [sys.executable, "-m", "archive_pipeline.relcli",
             "--workdir", str(self.work), "run", "--rules", str(RULES),
             "--hard-exit"], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 9)
        with RelationJob(self.work) as job:
            runs = [(row["run_id"], row["status"]) for row in job.store.runs()]
            self.assertEqual(runs, [(good.run_id, "completed")])
            self.assertEqual(self.fingerprint(job.store, good.run_id),
                             fingerprint)
            self.assertEqual(job.store.latest_successful_run()["run_id"],
                             good.run_id)
            # and a normal rerun still works, producing a new run
            again = job.run(RULES)
            self.assertEqual(again.status, "completed")
            self.assertGreater(again.run_id, good.run_id)

    def fingerprint(self, store, run_id: int) -> str:
        import hashlib
        payload = {row["relation"]: store.relation_rows(run_id, row["relation"],
                                                        limit=10000)
                   for row in store.relation_stats(run_id)}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                         sort_keys=True).encode()).hexdigest()

    def test_notice_is_read_from_one_snapshot(self):
        stop = threading.Event()

        def writer():
            store = Store(self.work / "pipeline.sqlite3")
            version = 20
            try:
                while not stop.is_set():
                    version += 1
                    with store.tx() as conn:
                        for suffix in ("a", "b"):
                            conn.execute(
                                "INSERT INTO versions(source_id, archive_id, "
                                "notice_key, url_key, version_no, title, "
                                "issued_on, body, raw_digest, content_digest, "
                                "business_digest, scan_id, created_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                ("relsim", f"arc_pair_{suffix}", "N-9999",
                                 "u", version, "noise", "2026-06-02",
                                 "noise", "x", "y", "z", 1, "t"))
                    time.sleep(0.001)
            finally:
                store.close()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            for _ in range(3):
                with RelationJob(self.work) as job:
                    outcome = job.run(RULES)
                    self.assertEqual(outcome.status, "completed")
                    rows = job.store.relation_rows(outcome.run_id, "notice",
                                                   limit=10000)
                    left = sum(1 for row in rows
                               if row["archive_id"] == "arc_pair_a")
                    right = sum(1 for row in rows
                                if row["archive_id"] == "arc_pair_b")
                    # both rows are inserted in one transaction, so a
                    # consistent snapshot sees both or neither
                    self.assertEqual(left, right)
        finally:
            stop.set()
            thread.join(timeout=5)


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


class CliTests(RelationCase):
    def test_check_and_run_exit_codes(self):
        self.assertEqual(cli_main(["--workdir", str(self.work), "check",
                                   "--rules", str(RULES)]), 0)
        self.assertEqual(cli_main(["--workdir", str(self.work), "run",
                                   "--rules", str(RULES)]), 0)
        self.assertEqual(cli_main(["--workdir", str(self.work), "show",
                                   "--relation", "ancestor"]), 0)
        self.assertEqual(cli_main(["--workdir", str(self.work), "show",
                                   "--relation", "nope"]), 2)

    def test_bad_rules_exit_code_three(self):
        path = write_rule_document(self.work / "rules" / "bad.json", {
            "d": relationship([("x", "string")]),
            "p": relationship([("u", "string"), ("v", "string")],
                              "../facts/org.jsonl")},
            [{"head": {"relation": "d", "args": ["?x"]},
              "body": [{"relation": "p", "args": ["?x", "literal"]}]}])
        self.assertEqual(cli_main(["--workdir", str(self.work), "run",
                                   "--rules", str(path)]), 3)
        # nothing was published
        with RelationJob(self.work) as job:
            self.assertEqual(job.store.runs(), [])


if __name__ == "__main__":
    unittest.main()

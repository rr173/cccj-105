"""Offline relation job: declared rules + archived notices + manual relations.

The job is a data transformation, not a question-answering tool:

1. **Check** — parse and validate the rule document completely, build the
   dependency graph, the stratification and the recursive-component plan, and
   report the exact JSON location of any problem.  No fact file and no archive
   database is opened.
2. **Run** — inside a single SQLite transaction:
   * create the ``job_runs`` row in state ``running``;
   * stream the JSONL base relations into the result database (set semantics,
     strict column/type checks with file+line, abort on the first bad record);
   * copy every archived notice version from the pipeline database through one
     read-only snapshot;
   * evaluate the strata in dependency order with semi-naive rounds;
   * write the run summary, the per-input digests, the per-relation counts,
     the per-round delta counts and the execution statistics;
   * commit.  Only then is the run visible as successful.

Failure handling: a bad fact, a budget overflow, a forced mid-run exit or any
unexpected exception rolls the entire transaction back.  The ``job_runs`` row
never commits in ``running`` state, so no failed attempt can be mistaken for a
result, and a previously successful run's rows are untouched.  Results are
never truncated and marked complete — the budget error is explicit.

The archive pipeline database is opened read-only; the result database
``relation.sqlite3`` is the job's own file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .releva import (
    BudgetExceeded,
    Evaluator,
    HardExit,
    Interrupted,
    JobError,
    RequestViolation,
    ResultStore,
    now_iso,
)
from .relfacts import FactError
from .relrule import NOTICE_RELATION, RuleError, RuleSet, load_rules

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Check / plan
# ---------------------------------------------------------------------------


def check_rules(path: str | Path) -> dict:
    """Validate a rule document and describe its plan; reads no data."""
    try:
        ruleset = load_rules(path)
    except RuleError as exc:
        return {"status": "rejected", "issues": [exc.as_dict()],
                "message": "规则文档被拒绝；未读取任何事实，未产出任何关系"}
    return {"status": "accepted", "rules": ruleset.summary(),
            "message": "规则已通过校验：类型、元数、安全性、否定分层均合法"}


def _component_rounds(rounds) -> list[dict]:
    """Aggregate per-relation round rows into per-component/round totals."""
    aggregated: dict[tuple[int, int], dict] = {}
    for record in rounds:
        key = (record.component_id, record.round_no)
        item = aggregated.setdefault(key, {
            "component_id": record.component_id,
            "round_no": record.round_no,
            "rows": [],
            "delta_rows": 0,
            "joins_delta": 0,
            "joins_full": 0,
        })
        item["rows"].append(record.relation)
        item["delta_rows"] += record.delta_rows
        item["joins_delta"] += sum(1 for plan in record.join_plan
                                   if plan["mode"] == "delta")
        item["joins_full"] += sum(1 for plan in record.join_plan
                                  if plan["mode"] == "full")
    return [aggregated[key] for key in sorted(aggregated)]


def format_plan(summary: dict) -> str:
    """Human-readable stratification and recursion plan (edges head -> body)."""
    plan = summary["plan"]
    lines = ["依赖边方向：head depends_on body（箭头由规则头指向正/负体原子）",
             "", "分层与递归分量："]
    for stratum in plan["strata"]:
        relations = ", ".join(stratum["relations"])
        if stratum["recursive"]:
            lines.append(f"  层 {stratum['stratum']}: [{relations}]  "
                         f"递归分量 #{stratum['component']}（半朴素迭代）")
        else:
            lines.append(f"  层 {stratum['stratum']}: [{relations}]  "
                         "非递归（依赖已完成）")
    lines.append("")
    lines.append("强连通分量（求值顺序）：")
    for index, component in enumerate(plan["components"]):
        marker = "递归" if len(component) > 1 else "单点"
        lines.append(f"  #{index}: {component}  [{marker}]")
    lines.append("")
    lines.append("规则依赖边：")
    for edge in plan["dependencies"]:
        arrow = " -not-> " if edge["negative"] else " -> "
        lines.append(f"  {edge['head']}{arrow}{edge['depends_on']} "
                     f"(rule {edge['rule_index']})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    run_id: int
    status: str
    summary: dict | None
    error: dict | None
    exit_code: int


class RelationJob:
    def __init__(self, workdir: str | Path,
                 pipeline_db: str | Path | None = None,
                 result_db: str | Path | None = None,
                 progress: Callable[[str], None] | None = None):
        self.workdir = Path(workdir)
        self.pipeline_db = (Path(pipeline_db) if pipeline_db
                            else self.workdir / "pipeline.sqlite3")
        self.result_db = (Path(result_db) if result_db
                          else self.workdir / "relation.sqlite3")
        self.progress = progress or (lambda _message: None)
        self.store: ResultStore | None = None
        #: The validated RuleSet of the last run attempt (None if rejected).
        self.last_ruleset: RuleSet | None = None

    # -- lifecycle -------------------------------------------------------
    def _open(self) -> ResultStore:
        if self.store is None:
            self.store = ResultStore(self.result_db)
        return self.store

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None

    def __enter__(self) -> "RelationJob":
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- run -------------------------------------------------------------
    def run(self, rules_path: str | Path, *, budget: int | None = None,
            fail_point: str | None = None, progress: bool | None = None,
            hard_exit_after_load: bool = False) -> RunOutcome:
        """Validate, load, evaluate and publish one complete job.

        ``fail_point`` values: ``before_commit``, ``after_evaluate`` and
        ``during_load`` raise a controlled :class:`HardExit` inside the open
        transaction, which rolls back and returns a ``failed`` outcome.
        ``hard_exit_after_load`` calls ``os._exit(9)`` from inside the
        transaction for a real process-death test.
        """
        if progress:
            self.progress = print
        self._open()
        try:
            ruleset = load_rules(rules_path)
        except RuleError as exc:
            self.last_ruleset = None
            return RunOutcome(0, "rejected", None, exc.as_dict(), 3)
        self.last_ruleset = ruleset
        store = self._open()
        started = now_iso()
        budget_value = budget if budget is not None else ruleset.budget
        try:
            return self._execute(store, ruleset, started, budget_value,
                                 fail_point, hard_exit_after_load)
        except FactError as exc:
            return RunOutcome(0, "failed", None, {"kind": "fact",
                                                 "error": exc.as_dict()}, 4)
        except BudgetExceeded as exc:
            return RunOutcome(0, "failed", None, {
                "kind": "budget", "error": exc.detail_json,
                "detail": exc.detail}, 4)
        except (HardExit, Interrupted) as exc:
            return RunOutcome(0, "failed", None, {
                "kind": "interrupted", "detail": str(exc),
                "message": "controlled failure inside the publish transaction; "
                           "the whole run was rolled back"}, 4)
        except (sqlite3.Error, JobError, OSError) as exc:
            return RunOutcome(0, "failed", None,
                              {"kind": "operational", "detail": str(exc)}, 2)

    def _execute(self, store: ResultStore, ruleset: RuleSet, started: str,
                 budget: int, fail_point: str | None,
                 hard_exit_after_load: bool = False) -> RunOutcome:
        conn = store.conn
        # A run row created now is uncommitted until the final COMMIT, so a
        # crash cannot leave a half-run visible.  ``mark_interrupted_runs``
        # exists only for databases written by older broken attempts.
        conn.execute("BEGIN IMMEDIATE")
        evaluator: Evaluator | None = None
        try:
            run_id = self._create_run_row(conn, ruleset, started, budget)
            evaluator = Evaluator(ruleset, conn, run_id, budget=budget)
            self.progress(f"[run {run_id}] loading base facts")
            evaluator.load_bases(self.pipeline_db)
            if fail_point == "during_load":
                raise HardExit("forced exit during fact loading")
            if hard_exit_after_load:
                # Real process death inside the open transaction.  Committed
                # data cannot include this run; SQLite rolls it back.
                import os
                os._exit(9)
            self.progress(f"[run {run_id}] evaluating "
                          f"{len(ruleset.plan.strata)} strata")
            evaluator.evaluate()
            if fail_point == "after_evaluate":
                raise HardExit("forced exit after evaluation")
            self._write_metadata(conn, evaluator)
            summary = self._summary(ruleset, evaluator, run_id, started)
            conn.execute(
                "UPDATE job_runs SET finished_at=?, status='completed', "
                "summary_json=? WHERE run_id=?",
                (now_iso(), json.dumps(summary, ensure_ascii=False), run_id))
            if fail_point == "before_commit":
                raise HardExit("forced exit before commit")
            conn.execute("COMMIT")
            return RunOutcome(run_id, "completed", summary, None, 0)
        except BaseException as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _create_run_row(self, conn: sqlite3.Connection, ruleset: RuleSet,
                        started: str, budget: int) -> int:
        cur = conn.execute(
            "INSERT INTO job_runs(started_at, status, rules_path, rules_sha256, "
            "rules_digest, budget, pipeline_db) VALUES (?, 'running', ?, ?, ?, ?, ?)",
            (started, str(ruleset.path), ruleset.sha256,
             hashlib.sha256(ruleset.canonical_json.encode()).hexdigest(),
             budget, str(self.pipeline_db)))
        return int(cur.lastrowid)

    def _write_metadata(self, conn: sqlite3.Connection,
                        evaluator: Evaluator) -> None:
        run_id = evaluator.run_id
        for row in evaluator.relation_rows_for_meta():
            conn.execute(
                "INSERT INTO rule_relations(run_id, relation, position, "
                "columns_json, base, recursive, stratum) VALUES (?,?,?,?,?,?,?)",
                (run_id, row["relation"], row["position"],
                 json.dumps(row["columns"], ensure_ascii=False),
                 row["base"], row["recursive"], row["stratum"]))
            conn.execute(
                "INSERT INTO relation_stats(run_id, relation, kind, stratum, "
                "tuples) VALUES (?,?,?,?,?)",
                (run_id, row["relation"], row["kind"], row["stratum"],
                 row["tuples"]))
        for digest in evaluator.input_digests:
            conn.execute(
                "INSERT INTO fact_inputs(run_id, relation, kind, digest_json) "
                "VALUES (?,?,?,?)", (run_id, digest["relation"], digest["kind"],
                                     json.dumps(digest, ensure_ascii=False)))
        for record in evaluator.rounds:
            conn.execute(
                "INSERT INTO round_stats(run_id, relation, component_id, "
                "round_no, variants, delta_rows, join_plan_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, record.relation, record.component_id, record.round_no,
                 record.variants, record.delta_rows,
                 json.dumps(record.join_plan, ensure_ascii=False)))
        for component in evaluator.components:
            per_relation = {name: {"rounds": 0, "delta_rows": 0,
                                   "joins_delta": 0, "joins_full": 0}
                            for name in component.relations}
            for item in evaluator.rounds:
                if item.component_id != component.component_id:
                    continue
                stats = per_relation[item.relation]
                stats["rounds"] += 1
                stats["delta_rows"] += item.delta_rows
                stats["joins_delta"] += sum(1 for plan in item.join_plan
                                            if plan["mode"] == "delta")
                stats["joins_full"] += sum(1 for plan in item.join_plan
                                           if plan["mode"] == "full")
            for relation, stats in per_relation.items():
                conn.execute(
                    "INSERT INTO execution_stats(run_id, component_id, relation, "
                    "rounds, delta_rows, joins_delta, joins_full) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (run_id, component.component_id, relation,
                     stats["rounds"], stats["delta_rows"],
                     stats["joins_delta"], stats["joins_full"]))

    def _summary(self, ruleset: RuleSet, evaluator: Evaluator, run_id: int,
                 started: str) -> dict:
        relations = []
        for row in evaluator.relation_rows_for_meta():
            relations.append({"relation": row["relation"], "kind": row["kind"],
                              "tuples": row["tuples"], "stratum": row["stratum"],
                              "recursive": bool(row["recursive"])})
        return {
            "run_id": run_id,
            "started_at": started,
            "finished_at": now_iso(),
            "rules": {
                "path": str(ruleset.path),
                "sha256": ruleset.sha256,
                "digest": hashlib.sha256(
                    ruleset.canonical_json.encode()).hexdigest(),
            },
            "budget": evaluator.budget,
            "total_tuples": evaluator.total,
            "derived_tuples": evaluator.derived_total,
            "pipeline_db": str(self.pipeline_db),
            "notice": (evaluator.notice_view.as_dict()
                       if evaluator.notice_view else None),
            "inputs": evaluator.input_digests,
            "relations": relations,
            "components": [
                {"component_id": component.component_id,
                 "relations": component.relations,
                 "recursive": component.recursive,
                 "rounds": component.rounds,
                 "delta_rows": component.delta_rows,
                 "joins_delta": component.joins_delta,
                 "joins_full": component.joins_full}
                for component in evaluator.components],
            "rounds": [
                {"relation": record.relation,
                 "component_id": record.component_id,
                 "round_no": record.round_no,
                 "variants": record.variants,
                 "delta_rows": record.delta_rows,
                 "joins": [
                     {"rule_index": item["rule_index"],
                      "position": item["position"],
                      "relation": item["relation"],
                      "mode": item["mode"]}
                     for item in record.join_plan
                     if item["mode"] in ("delta", "full", "not_exists")]}
                for record in evaluator.rounds],
            "component_rounds": _component_rounds(evaluator.rounds),
            "plan": ruleset.plan.describe(),
        }


# ---------------------------------------------------------------------------
# Read back
# ---------------------------------------------------------------------------


def read_relation(store: ResultStore, run_id: int, relation: str,
                  *, offset: int = 0, limit: int = 100) -> dict:
    if offset < 0:
        raise RequestViolation(f"offset must be >= 0, got {offset}")
    if limit <= 0 or limit > 1000:
        raise RequestViolation(f"limit must be in 1..1000, got {limit}")
    info = store.relation_info(run_id, relation)
    if info is None:
        raise RequestViolation(f"run {run_id} has no relation {relation!r}")
    total = store.relation_count(run_id, relation)
    return {
        "run_id": run_id,
        "relation": relation,
        "columns": info["columns"],
        "kind": ("notice" if relation == NOTICE_RELATION else
                 ("base" if info["base"] else "derived")),
        "total": total,
        "offset": offset,
        "limit": limit,
        "origin": ("archive pipeline SQLite" if relation == NOTICE_RELATION
                   else ("JSONL base relation" if info["base"]
                         else "derived by rules")),
        "rows": store.relation_rows(run_id, relation, offset=offset,
                                    limit=limit),
    }

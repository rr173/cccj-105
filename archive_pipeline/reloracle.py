"""A deliberately naive, independent reference evaluator (test oracle only).

This module is **not** part of the production path.  It exists so the tests
(and the demonstration report) can cross-check recursively derived relations
against a second implementation that shares no evaluation code with
:mod:`archive_pipeline.releva`.  It is written for tiny finite inputs:

* facts are Python lists of tuples held entirely in memory;
* every stratum is evaluated by repeatedly re-running every rule over the
  **complete** current sets until a pass adds nothing (a brute force fixpoint,
  not semi-naive);
* SCCs and their order are computed by brute-force reachability.

It reads the same JSON rule document and JSONL fact files, but parses them
with its own code and never imports the rule compiler, the SQL evaluator, the
fact loaders or the plan builder.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

NOTICE_COLUMNS = ("source_id", "archive_id", "version_no", "title", "body")


class OracleError(RuntimeError):
    pass


def _var(term):
    if isinstance(term, str) and term.startswith("?"):
        return ("var", term)
    if isinstance(term, dict) and "const" in term:
        value = term["const"]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise OracleError(f"bad constant {value!r}")
        return ("const", value)
    raise OracleError(f"bad term {term!r}")


class Oracle:
    def __init__(self, rules_path: str | Path, facts_dir: str | Path,
                 pipeline_db: str | Path | None = None):
        self.rules_path = Path(rules_path)
        self.document = json.loads(self.rules_path.read_text(encoding="utf-8"))
        self.facts_dir = Path(facts_dir)
        self.relations: dict[str, dict] = {}
        for name, spec in self.document["relations"].items():
            self.relations[name] = {
                "columns": [(c[0], c[1]) for c in spec["columns"]],
                "source": spec.get("source"),
                "base": spec.get("source") is not None,
            }
        self.rules = []
        for index, rule in enumerate(self.document["rules"]):
            positives = [item for item in rule["body"] if "relation" in item]
            negatives = [item["not"] for item in rule["body"] if "not" in item]
            comparisons = [item for item in rule["body"]
                           if "eq" in item or "neq" in item]
            self.rules.append({"index": index, "head": rule["head"],
                               "positives": positives,
                               "negatives": negatives,
                               "comparisons": comparisons})
        self.facts: dict[str, list[tuple]] = {}
        self._load_bases(pipeline_db)
        self._build_plan()

    # -- input -----------------------------------------------------------
    def _load_bases(self, pipeline_db):
        for name, relation in self.relations.items():
            if not relation["base"]:
                continue
            declared = Path(relation["source"]["path"])
            path = (self.rules_path.parent / declared)
            if not path.exists():
                path = self.facts_dir / declared.name
            rows = []
            columns = [c[0] for c in relation["columns"]]
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if set(record) != set(columns):
                    raise OracleError(f"{path}: columns mismatch {record}")
                values = []
                for column in columns:
                    value = record[column]
                    if value is None or isinstance(value, bool):
                        raise OracleError(f"{path}: bad value {value!r}")
                    values.append(value)
                rows.append(tuple(values))
            self.facts[name] = sorted(set(rows))
        self.facts["notice"] = []
        if pipeline_db is not None and Path(pipeline_db).exists():
            conn = sqlite3.connect(f"file:{Path(pipeline_db)}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT source_id, archive_id, version_no, title, body "
                    "FROM versions").fetchall()
            finally:
                conn.close()
            self.facts["notice"] = sorted(set(tuple(row) for row in rows))

    # -- plan by brute force ---------------------------------------------
    def _build_plan(self):
        names = list(self.relations)
        dependencies = {name: set() for name in names}
        negative = {name: set() for name in names}
        for rule in self.rules:
            head = rule["head"]["relation"]
            for atom in rule["positives"]:
                if atom["relation"] != "notice":
                    dependencies[head].add(atom["relation"])
            for atom in rule["negatives"]:
                if atom["relation"] != "notice":
                    dependencies[head].add(atom["relation"])
                    negative[head].add(atom["relation"])

        def reach(start):
            seen, stack = set(), [start]
            while stack:
                node = stack.pop()
                for target in dependencies[node]:
                    if target not in seen:
                        seen.add(target)
                        stack.append(target)
            return seen

        reachable = {name: reach(name) for name in names}
        components = []
        assigned = set()
        for name in names:
            if name in assigned:
                continue
            component = {other for other in names
                         if other == name or
                         (other in reachable[name] and name in reachable[other])}
            assigned |= component
            components.append(sorted(component))
        for component in components:
            for head in component:
                for target in negative[head]:
                    if target in component:
                        raise OracleError("negative edge inside component")
        # order: component B after A when B depends on a member of A
        ordered = []
        remaining = list(components)
        while remaining:
            for component in list(remaining):
                blocked = any(dependencies[head] & set(other)
                              for head in component
                              for other in remaining if other is not component)
                if not blocked:
                    ordered.append(component)
                    remaining.remove(component)
                    break
            else:  # pragma: no cover - the check above rejects cycles
                raise OracleError("component order has a cycle")
        self.components = ordered

    # -- evaluation --------------------------------------------------------
    def _apply_rule(self, rule) -> set[tuple]:
        bindings = [{}]
        for atom in rule["positives"]:
            relation = atom["relation"]
            if relation not in self.facts:
                raise OracleError(f"unknown relation {relation}")
            columns = ([(name, "string") for name in NOTICE_COLUMNS]
                       if relation == "notice"
                       else self.relations[relation]["columns"])
            terms = [_var(term) for term in atom["args"]]
            if len(terms) != len(columns):
                raise OracleError(f"arity {relation}")
            extended = []
            for binding in bindings:
                for row in self.facts[relation]:
                    candidate = dict(binding)
                    good = True
                    for term, value in zip(terms, row):
                        if term[0] == "const":
                            if value != term[1] or type(value) is not type(term[1]):
                                good = False
                                break
                        elif term[1] in candidate:
                            if candidate[term[1]] != value:
                                good = False
                                break
                        else:
                            candidate[term[1]] = value
                    if good:
                        extended.append(candidate)
            bindings = extended
            if not bindings:
                return set()
        results = set()
        for binding in bindings:
            ok = True
            for comparison in rule["comparisons"]:
                sides = []
                for term in (comparison.get("eq") or comparison.get("neq")):
                    parsed = _var(term)
                    sides.append(parsed[1] if parsed[0] == "const"
                                 else binding[parsed[1]])
                if type(sides[0]) is not type(sides[1]):
                    raise OracleError("comparison across types")
                equal = sides[0] == sides[1]
                if (comparison["eq"] if "eq" in comparison else None) is not None:
                    ok = ok and equal
                else:
                    ok = ok and not equal
                if not ok:
                    break
            if not ok:
                continue
            for atom in rule["negatives"]:
                terms = [_var(term) for term in atom["args"]]
                matched = False
                for row in self.facts[atom["relation"]]:
                    good = True
                    for term, value in zip(terms, row):
                        expected = (term[1] if term[0] == "const"
                                    else binding[term[1]])
                        if value != expected or type(value) is not type(expected):
                            good = False
                            break
                    if good:
                        matched = True
                        break
                if matched:
                    ok = False
                    break
            if not ok:
                continue
            head_terms = [_var(term) for term in rule["head"]["args"]]
            results.add(tuple(term[1] if term[0] == "const"
                              else binding[term[1]] for term in head_terms))
        return results

    def evaluate(self) -> dict[str, list[tuple]]:
        for component in self.components:
            member = set(component)
            rules = [rule for rule in self.rules
                     if rule["head"]["relation"] in member]
            for name in component:
                self.facts.setdefault(name, [])
            changed = True
            while changed:
                changed = False
                for rule in rules:
                    new = self._apply_rule(rule) - set(
                        self.facts[rule["head"]["relation"]])
                    if new:
                        self.facts[rule["head"]["relation"]] = sorted(
                            set(self.facts[rule["head"]["relation"]]) | new)
                        changed = True
        return {name: sorted(rows) for name, rows in self.facts.items()}

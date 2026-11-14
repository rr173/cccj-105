"""Declarative relation rules: parse, type-check, stratify, plan.

This module turns a hand-authored JSON rule document into a fully validated
plan.  It never reads a fact tuple, never touches the archive database and
never evaluates user-supplied Python or SQL text.

Document shape (``format: relation-rules/1``)
---------------------------------------------
::

    {
      "format": "relation-rules/1",
      "relations": {
        "org":     {"columns": [["org_id", "string"]]},
        "parent":  {"columns": [["child", "string"], ["parent", "string"]],
                    "source": {"kind": "jsonl", "path": "parent.jsonl"}},
        "mentions":{"columns": [["archive_id", "string"], ["version_no", "int"],
                                ["org_id", "string"]],
                    "source": {"kind": "jsonl", "path": "mentions.jsonl"}},
        "ancestor":{"columns": [["org_id", "string"], ["ancestor", "string"]]}
      },
      "rules": [
        {"head": {"relation": "ancestor", "args": ["?x", "?y"]},
         "body": [
           {"relation": "parent", "args": ["?x", "?y"]},
           {"not": {"relation": "excluded", "args": ["?x", "?z"]}},
           {"eq": ["?y", {"const": "ROOT"}]}
         ]}
      ]
    }

* One reserved relation name, ``notice``, is **not** declared here.  It is read
  read-only from the archive SQLite database and always has the columns
  ``source_id:string, archive_id:string, version_no:int, title:string,
  body:string``.  Its identity is ``(source_id, archive_id, version_no)`` —
  never the printed notice number, and never a guess from the body.
* A relation either declares a ``source`` (a *base* relation backed by JSONL)
  or has no source at all (a *derived* relation that must be defined by some
  rule head).  A relation with a source may **not** be used as a rule head.
* ``{"const": value}`` is a typed constant; any bare string that starts with
  ``?`` is a variable; every other bare string is rejected rather than guessed
  to be a constant.  Constants carry their type (string / int); booleans are
  never accepted as integers.

Validation order (all of it happens before a single fact is read)
------------------------------------------------------------------
1. JSON syntax and the top-level shape;
2. per-relation column declarations (types, duplicate names/columns, sources);
3. per-rule syntax: head/body positions, arity, unknown relations, variable
   and constant typing, repeated variables, comparison types, safety;
4. base relations used as heads, undefined derived relations, duplicate rules;
5. dependency graph (positive and negative edges) and stratification.

Any violation raises :class:`RuleError` carrying the exact JSON location.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = "relation-rules/1"
NOTICE_FORMAT = "notice-relation/1"
NOTICE_RELATION = "notice"
NOTICE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source_id", "string"),
    ("archive_id", "string"),
    ("version_no", "int"),
    ("title", "string"),
    ("body", "string"),
)

TYPES = ("string", "int")
SOURCE_KINDS = ("jsonl",)

#: The fact store uses one fixed-width tuple table; this is a storage limit,
#: not a semantic one.  The reserved ``notice`` relation uses five columns.
MAX_COLUMNS = 8
#: Unused fixed-width columns are filled with this JSON constant (never NULL,
#: so the primary key gives real set semantics).
FACT_PAD = json.dumps("~rel-unused~")

#: Exit code for a rejected rule document.
EXIT_RULES = 3


class RuleError(RuntimeError):
    """The rule document is unusable; ``location`` is a JSON path string."""

    def __init__(self, code: str, detail: str, location: str = "",
                 rule_index: int | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.location = location
        self.rule_index = rule_index

    def as_dict(self) -> dict:
        out = {"code": self.code, "detail": self.detail,
               "location": self.location}
        if self.rule_index is not None:
            out["rule_index"] = self.rule_index
        return out


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Term:
    """A variable (``?x``) or a typed constant."""

    kind: str                      # "var" | "const"
    value: str | int
    type: str                      # "string" | "int"
    text: str                      # original spelling, for diagnostics


@dataclass(frozen=True)
class Atom:
    relation: str
    args: tuple[Term, ...]
    negated: bool = False
    location: str = ""


@dataclass(frozen=True)
class Comparison:
    op: str                        # "eq" | "neq"
    left: Term
    right: Term
    location: str = ""


@dataclass(frozen=True)
class Rule:
    index: int
    head: Atom
    positives: tuple[Atom, ...]
    negatives: tuple[Atom, ...]
    comparisons: tuple[Comparison, ...]
    location: str = ""
    description: str = ""


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class Relation:
    name: str
    columns: tuple[Column, ...]
    source: dict | None = None         # {"kind": "jsonl", "path": "..."}
    rules: tuple[Rule, ...] = ()       # every rule with this head, in file order

    @property
    def arity(self) -> int:
        return len(self.columns)

    @property
    def is_base(self) -> bool:
        return self.source is not None

    def column_types(self) -> tuple[str, ...]:
        return tuple(column.type for column in self.columns)


@dataclass(frozen=True)
class DependencyEdge:
    """``head`` depends on ``target``; ``negative`` marks a negated body atom."""

    head: str
    target: str
    negative: bool
    rule_index: int


@dataclass(frozen=True)
class Stratum:
    index: int
    relations: tuple[str, ...]
    #: Strongly connected component id (Tarjan order) when recursive.
    component: int | None
    recursive: bool


@dataclass(frozen=True)
class Plan:
    strata: tuple[Stratum, ...]                 # derived relations only
    edges: tuple[DependencyEdge, ...]
    components: tuple[tuple[str, ...], ...]   # SCCs in evaluation order
    relation_stratum: dict[str, int]
    relation_component: dict[str, int | None]
    base_relations: tuple[str, ...] = ()

    def describe(self) -> dict:
        return {
            "edge_direction": "head depends_on body atom",
            "base_relations": list(self.base_relations),
            "strata": [
                {"stratum": s.index, "relations": list(s.relations),
                 "component": s.component, "recursive": s.recursive}
                for s in self.strata],
            "dependencies": [
                {"head": e.head, "depends_on": e.target,
                 "negative": e.negative, "rule_index": e.rule_index}
                for e in self.edges],
            "components": [list(c) for c in self.components],
        }


@dataclass
class RuleSet:
    path: Path
    sha256: str
    canonical_json: str
    relations: dict[str, Relation]           # user relations, declaration order
    rules: tuple[Rule, ...]
    plan: Plan
    budget: int

    def summary(self) -> dict:
        return {
            "format": FORMAT,
            "path": str(self.path),
            "sha256": self.sha256,
            "budget": self.budget,
            "relations": [
                {"name": name,
                 "columns": [[c.name, c.type] for c in rel.columns],
                 "base": rel.is_base,
                 "source": rel.source}
                for name, rel in self.relations.items()],
            "notice": {
                "columns": [[n, t] for n, t in NOTICE_COLUMNS],
                "identity": ["source_id", "archive_id", "version_no"],
                "source": "archive pipeline SQLite (read-only view)"},
            "rules": [
                {"rule_index": rule.index,
                 "location": rule.location,
                 "head": render_atom(rule.head),
                 "body": [render_atom(a) for a in rule.positives]
                         + ["not " + render_atom(a) for a in rule.negatives]
                         + [render_comparison(c) for c in rule.comparisons]}
                for rule in self.rules],
            "plan": self.plan.describe(),
        }


def render_term(term: Term) -> str:
    if term.kind == "var":
        return term.value
    if term.type == "string":
        return json.dumps(term.value, ensure_ascii=False)
    return str(term.value)


def render_atom(atom: Atom) -> str:
    return f"{atom.relation}({', '.join(render_term(a) for a in atom.args)})"


def render_comparison(item: Comparison) -> str:
    op = "=" if item.op == "eq" else "!="
    return f"{render_term(item.left)} {op} {render_term(item.right)}"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(pairs):
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def _is_int(value: Any) -> bool:
    return type(value) is int and not isinstance(value, bool)


def _expect_object(value: Any, location: str) -> dict:
    if not isinstance(value, dict):
        raise RuleError("expected_object", f"{location} must be a JSON object",
                        location)
    return value


def _reject_unknown(obj: dict, allowed: set[str], location: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise RuleError("unknown_key",
                        f"{location}: unknown key(s) {unknown}", location)


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuleError("expected_string",
                        f"{location} must be a non-empty string", location)
    return value


# ---------------------------------------------------------------------------
# Term parsing
# ---------------------------------------------------------------------------


def parse_term(raw: Any, location: str) -> Term:
    """Parse one argument position.

    A JSON string beginning with ``?`` is a variable.  A JSON string that does
    **not** begin with ``?`` is rejected — guessing it to be a constant would
    turn a typo into a silent filter.  A typed constant must be written as
    ``{"const": VALUE}``.
    """
    if isinstance(raw, str):
        if not raw.startswith("?"):
            raise RuleError(
                "bare_string_term",
                f"{location}: {raw!r} is a bare string; variables must start "
                "with '?' and constants must be written as {{\"const\": ...}}",
                location)
        name = raw[1:]
        if not name or not all(ch.isalnum() or ch == "_" for ch in name) \
                or name[0].isdigit():
            raise RuleError(
                "bad_variable_name",
                f"{location}: {raw!r} is not a valid variable name "
                "(letters, digits, underscore; must not start with a digit)",
                location)
        return Term("var", raw, "var", raw)
    if isinstance(raw, dict):
        _reject_unknown(raw, {"const"}, location)
        if "const" not in raw:
            raise RuleError("bad_term",
                            f"{location}: an object term must be {{\"const\": ...}}",
                            location)
        value = raw["const"]
        if isinstance(value, str):
            return Term("const", value, "string", json.dumps(value,
                                                             ensure_ascii=False))
        if _is_int(value):
            if -(2 ** 63) <= value < 2 ** 63:
                return Term("const", value, "int", str(value))
            raise RuleError(
                "int_out_of_range",
                f"{location}: integer constant {value} is outside the signed "
                "64-bit range", location)
        if isinstance(value, bool):
            raise RuleError(
                "bool_not_int",
                f"{location}: booleans are not integers and cannot be used as "
                "a typed constant", location)
        if value is None:
            raise RuleError("null_constant",
                            f"{location}: null is not a valid typed constant",
                            location)
        raise RuleError("bad_constant_type",
                        f"{location}: constants must be strings or signed "
                        f"64-bit integers, got {type(value).__name__}", location)
    raise RuleError("bad_term",
                    f"{location}: expected a variable string or {{\"const\": ...}}, "
                    f"got {type(raw).__name__}", location)


# ---------------------------------------------------------------------------
# Atom / rule parsing
# ---------------------------------------------------------------------------


def _parse_atom(raw: Any, location: str, relations: dict[str, Relation],
                *, allow_notice: bool = True) -> Atom:
    obj = _expect_object(raw, location)
    _reject_unknown(obj, {"relation", "args"}, location)
    name = _nonempty_string(obj.get("relation"), f"{location}.relation")
    args = obj.get("args")
    if not isinstance(args, list):
        raise RuleError("bad_args", f"{location}.args must be a list", location)
    if name != NOTICE_RELATION:
        relation = relations.get(name)
        if relation is None:
            raise RuleError("unknown_relation",
                            f"{location}: relation {name!r} is not declared",
                            location)
        arity = relation.arity
        types = relation.column_types()
    else:
        if not allow_notice:
            raise RuleError("notice_not_allowed_here",
                            f"{location}: the reserved notice relation is a "
                            "base relation and cannot be a rule head", location)
        arity = len(NOTICE_COLUMNS)
        types = tuple(t for _n, t in NOTICE_COLUMNS)
    if len(args) != arity:
        raise RuleError(
            "arity_mismatch",
            f"{location}: relation {name!r} has {arity} columns but the atom "
            f"supplies {len(args)} arguments", location)
    terms = tuple(
        parse_term(arg, f"{location}.args[{index}]")
        for index, arg in enumerate(args))
    for index, (term, declared) in enumerate(zip(terms, types)):
        if term.kind == "const" and term.type != declared:
            raise RuleError(
                "constant_type_mismatch",
                f"{location}.args[{index}]: constant {term.text} is {term.type} "
                f"but relation {name!r} column {index} is {declared}",
                f"{location}.args[{index}]")
    return Atom(name, terms, False, location)


def _parse_comparison(raw: Any, location: str) -> Comparison:
    obj = _expect_object(raw, location)
    _reject_unknown(obj, {"eq", "neq"}, location)
    if ("eq" in obj) == ("neq" in obj):
        raise RuleError("bad_comparison",
                        f"{location}: exactly one of 'eq'/'neq' is required",
                        location)
    op = "eq" if "eq" in obj else "neq"
    operands = obj[op]
    if not isinstance(operands, list) or len(operands) != 2:
        raise RuleError("bad_comparison",
                        f"{location}.{op} must be a two-element list", location)
    left = parse_term(operands[0], f"{location}.{op}[0]")
    right = parse_term(operands[1], f"{location}.{op}[1]")
    for index, term in enumerate((left, right)):
        if term.kind == "const" and term.type not in TYPES:
            raise RuleError("bad_comparison",
                            f"{location}.{op}[{index}]: constants are string or "
                            "signed 64-bit integer", location)
    return Comparison(op, left, right, location)


def parse_rule(raw: Any, rule_index: int, relations: dict[str, Relation]) -> Rule:
    location = f"rules[{rule_index}]"
    obj = _expect_object(raw, location)
    _reject_unknown(obj, {"head", "body", "description"}, location)
    if "description" in obj and not isinstance(obj["description"], str):
        raise RuleError("bad_description",
                        f"{location}.description must be a string",
                        f"{location}.description", rule_index)
    if "head" not in obj:
        raise RuleError("missing_head", f"{location}: missing 'head'", location)
    if "body" not in obj:
        raise RuleError("missing_body", f"{location}: missing 'body'", location)
    head = _parse_atom(obj["head"], f"{location}.head", relations,
                       allow_notice=False)
    head_relation = relations.get(head.relation)
    if head_relation is None:  # pragma: no cover - parse_atom already refuses
        raise RuleError("unknown_relation", f"{location}.head: unknown relation",
                        location)
    if head_relation.is_base:
        raise RuleError(
            "base_relation_as_head",
            f"{location}.head: relation {head.relation!r} is backed by a base "
            "source and cannot be derived by a rule",
            location)
    body = obj["body"]
    if not isinstance(body, list) or not body:
        raise RuleError("empty_body",
                        f"{location}.body must be a non-empty list", location)
    positives: list[Atom] = []
    negatives: list[Atom] = []
    comparisons: list[Comparison] = []
    for index, item in enumerate(body):
        where = f"{location}.body[{index}]"
        if not isinstance(item, dict):
            raise RuleError("bad_body_item",
                            f"{where} must be an object", where)
        if "not" in item:
            _reject_unknown(item, {"not"}, where)
            atom = _parse_atom(item["not"], f"{where}.not", relations)
            negatives.append(Atom(atom.relation, atom.args, True, f"{where}.not"))
        elif "eq" in item or "neq" in item:
            comparisons.append(_parse_comparison(item, where))
        elif "relation" in item:
            positives.append(_parse_atom(item, where, relations))
        else:
            raise RuleError(
                "bad_body_item",
                f"{where}: a body item must be a positive atom "
                "(relation/args), {{\"not\": atom}} or {{\"eq\"|\"neq\": [a, b]}}",
                where)
    if not positives:
        raise RuleError(
            "no_positive_atom",
            f"{location}: a rule needs at least one positive body atom; "
            "negation or equality cannot create a variable's range", location)
    return Rule(rule_index, head, tuple(positives), tuple(negatives),
                tuple(comparisons), location, obj.get("description", ""))


# ---------------------------------------------------------------------------
# Typing and safety
# ---------------------------------------------------------------------------


def _relation_types(name: str, relations: dict[str, Relation]) -> tuple[str, ...]:
    if name == NOTICE_RELATION:
        return tuple(t for _n, t in NOTICE_COLUMNS)
    return relations[name].column_types()


def check_rule(rule: Rule, relations: dict[str, Relation]) -> None:
    """Variable typing and safety for one rule, with exact locations."""
    # 1. A variable used more than once inside one atom (same or different
    #    column) is allowed, but then the two columns must have the same type.
    variable_types: dict[str, str] = {}
    bound_in_positive: set[str] = set()
    for atom in rule.positives:
        declared = _relation_types(atom.relation, relations)
        for index, term in enumerate(atom.args):
            if term.kind != "var":
                continue
            column_type = declared[index]
            seen = variable_types.get(term.value)
            if seen is not None and seen != column_type:
                raise RuleError(
                    "variable_type_conflict",
                    f"{atom.location}.args[{index}]: variable {term.value} is "
                    f"used both as {seen} and as {column_type} in rule "
                    f"{rule.index}", atom.location, rule.index)
            variable_types[term.value] = column_type
            bound_in_positive.add(term.value)
    # 2. Negated atoms: every variable must be bound by a positive atom, and
    #    the variable's type must be consistent with the negated column.
    for atom in rule.negatives:
        declared = _relation_types(atom.relation, relations)
        for index, term in enumerate(atom.args):
            if term.kind != "var":
                continue
            if term.value not in bound_in_positive:
                raise RuleError(
                    "unsafe_variable",
                    f"{atom.location}.args[{index}]: variable {term.value} does "
                    "not occur in any positive body atom; negation cannot "
                    "create its range", atom.location, rule.index)
            seen = variable_types.get(term.value)
            if seen is not None and seen != declared[index]:
                raise RuleError(
                    "variable_type_conflict",
                    f"{atom.location}.args[{index}]: variable {term.value} is "
                    f"{seen} here but the negated relation column is "
                    f"{declared[index]}", atom.location, rule.index)
            variable_types[term.value] = declared[index]
    # 3. Comparisons: both sides must have the same type; a variable on either
    #    side must be positive-bound.
    for comparison in rule.comparisons:
        resolved: list[str] = []
        for side, term in (("left", comparison.left),
                           ("right", comparison.right)):
            if term.kind == "const":
                resolved.append(term.type)
                continue
            if term.value not in bound_in_positive:
                raise RuleError(
                    "unsafe_variable",
                    f"{comparison.location}.{comparison.op} ({side}): variable "
                    f"{term.value} does not occur in any positive body atom; "
                    "a comparison cannot create its range",
                    comparison.location, rule.index)
            known = variable_types.get(term.value)
            if known is not None and known != "var":
                resolved.append(known)
            else:  # pragma: no cover - every positive occurrence sets a type
                raise RuleError("untyped_variable",
                                f"{comparison.location}: {term.value} has no "
                                "declared type", comparison.location, rule.index)
        if resolved[0] != resolved[1]:
            raise RuleError(
                "comparison_type_mismatch",
                f"{comparison.location}: comparing {resolved[0]} with "
                f"{resolved[1]} is not allowed; equality only compares the "
                "same type", comparison.location, rule.index)
    # 4. Head variables must be bound by a positive atom, and their type must
    #    match the head column they are projected into.
    head_types = _relation_types(rule.head.relation, relations)
    for index, term in enumerate(rule.head.args):
        if term.kind != "var":
            continue
        if term.value not in bound_in_positive:
            raise RuleError(
                "unsafe_head_variable",
                f"{rule.head.location}.args[{index}]: head variable {term.value} "
                "does not occur in any positive body atom", rule.head.location,
                rule.index)
        bound_type = variable_types[term.value]
        if bound_type != head_types[index]:
            raise RuleError(
                "variable_type_conflict",
                f"{rule.head.location}.args[{index}]: variable {term.value} is "
                f"{bound_type} in the body but the head column is "
                f"{head_types[index]}", rule.head.location, rule.index)


# ---------------------------------------------------------------------------
# Stratification
# ---------------------------------------------------------------------------


def _tarjan(nodes: list[str], edges: list[tuple[str, str]]
            ) -> list[list[str]]:
    """Tarjan SCCs; components come back in dependency-first order."""
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    for head, target in edges:
        adjacency[head].append(target)
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []
    counter = [0]

    def strongconnect(node: str) -> None:
        index_of[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for target in adjacency[node]:
            if target not in index_of:
                strongconnect(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index_of[target])
        if low[node] == index_of[node]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.discard(item)
                component.append(item)
                if item == node:
                    break
            components.append(component)

    for node in nodes:
        if node not in index_of:
            strongconnect(node)
    return components


def build_plan(relations: dict[str, Relation], rules: tuple[Rule, ...]) -> Plan:
    """Dependency graph, SCCs, stratification and evaluation plan.

    Dependency edges point **from the head relation to each body relation**
    (``head depends on body``).  The SCC analysis runs over derived relations
    only; base relations (JSONL sources and the reserved ``notice`` relation)
    have no incoming rule and are simply loaded before stratum 0.  Tarjan
    yields components in dependency-first order, so a component is always
    evaluated after everything it depends on.

    A negative edge whose two endpoints fall in the same SCC rejects the whole
    document: a relation would have to be fully computed before it was used,
    which is exactly the negation-through-recursion cycle the language forbids.
    This is checked structurally, so an empty input cannot make it legal.
    """
    base_relations = tuple(name for name, relation in relations.items()
                           if relation.is_base)
    derived_nodes = [name for name, relation in relations.items()
                     if not relation.is_base]
    edges: list[DependencyEdge] = []
    for rule in rules:
        for atom in list(rule.positives) + list(rule.negatives):
            edges.append(DependencyEdge(rule.head.relation, atom.relation,
                                        atom.negated, rule.index))
    derived_set = set(derived_nodes)
    internal_edges = [(edge.head, edge.target) for edge in edges
                      if edge.target in derived_set]
    components = _tarjan(derived_nodes, internal_edges)
    component_of: dict[str, int] = {}
    for cid, component in enumerate(components):
        for name in component:
            component_of[name] = cid
    for edge in edges:
        if (edge.negative and edge.target in component_of
                and edge.head in component_of
                and component_of[edge.head] == component_of[edge.target]):
            raise RuleError(
                "negative_cycle",
                f"rule {edge.rule_index}: negative dependency {edge.head} -> "
                f"not {edge.target} lies inside recursive component "
                f"{sorted(components[component_of[edge.head]])}; stratified "
                "negation requires the negated relation to be fully computed "
                "first (this is rejected even when the input is empty)",
                f"rules[{edge.rule_index}]", edge.rule_index)
    recursive_components = {
        cid for cid, component in enumerate(components) if len(component) > 1
    }
    for edge in internal_edges:
        if edge[0] == edge[1]:
            recursive_components.add(component_of[edge[0]])
    strata: list[Stratum] = []
    relation_stratum: dict[str, int] = {}
    relation_component: dict[str, int | None] = {}
    for cid, component in enumerate(components):
        stratum_index = len(strata)
        strata.append(Stratum(stratum_index, tuple(sorted(component)),
                              cid if cid in recursive_components else None,
                              cid in recursive_components))
        for name in component:
            relation_stratum[name] = stratum_index
            relation_component[name] = (cid if cid in recursive_components
                                        else None)
    for name in base_relations:
        relation_stratum[name] = -1
        relation_component[name] = None
    return Plan(tuple(strata), tuple(edges), tuple(tuple(c) for c in components),
                relation_stratum, relation_component,
                tuple(sorted(base_relations)) + (NOTICE_RELATION,))


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------


def _parse_source(raw: Any, location: str, document_dir: Path,
                  relation_name: str) -> dict:
    obj = _expect_object(raw, location)
    _reject_unknown(obj, {"kind", "path"}, location)
    kind = obj.get("kind")
    if kind not in SOURCE_KINDS:
        raise RuleError("unknown_source_kind",
                        f"{location}.kind must be one of {list(SOURCE_KINDS)}",
                        location)
    path = _nonempty_string(obj.get("path"), f"{location}.path")
    return {"kind": kind, "path": path, "document_dir": str(document_dir),
            "relation": relation_name}


def load_rules(path: str | Path) -> RuleSet:
    """Read and fully validate a rule document.

    Nothing is read from any data file; the returned object is a plan.
    """
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RuleError("unreadable_rules",
                        f"cannot read rule document {path}: {exc}") from exc
    import hashlib
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    try:
        raw = json.loads(raw_bytes.decode("utf-8"),
                         object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuleError("bad_json",
                        f"rule document is not valid UTF-8 JSON: {exc}") from exc
    root = _expect_object(raw, "$")
    _reject_unknown(root, {"format", "description", "budget", "relations",
                           "rules"}, "$")
    if root.get("format") != FORMAT:
        raise RuleError("bad_format",
                        f"$.format must be {FORMAT!r}, got {root.get('format')!r}",
                        "$.format")
    description = root.get("description", "")
    if not isinstance(description, str):
        raise RuleError("bad_description",
                        "$.description must be a string", "$.description")
    budget = root.get("budget")
    if budget is None:
        budget = 100_000
    if not _is_int(budget) or budget <= 0:
        raise RuleError("bad_budget",
                        "$.budget must be a positive signed 64-bit integer",
                        "$.budget")
    relations_raw = root.get("relations")
    if not isinstance(relations_raw, dict) or not relations_raw:
        raise RuleError("bad_relations",
                        "$.relations must be a non-empty object", "$.relations")
    document_dir = path.parent
    relations: dict[str, Relation] = {}
    for name, body in relations_raw.items():
        location = f"$.relations[{name!r}]"
        if name == NOTICE_RELATION:
            raise RuleError(
                "reserved_relation",
                f"{location}: {NOTICE_RELATION!r} is reserved and always read "
                "from the archive database; do not declare it", location)
        if not name or not all(ch.isalnum() or ch == "_" for ch in name):
            raise RuleError("bad_relation_name",
                            f"{location}: relation names use letters, digits and "
                            "underscore", location)
        obj = _expect_object(body, location)
        _reject_unknown(obj, {"columns", "source"}, location)
        columns_raw = obj.get("columns")
        if not isinstance(columns_raw, list) or not columns_raw:
            raise RuleError("bad_columns",
                            f"{location}.columns must be a non-empty list",
                            location)
        columns: list[Column] = []
        seen_names: set[str] = set()
        for index, column_raw in enumerate(columns_raw):
            where = f"{location}.columns[{index}]"
            if not isinstance(column_raw, list) or len(column_raw) != 2:
                raise RuleError("bad_column",
                                f"{where} must be [name, type]", where)
            column_name = column_raw[0]
            column_type = column_raw[1]
            if not isinstance(column_name, str) or not column_name:
                raise RuleError("bad_column",
                                f"{where}[0] must be a non-empty string", where)
            if column_name in seen_names:
                raise RuleError("duplicate_column",
                                f"{where}: duplicate column name "
                                f"{column_name!r}", where)
            seen_names.add(column_name)
            if column_type not in TYPES:
                raise RuleError(
                    "bad_column_type",
                    f"{where}: type must be one of {list(TYPES)}; booleans are "
                    "not represented", where)
            columns.append(Column(column_name, column_type))
        if len(columns) > MAX_COLUMNS:
            raise RuleError(
                "too_many_columns",
                f"{location}.columns: the fact store supports at most "
                f"{MAX_COLUMNS} columns per relation, got {len(columns)}",
                location + ".columns")
        source_raw = obj.get("source")
        source = None
        if source_raw is not None:
            source = _parse_source(source_raw, f"{location}.source",
                                   document_dir, name)
        relations[name] = Relation(name, tuple(columns), source)

    rules_raw = root.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise RuleError("bad_rules", "$.rules must be a non-empty list",
                        "$.rules")
    parsed_rules = [parse_rule(item, index, relations)
                    for index, item in enumerate(rules_raw)]
    for rule in parsed_rules:
        check_rule(rule, relations)
    # Every derived relation must have at least one defining rule; a base
    # relation must never be the head of one (checked in parse_rule).
    defined = {rule.head.relation for rule in parsed_rules}
    for name, relation in relations.items():
        if not relation.is_base and name not in defined:
            raise RuleError("undefined_derived_relation",
                            f"$.relations[{name!r}]: relation is not backed by a "
                            "source and no rule head defines it",
                            f"$.relations[{name!r}]")
    # Attach rules to their head relations, preserving file order.
    by_head: dict[str, list[Rule]] = {}
    for rule in parsed_rules:
        by_head.setdefault(rule.head.relation, []).append(rule)
    attached: dict[str, Relation] = {}
    for name, relation in relations.items():
        attached[name] = Relation(name, relation.columns, relation.source,
                                  tuple(by_head.get(name, ())))
    plan = build_plan(attached, tuple(parsed_rules))
    canonical = json.dumps(root, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return RuleSet(path, sha256, canonical, attached, tuple(parsed_rules),
                   plan, budget)

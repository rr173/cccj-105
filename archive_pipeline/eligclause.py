"""Clause definition loading, validation and archive binding.

A clause file is plain JSON.  It binds exactly one archived notice version
(``source_id`` + ``archive_id`` + ``version_no``), declares the typed fields
of the application subject and of each team member, and defines named
conditions.  Every named condition carries a unique id and a citation into
the archived body: a half-open character span plus the exact quoted original
sentence.  The clause is accepted only when the archived body slice equals
that quote — a missing version, a citation pointing at another variant of the
same notice number, or a shifted span is a hard error.

All authoring mistakes (misspelled JSON keys, undeclared field names, wrong
scope, a constant of the wrong type, undefined references, reference cycles)
are collected and raised :class:`ClauseError` *before the first application
is read*.  Such mistakes are never downgraded to a per-application "needs
evidence" verdict.

Example clause (see ``scenario/eligibility/clauses/E-9001-v1.json``)::

    {
      "format": "eligibility-clause/1",
      "source_id": "eligsim",
      "archive_id": "arc_…",
      "version_no": 1,
      "scope": {
        "subject": {"fields": {"entity_type": "string",
                               "registered_years": "decimal"}},
        "member":  {"fields": {"degree_level": "string",
                               "years_experience": "decimal"}}
      },
      "conditions": [
        {"id": "ENTITY_TYPE",
         "scope": "subject",
         "citation": {"start": 0, "end": 25, "quote": "申报主体应为…"},
         "expr": {"field": "entity_type", "op": "in",
                  "values": ["research_institute", "university"]}}
      ],
      "root": "ROOT"
    }

Condition expression language::

    {"field": "F", "op": "eq|gt|ge|lt|le", "value": CONSTANT}
    {"field": "F", "op": "in", "values": [CONSTANT, ...]}
    {"all": [EXPR, ...]}      empty all is true
    {"any": [EXPR, ...]}      empty any is false
    {"not": EXPR}             unknown stays unknown
    {"ref": "CONDITION_ID"}   same-scope named condition, forward refs allowed
    {"count": {"predicate": MEMBER_EXPR, "at_least": K}}

``count`` is only valid in subject scope; the predicate is evaluated in
member scope, once per member, so a predicate combining degree and years
always reads both fields from the same person.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .eligvalue import FIELD_TYPES, validate_constant

CLAUSE_FORMAT = "eligibility-clause/1"
SCOPES = ("subject", "member")
COMPARISON_OPS = {"eq", "gt", "ge", "lt", "le"}
ENUM_OP = "in"


class ClauseError(RuntimeError):
    """One or more clause authoring/binding errors; no judgment may be made."""

    def __init__(self, issues: list[dict], message: str = "invalid clause"):
        super().__init__(message)
        self.issues = issues

    def pretty(self) -> str:
        return "\n".join(
            f"- [{issue['code']}] {issue['location']}: {issue['detail']}"
            for issue in self.issues)


@dataclass(frozen=True)
class ClauseBinding:
    source_id: str
    archive_id: str
    version_no: int
    notice_key: str | None
    title: str
    issued_on: str
    body_sha256: str
    raw_digest: str


@dataclass
class Condition:
    id: str
    scope: str
    citation: dict
    expr: dict
    index: int
    description: str | None = None


@dataclass
class Clause:
    path: Path
    file_sha256: str
    canonical_json: str
    digest: str
    binding: ClauseBinding
    scopes: dict[str, dict[str, str]]
    conditions: dict[str, Condition]
    condition_order: list[str]
    root: str

    def summary(self) -> dict:
        return {
            "format": CLAUSE_FORMAT,
            "clause_path": str(self.path),
            "file_sha256": self.file_sha256,
            "clause_digest": self.digest,
            "binding": {
                "source_id": self.binding.source_id,
                "archive_id": self.binding.archive_id,
                "version_no": self.binding.version_no,
                "notice_key": self.binding.notice_key,
                "title": self.binding.title,
                "issued_on": self.binding.issued_on,
                "body_sha256": self.binding.body_sha256,
                "raw_digest": self.binding.raw_digest,
            },
            "scope": self.scopes,
            "root": self.root,
            "conditions": [
                {"id": cid, "scope": self.conditions[cid].scope,
                 "citation": self.conditions[cid].citation,
                 "description": self.conditions[cid].description}
                for cid in self.condition_order
            ],
        }


class PipelineReader:
    """Read-only view of the identity-schema pipeline database."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        if not self.path.exists():
            raise ClauseError([{
                "code": "pipeline_unavailable",
                "location": "clause",
                "detail": f"pipeline database not found: {self.path} "
                          "(run a scan first, or build the sample workdir)",
            }])
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        user_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version < 2:
            raise ClauseError([{
                "code": "pipeline_legacy",
                "location": "clause",
                "detail": "pipeline database uses the pre-identity schema; the "
                          "eligibility job reads identity-schema data only and "
                          "never migrates the old database. Run "
                          "'python -m archive_pipeline.cli repair --preview' "
                          "then 'repair --apply' first.",
            }])
        if not self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='versions'"
        ).fetchone():
            raise ClauseError([{
                "code": "pipeline_unavailable",
                "location": "clause",
                "detail": f"pipeline database {self.path} has no versions table",
            }])

    def version(self, source_id: str, archive_id: str, version_no: int) -> dict | None:
        row = self.conn.execute(
            "SELECT source_id, archive_id, notice_key, version_no, title, "
            "issued_on, body, raw_digest FROM versions "
            "WHERE source_id=? AND archive_id=? AND version_no=?",
            (source_id, archive_id, version_no)).fetchone()
        return dict(row) if row else None

    def source_has_versions(self, source_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM versions WHERE source_id=? LIMIT 1",
            (source_id,)).fetchone() is not None

    def archive_versions(self, source_id: str, archive_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT source_id, archive_id, notice_key, version_no, title, "
            "issued_on, body, raw_digest FROM versions "
            "WHERE source_id=? AND archive_id=? ORDER BY version_no",
            (source_id, archive_id))]

    def sibling_versions(self, source_id: str, archive_id: str,
                         notice_key: str) -> list[dict]:
        """Other variants of the same source that publish ``notice_key``."""
        return [dict(row) for row in self.conn.execute(
            "SELECT source_id, archive_id, notice_key, version_no, title, "
            "issued_on, body, raw_digest FROM versions "
            "WHERE source_id=? AND notice_key=? AND archive_id<>? "
            "ORDER BY archive_id, version_no",
            (source_id, notice_key, archive_id))]

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _issue(issues: list[dict], code: str, location: str, detail: str) -> None:
    issues.append({"code": code, "location": location, "detail": detail})


def _unknown_keys(issues, mapping: dict, allowed: set[str], location: str) -> None:
    for key in sorted(set(mapping) - allowed):
        _issue(issues, "unknown_key", location,
               f"unknown key {key!r}; allowed keys: {sorted(allowed)}")


def _validate_scope(raw: Any, name: str, issues: list[dict],
                    location: str) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        _issue(issues, "invalid_scope", location,
               f"scope {name!r} must be a JSON object")
        return None
    _unknown_keys(issues, raw, {"fields", "description"}, location)
    if "fields" not in raw:
        _issue(issues, "missing_key", location, "missing required key 'fields'")
        return None
    fields = raw["fields"]
    if not isinstance(fields, dict):
        _issue(issues, "invalid_scope", f"{location}.fields",
               "fields must be a JSON object of name -> type")
        return None
    normalized: dict[str, str] = {}
    for field_name, field_type in fields.items():
        floc = f"{location}.fields[{field_name!r}]"
        if not isinstance(field_name, str) or not field_name:
            _issue(issues, "invalid_field", floc,
                   "field names must be non-empty strings")
            continue
        if not isinstance(field_type, str) or field_type not in FIELD_TYPES:
            _issue(issues, "invalid_field_type", floc,
                   f"declared type must be one of {list(FIELD_TYPES)}, "
                   f"got {field_type!r}")
            continue
        normalized[field_name] = field_type
    return normalized


def _validate_expr(
    expr: Any, scope: str, scopes: dict[str, dict[str, str]] | None,
    issues: list[dict], base: str, owner: str | None, refs: list[dict],
) -> dict:
    """Validate one expression node; return a normalized AST node.

    ``scopes`` holds the validated field declarations of both scopes so a
    ``count`` predicate can be checked against the *member* declarations even
    though the surrounding expression is subject-scoped.
    """
    fields = (scopes or {}).get(scope, {})
    if not isinstance(expr, dict):
        _issue(issues, "invalid_expression", base,
               "an expression must be a JSON object")
        return {"kind": "invalid", "location": base}
    if not expr:
        _issue(issues, "invalid_expression", base,
               "an expression object may not be empty")
        return {"kind": "invalid", "location": base}
    keys = set(expr)

    if "field" in keys:
        if "op" not in keys:
            _issue(issues, "missing_key", base,
                   "field expressions require 'op'")
            return {"kind": "invalid", "location": base}
        op = expr["op"]
        if not isinstance(op, str) or op not in COMPARISON_OPS | {ENUM_OP}:
            _issue(issues, "invalid_operator", base,
                   f"op must be one of {sorted(COMPARISON_OPS | {ENUM_OP})}, "
                   f"got {op!r}")
            return {"kind": "invalid", "location": base}
        expected = ({"field", "op", "values"} if op == ENUM_OP
                    else {"field", "op", "value"})
        allowed = expected | {"label"}
        _unknown_keys(issues, expr, allowed, base)
        missing = expected - keys
        if missing:
            _issue(issues, "missing_key", base,
                   f"missing required key(s) {sorted(missing)} for op {op!r}")
            return {"kind": "invalid", "location": base}
        field_name = expr["field"]
        if not isinstance(field_name, str) or not field_name:
            _issue(issues, "invalid_field", f"{base}.field",
                   "field must be a non-empty string")
            return {"kind": "invalid", "location": base}
        declared = fields.get(field_name)
        if declared is None:
            _issue(issues, "unknown_field", f"{base}.field",
                   f"field {field_name!r} is not declared in {scope} scope; "
                   f"declared fields: {sorted(fields)}")
            return {"kind": "invalid", "location": base}
        if op in {"gt", "ge", "lt", "le"} and declared not in ("decimal", "date"):
            _issue(issues, "invalid_comparison", base,
                   f"ordered comparison {op!r} is only valid for decimal/date "
                   f"fields; {field_name!r} is {declared}")
            return {"kind": "invalid", "location": base}
        constants: list[Any] = []
        if op == ENUM_OP:
            values = expr["values"]
            if not isinstance(values, list) or not values:
                _issue(issues, "invalid_enum", f"{base}.values",
                       "values must be a non-empty JSON array")
                return {"kind": "invalid", "location": base}
            for index, value in enumerate(values):
                normalized, error = validate_constant(declared, value)
                if error:
                    _issue(issues, "constant_type", f"{base}.values[{index}]",
                           f"field {field_name!r} is {declared}: {error}")
                else:
                    constants.append(normalized)
        else:
            normalized, error = validate_constant(declared, expr["value"])
            if error:
                _issue(issues, "constant_type", f"{base}.value",
                       f"field {field_name!r} is {declared}: {error}")
            else:
                constants.append(normalized)
        return {
            "kind": ENUM_OP if op == ENUM_OP else "compare",
            "scope": scope,
            "field": field_name,
            "declared_type": declared,
            "op": op,
            "constant": expr.get("value") if op != ENUM_OP else None,
            "values": constants if op == ENUM_OP else None,
            "location": base,
            "label": expr.get("label"),
        }

    if "all" in keys or "any" in keys:
        kind = "all" if "all" in keys else "any"
        _unknown_keys(issues, expr, {kind, "label"}, base)
        children_raw = expr[kind]
        if not isinstance(children_raw, list):
            _issue(issues, "invalid_expression", f"{base}.{kind}",
                   f"{kind} requires a JSON array of expressions")
            return {"kind": "invalid", "location": base}
        children = []
        for index, child in enumerate(children_raw):
            children.append(_validate_expr(
                child, scope, scopes, issues, f"{base}.{kind}[{index}]",
                owner, refs))
        return {"kind": kind, "scope": scope, "children": children,
                "location": base, "label": expr.get("label")}

    if "not" in keys:
        _unknown_keys(issues, expr, {"not", "label"}, base)
        child = _validate_expr(expr["not"], scope, scopes, issues,
                               f"{base}.not", owner, refs)
        return {"kind": "not", "scope": scope, "child": child,
                "location": base, "label": expr.get("label")}

    if "ref" in keys:
        _unknown_keys(issues, expr, {"ref", "label"}, base)
        target = expr["ref"]
        if not isinstance(target, str) or not target:
            _issue(issues, "invalid_reference", f"{base}.ref",
                   "ref must be a non-empty condition id string")
            return {"kind": "invalid", "location": base}
        refs.append({"owner": owner, "target": target, "scope": scope,
                     "location": base})
        return {"kind": "ref", "scope": scope, "target": target,
                "location": base, "label": expr.get("label")}

    if "count" in keys:
        _unknown_keys(issues, expr, {"count", "label"}, base)
        count = expr["count"]
        if scope != "subject":
            _issue(issues, "count_scope", base,
                   "count conditions are only valid in subject scope")
            return {"kind": "invalid", "location": base}
        if not isinstance(count, dict):
            _issue(issues, "invalid_count", f"{base}.count",
                   "count must be an JSON object with predicate and at_least")
            return {"kind": "invalid", "location": base}
        _unknown_keys(issues, count, {"predicate", "at_least", "label"},
                      f"{base}.count")
        if "predicate" not in count or "at_least" not in count:
            _issue(issues, "missing_key", f"{base}.count",
                   "count requires 'predicate' and 'at_least'")
            return {"kind": "invalid", "location": base}
        threshold = count["at_least"]
        if type(threshold) is not int or threshold < 0:
            _issue(issues, "invalid_count", f"{base}.count.at_least",
                   f"at_least must be a non-negative JSON integer, "
                   f"got {threshold!r}")
            return {"kind": "invalid", "location": base}
        predicate = _validate_expr(count["predicate"], "member",
                                   scopes, issues,
                                   f"{base}.count.predicate", owner, refs)
        return {"kind": "count", "scope": scope, "at_least": threshold,
                "predicate": predicate, "location": base,
                "label": expr.get("label")}

    _issue(issues, "unknown_expression", base,
           f"unrecognized expression keys {sorted(keys)}; expected one of "
           "field/all/any/not/ref/count (check spelling)")
    return {"kind": "invalid", "location": base}


def _canonical(raw: dict) -> str:
    return json.dumps(raw, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _normalize_citations(conditions_raw: list, issues: list[dict]) -> None:
    """Shape-check citation objects in place (before touching the archive)."""
    for index, cond in enumerate(conditions_raw):
        if not isinstance(cond, dict):
            continue
        base = f"conditions[{index}]"
        citation = cond.get("citation")
        loc = f"{base}.citation"
        if not isinstance(citation, dict):
            _issue(issues, "invalid_citation", loc,
                   "citation must be a JSON object")
            continue
        _unknown_keys(issues, citation, {"start", "end", "quote"}, loc)
        for key in ("start", "end"):
            if key not in citation:
                _issue(issues, "missing_key", loc,
                       f"citation requires {key}")
            elif type(citation[key]) is not int or citation[key] < 0:
                _issue(issues, "invalid_citation", f"{loc}.{key}",
                       f"citation {key} must be a non-negative JSON integer")
        if "quote" not in citation:
            _issue(issues, "missing_key", loc, "citation requires quote")
        elif (not isinstance(citation["quote"], str)
              or not citation["quote"]):
            _issue(issues, "invalid_citation", f"{loc}.quote",
                   "citation quote must be a non-empty string")
        if (type(citation.get("start")) is int
                and type(citation.get("end")) is int
                and citation["start"] >= citation["end"]):
            _issue(issues, "invalid_citation", loc,
                   "citation end must be greater than start (half-open span)")


def _validate_binding(raw: dict, issues: list[dict]) -> tuple | None:
    ok = True
    for key, kind in (("source_id", "str"), ("archive_id", "str")):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            _issue(issues, "invalid_binding", key,
                   f"{key} must be a non-empty string")
            ok = False
    version_no = raw.get("version_no")
    if type(version_no) is not int or version_no < 1:
        _issue(issues, "invalid_binding", "version_no",
               "version_no must be a positive JSON integer")
        ok = False
    notice_key = raw.get("notice_key")
    if notice_key is not None and (not isinstance(notice_key, str)
                                   or not notice_key):
        _issue(issues, "invalid_binding", "notice_key",
               "notice_key, when present, must be a non-empty string")
        ok = False
    return (raw.get("source_id"), raw.get("archive_id"), version_no,
            notice_key) if ok else None


def _verify_citations(clause_raw: dict, binding: tuple, reader: PipelineReader,
                      issues: list[dict]) -> dict | None:
    source_id, archive_id, version_no, notice_key = binding
    row = reader.version(source_id, archive_id, version_no)
    if row is None:
        if not reader.source_has_versions(source_id):
            detail = (f"source {source_id!r} has no archived versions; cannot "
                      f"bind {archive_id} v{version_no}")
        else:
            known = reader.archive_versions(source_id, archive_id)
            if not known:
                detail = (f"archive identity {archive_id!r} does not exist in "
                          f"source {source_id!r}")
            else:
                detail = (f"archive {archive_id} v{version_no} does not exist; "
                          f"available versions: "
                          f"{[v['version_no'] for v in known]}")
        _issue(issues, "unknown_version", "clause", detail)
        return None
    if notice_key is not None and row["notice_key"] != notice_key:
        _issue(issues, "notice_key_mismatch", "notice_key",
               f"clause expects notice_key {notice_key!r} but archive "
               f"{archive_id} v{version_no} publishes {row['notice_key']!r}")
    body = row["body"]
    for index, cond in enumerate(clause_raw["conditions"]):
        citation = cond.get("citation")
        if not isinstance(citation, dict):
            continue
        start, end, quote = (citation.get("start"), citation.get("end"),
                             citation.get("quote"))
        if (type(start) is not int or type(end) is not int
                or not isinstance(quote, str)):
            continue
        if start >= end or end > len(body):
            _issue(issues, "citation_out_of_bounds",
                   f"conditions[{index}].citation",
                   f"span [{start},{end}) is outside the archived body "
                   f"(length {len(body)})")
            continue
        if body[start:end] == quote:
            continue
        occurrences = []
        cursor = body.find(quote)
        while cursor != -1:
            occurrences.append([cursor, cursor + len(quote)])
            cursor = body.find(quote, cursor + 1)
        if occurrences:
            _issue(issues, "citation_mismatch",
                   f"conditions[{index}].citation",
                   f"cited slice does not equal the quote; quote occurs at "
                   f"{occurrences} but the clause says [{start},{end})")
            continue
        siblings = (reader.sibling_versions(source_id, archive_id,
                                            row["notice_key"])
                    if row["notice_key"] else [])
        matches = [s for s in siblings if quote in s["body"]]
        if matches:
            where = ", ".join(
                f"{m['archive_id']} v{m['version_no']}" for m in matches)
            _issue(issues, "citation_in_other_variant",
                   f"conditions[{index}].citation",
                   f"quote is absent from bound variant {archive_id} v{version_no} "
                   f"but appears in another variant of notice "
                   f"{row['notice_key']!r}: {where} — the clause is bound to the "
                   "wrong business variant")
        else:
            _issue(issues, "citation_not_found",
                   f"conditions[{index}].citation",
                   f"quote not found anywhere in bound body {archive_id} "
                   f"v{version_no}: {quote!r}")
    return row


def _detect_cycles(condition_order: list[str], conditions: dict[str, Condition],
                   refs: list[dict], issues: list[dict]) -> None:
    edges: dict[str, list[dict]] = {cid: [] for cid in condition_order}
    for ref in refs:
        owner, target = ref["owner"], ref["target"]
        if owner in conditions and target in conditions:
            edges[owner].append(ref)
    state: dict[str, int] = {cid: 0 for cid in condition_order}  # 0/1/2
    stack: list[str] = []
    seen_cycles: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for ref in edges.get(node, []):
            target = ref["target"]
            if state.get(target, 2) == 1:
                start = stack.index(target)
                cycle = stack[start:] + [target]
                key = tuple(sorted(set(cycle)))
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    _issue(issues, "reference_cycle", ref["location"],
                           "reference cycle among conditions: "
                           + " -> ".join(cycle))
            elif state.get(target, 2) == 0:
                visit(target)
        stack.pop()
        state[node] = 2

    for cid in condition_order:
        if state[cid] == 0:
            visit(cid)


def load_clause(pipeline_db_path: str | Path,
                clause_path: str | Path) -> Clause:
    """Load and fully validate a clause; raises :class:`ClauseError`."""
    clause_path = Path(clause_path)
    try:
        file_bytes = clause_path.read_bytes()
    except OSError as exc:
        raise ClauseError([{
            "code": "clause_file_unreadable", "location": "clause",
            "detail": f"cannot read clause file {clause_path}: {exc}"}])
    try:
        raw = json.loads(file_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClauseError([{
            "code": "clause_invalid_json", "location": "clause",
            "detail": f"clause file is not valid UTF-8 JSON: {exc}"}])
    if not isinstance(raw, dict):
        raise ClauseError([{
            "code": "invalid_clause", "location": "clause",
            "detail": "a clause file must be a JSON object"}])

    issues: list[dict] = []
    allowed = {"format", "source_id", "archive_id", "version_no", "notice_key",
               "title", "scope", "conditions", "root", "description"}
    _unknown_keys(issues, raw, allowed, "clause")
    for key in ("format", "source_id", "archive_id", "version_no",
                "scope", "conditions", "root"):
        if key not in raw:
            _issue(issues, "missing_key", "clause",
                   f"missing required key {key!r}")
    if "format" in raw and raw["format"] != CLAUSE_FORMAT:
        _issue(issues, "invalid_format", "format",
               f"format must be {CLAUSE_FORMAT!r}, got {raw['format']!r}")
    if "title" in raw and (not isinstance(raw["title"], str)):
        _issue(issues, "invalid_clause", "title", "title must be a string")
    if "description" in raw and not isinstance(raw["description"], str):
        _issue(issues, "invalid_clause", "description",
               "description must be a string")

    binding = _validate_binding(raw, issues) if all(
        key in raw for key in ("source_id", "archive_id", "version_no")) else None
    if "notice_key" in raw and binding is None:
        value = raw["notice_key"]
        if not isinstance(value, str) or not value:
            _issue(issues, "invalid_binding", "notice_key",
                   "notice_key, when present, must be a non-empty string")

    scopes: dict[str, dict[str, str]] = {}
    scope_raw = raw.get("scope")
    if "scope" in raw:
        if not isinstance(scope_raw, dict):
            _issue(issues, "invalid_scope", "scope",
                   "scope must be a JSON object with subject and member")
        else:
            _unknown_keys(issues, scope_raw, set(SCOPES), "scope")
            for name in SCOPES:
                if name not in scope_raw:
                    _issue(issues, "missing_key", "scope",
                           f"scope requires {name!r}")
                    continue
                validated = _validate_scope(scope_raw[name], name, issues,
                                            f"scope.{name}")
                if validated is not None:
                    scopes[name] = validated

    conditions_raw = raw.get("conditions")
    conditions: dict[str, Condition] = {}
    order: list[str] = []
    refs: list[dict] = []
    if "conditions" in raw:
        if not isinstance(conditions_raw, list) or not conditions_raw:
            _issue(issues, "invalid_conditions", "conditions",
                   "conditions must be a non-empty JSON array")
            conditions_raw = []
        for index, cond in enumerate(conditions_raw):
            base = f"conditions[{index}]"
            if not isinstance(cond, dict):
                _issue(issues, "invalid_condition", base,
                       "each condition must be a JSON object")
                continue
            _unknown_keys(issues, cond, {"id", "scope", "citation", "expr",
                                         "description", "title"}, base)
            cid = cond.get("id")
            if not isinstance(cid, str) or not cid:
                _issue(issues, "invalid_condition", f"{base}.id",
                       "condition id must be a non-empty string")
                continue
            if cid in conditions:
                _issue(issues, "duplicate_condition", f"{base}.id",
                       f"condition id {cid!r} is used more than once")
                continue
            scope = cond.get("scope")
            if not isinstance(scope, str) or scope not in SCOPES:
                _issue(issues, "invalid_scope", f"{base}.scope",
                       f"condition scope must be one of {list(SCOPES)}, "
                       f"got {scope!r}")
                continue
            if "citation" not in cond:
                _issue(issues, "missing_key", base, "condition requires citation")
            if "expr" not in cond:
                _issue(issues, "missing_key", base, "condition requires expr")
            if "description" in cond and not isinstance(cond["description"], str):
                _issue(issues, "invalid_condition", f"{base}.description",
                       "description must be a string")
            expr = _validate_expr(
                cond.get("expr"), scope, scopes, issues,
                f"{base}.expr", cid, refs)
            citation = (cond.get("citation") if isinstance(cond.get("citation"), dict)
                        else {"start": None, "end": None, "quote": None})
            conditions[cid] = Condition(
                id=cid, scope=scope, citation=citation, expr=expr, index=index,
                description=cond.get("description"))
            order.append(cid)

    if isinstance(conditions_raw, list):
        _normalize_citations(conditions_raw, issues)

    # Reference resolution (forward references are allowed).
    for ref in refs:
        target = ref["target"]
        if ref["owner"] not in conditions:
            continue
        if target not in conditions:
            _issue(issues, "undefined_reference", ref["location"],
                   f"ref {target!r} does not name a defined condition")
            continue
        if conditions[target].scope != ref["scope"]:
            _issue(issues, "scope_mismatch", ref["location"],
                   f"ref {target!r} has scope {conditions[target].scope!r} but "
                   f"is used in {ref['scope']!r} scope; references may only "
                   "cross scopes through a count predicate")
    _detect_cycles(order, conditions, refs, issues)

    root = raw.get("root")
    if "root" in raw:
        if not isinstance(root, str) or not root:
            _issue(issues, "invalid_root", "root",
                   "root must be a non-empty condition id string")
            root = None
        elif root not in conditions:
            _issue(issues, "undefined_reference", "root",
                   f"root {root!r} does not name a defined condition")
            root = None
        elif conditions[root].scope != "subject":
            _issue(issues, "root_scope", "root",
                   f"root condition {root!r} must be subject-scoped to decide "
                   "the whole application")
            root = None

    if issues:
        raise ClauseError(issues)

    reader = PipelineReader(pipeline_db_path)
    try:
        row = _verify_citations(raw, binding, reader, issues)
    finally:
        reader.close()
    if row is None or issues:
        if not issues:
            _issue(issues, "unknown_version", "clause",
                   "binding could not be resolved")
        raise ClauseError(issues)

    canonical = _canonical(raw)
    clause = Clause(
        path=clause_path,
        file_sha256=hashlib.sha256(file_bytes).hexdigest(),
        canonical_json=canonical,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        binding=ClauseBinding(
            source_id=binding[0], archive_id=binding[1],
            version_no=binding[2], notice_key=row["notice_key"],
            title=row["title"], issued_on=row["issued_on"],
            body_sha256=hashlib.sha256(row["body"].encode("utf-8")).hexdigest(),
            raw_digest=row["raw_digest"]),
        scopes=scopes,
        conditions=conditions,
        condition_order=order,
        root=root,
    )
    return clause

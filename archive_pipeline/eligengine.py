"""Three-valued eligibility evaluation with complete, auditable explanations.

Fixed truth rules (no short-circuiting):

* ``all`` is false if any child is false, true if every child is true,
  otherwise unknown.  An empty ``all`` is true.
* ``any`` is true if any child is true, false if every child is false,
  otherwise unknown.  An empty ``any`` is false.
* ``not`` maps true→false, false→true and preserves unknown.
* Comparing a missing/null/wrong-typed value is unknown, never true.
* Member counts first evaluate the predicate for every available member:
  ``t`` members are true, ``u`` unknown, ``f`` false.  When the member list
  is declared complete the count lies in ``[t, t+u]``; when it is not,
  unlisted members could only add true counts, so the count is ``[t, ∞)``.
  Lower bound reaching ``k`` is true, an upper bound below ``k`` is false,
  everything else is unknown.  ``k = 0`` is always true.

Every expression node is evaluated and described — conditions that are
already false do not hide the reasons of unknown siblings.  Each leaf records
the declared field, the actual value (or the explicit unknown reason), the
JSON Pointer into the application document, and its clause location; each
named condition carries the announcement citation that the clause loader
verified against the archived body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .eligclause import Clause
from .eligvalue import (
    UNKNOWN_MISSING,
    UNKNOWN_NULL,
    UNKNOWN_TYPE,
    compare,
    read_field,
)

TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"
RESULTS = (TRUE, FALSE, UNKNOWN)

VERDICT_LABELS = {TRUE: "满足", FALSE: "不满足", UNKNOWN: "待补证"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pointer_escape(token: str) -> str:
    """RFC 6901 JSON Pointer token escaping."""
    return token.replace("~", "~0").replace("/", "~1")


def json_pointer(*tokens: str | int) -> str:
    return "".join("/" + pointer_escape(str(token)) for token in tokens)


def _serialize_value(value: Any) -> Any:
    """Typed values stay JSON-friendly: Decimal becomes its exact string."""
    if hasattr(value, "as_tuple") and not isinstance(value, (int, float)):
        return format(value, "f")
    return value


def _rule_result(children: list[str], kind: str) -> tuple[str, str]:
    if kind == "all":
        if not children:
            return TRUE, "all: 空的 all 为真"
        if FALSE in children:
            return FALSE, "all: 存在假分支 → 假（假优先于未知）"
        if UNKNOWN in children:
            return UNKNOWN, "all: 无假分支但存在未知 → 待补证"
        return TRUE, "all: 全部分支为真 → 真"
    if not children:
        return FALSE, "any: 空的 any 为假"
    if TRUE in children:
        return TRUE, "any: 存在真分支 → 真（真优先于未知）"
    if UNKNOWN in children:
        return UNKNOWN, "any: 无真分支但存在未知 → 待补证"
    return FALSE, "any: 全部分支为假 → 假"
    # pragma: no cover


class ApplicationStructureError(ValueError):
    """The application line is structurally unusable (member structure)."""

    def __init__(self, code: str, detail: str, line_no: int):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.line_no = line_no


@dataclass
class ApplicationDocument:
    application_id: str
    subject: dict
    items: list[dict]
    complete: bool
    line_no: int
    raw: dict = field(repr=False, default_factory=dict)


def validate_application(raw: Any, line_no: int) -> ApplicationDocument:
    """Validate one JSONL record's *structure* only.

    Declared field values are deliberately NOT checked here: a missing key,
    ``null`` or a wrong-typed value is a per-field unknown, while damaged
    application/member structure is a file-level failure.
    """
    if not isinstance(raw, dict):
        raise ApplicationStructureError(
            "application_not_object",
            f"line {line_no}: the JSON record must be an object", line_no)
    for key in ("application_id", "subject", "items", "complete"):
        if key not in raw:
            raise ApplicationStructureError(
                "missing_structure_key",
                f"line {line_no}: missing required key {key!r}", line_no)
    application_id = raw["application_id"]
    if not isinstance(application_id, str) or not application_id.strip():
        raise ApplicationStructureError(
            "invalid_application_id",
            f"line {line_no}: application_id must be a non-empty string", line_no)
    subject = raw["subject"]
    if not isinstance(subject, dict):
        raise ApplicationStructureError(
            "invalid_subject",
            f"line {line_no}: subject must be a JSON object", line_no)
    items = raw["items"]
    if not isinstance(items, list):
        raise ApplicationStructureError(
            "invalid_items",
            f"line {line_no}: items must be a JSON array "
            "(use [] with complete=true for an explicitly empty team)", line_no)
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApplicationStructureError(
                "invalid_item",
                f"line {line_no}: items[{index}] must be a JSON object", line_no)
    complete = raw["complete"]
    if type(complete) is not bool:
        raise ApplicationStructureError(
            "invalid_complete",
            f"line {line_no}: complete must be a JSON boolean", line_no)
    return ApplicationDocument(application_id=application_id, subject=subject,
                               items=items, complete=complete, line_no=line_no,
                               raw=raw)


class Evaluator:
    def __init__(self, clause: Clause):
        self.clause = clause
        self._subject_cache: dict[str, dict] = {}
        self._member_cache: dict[tuple[str, int], dict] = {}
        self._subject_registry: dict[str, dict] = {}
        self._member_registry: dict[tuple[str, int], dict] = {}
        self._member_count: int = 0

    # -- public ------------------------------------------------------------
    def evaluate(self, application: ApplicationDocument) -> dict:
        self._application = application
        self._subject_cache = {}
        self._member_cache = {}
        self._subject_registry = {}
        self._member_registry = {}
        self._member_count = len(application.items)
        root_eval = self._condition(self.clause.root, None)
        # Evaluate every subject-scoped condition as well, so the explanation
        # covers all named branches even when the root never references one.
        for cid in self.clause.condition_order:
            if self.clause.conditions[cid].scope == "subject":
                self._condition(cid, None)
        # Materialize every named condition that was actually evaluated once;
        # ref nodes point here instead of embedding a copy of the target.
        subject_nodes = [self._subject_registry[cid]
                         for cid in self.clause.condition_order
                         if cid in self._subject_registry]
        member_nodes = []
        for index in range(self._member_count):
            for cid in self.clause.condition_order:
                node = self._member_registry.get((cid, index))
                if node is not None:
                    member_nodes.append(node)
        condition_index = [self._condition_index_entry(cid)
                           for cid in self.clause.condition_order]
        return {
            "application_id": application.application_id,
            "line_no": application.line_no,
            "verdict": root_eval["result"],
            "verdict_label": VERDICT_LABELS[root_eval["result"]],
            "root_condition": self.clause.root,
            "clause_digest": self.clause.digest,
            "clause_binding": {
                "source_id": self.clause.binding.source_id,
                "archive_id": self.clause.binding.archive_id,
                "version_no": self.clause.binding.version_no,
                "notice_key": self.clause.binding.notice_key,
                "title": self.clause.binding.title,
                "issued_on": self.clause.binding.issued_on,
                "body_sha256": self.clause.binding.body_sha256,
            },
            "input": {
                "subject_pointer": "/subject",
                "items_pointer": "/items",
                "member_count": len(application.items),
                "members_complete": application.complete,
            },
            "input_document": dict(application.raw),
            # One verified citation per condition (bound to the clause digest
            # and body digest).
            "citations": [self._citation(cid)
                          for cid in self.clause.condition_order],
            "clause_binding_pointer": "/clause_binding",
            "explanation": root_eval,
            "condition_nodes": {
                "pointer": "/condition_nodes",
                "subject": subject_nodes,
                "members": member_nodes,
            },
            "conditions": condition_index,
            "unknown_values": self._collect_unknowns(
                [root_eval] + subject_nodes + member_nodes),
            "false_conditions": [
                {"condition_id": item["condition_id"],
                 "location": item["location"], "result": item["result"]}
                for item in condition_index if item["result"] == FALSE
            ],
        }

    def _condition_index_entry(self, cid: str) -> dict:
        condition = self.clause.conditions[cid]
        if condition.scope == "subject":
            node = self._subject_registry.get(cid)
            pointer = f"/condition_nodes/subject/{cid}"
            result = node["result"] if node else None
        else:
            node = None
            pointer = "/condition_nodes/members"
            result = None
        return {
            "condition_id": cid,
            "scope": condition.scope,
            "location": f"conditions[{condition.index}]",
            "description": condition.description,
            "citation_pointer": f"/citations/{condition.index}",
            "condition_pointer": pointer,
            "result": result,
            "note": (None if condition.scope == "subject"
                     else "成员作用域条件按每名成员在 count 谓词内求值；"
                          "逐人结果见成员计数节点"),
        }

    # -- condition layer ---------------------------------------------------
    def _citation(self, cid: str) -> dict:
        condition = self.clause.conditions[cid]
        citation = condition.citation
        return {
            "condition_id": cid,
            "location": f"conditions[{condition.index}].citation",
            "explanation_pointer": f"/citations/{condition.index}",
            "source_id": self.clause.binding.source_id,
            "archive_id": self.clause.binding.archive_id,
            "version_no": self.clause.binding.version_no,
            "notice_key": self.clause.binding.notice_key,
            "issued_on": self.clause.binding.issued_on,
            "body_sha256": self.clause.binding.body_sha256,
            "start": citation.get("start"),
            "end": citation.get("end"),
            "quote": citation.get("quote"),
            "verified": "body[start:end] == quote",
        }

    def _condition(self, cid: str, member_index: int | None) -> dict:
        cache = (self._subject_cache if member_index is None
                 else self._member_cache)
        key = cid if member_index is None else (cid, member_index)
        if key in cache:
            return cache[key]
        condition = self.clause.conditions[cid]
        tree = self._expr(condition.expr, member_index, cid,
                          f"conditions[{condition.index}].expr")
        node = {
            "condition_id": cid,
            "scope": condition.scope,
            "location": f"conditions[{condition.index}]",
            "description": condition.description,
            "citation_pointer": f"/citations/{condition.index}",
            "member_index": member_index,
            "result": tree["result"],
            "tree": tree,
        }
        if member_index is None:
            self._subject_registry[cid] = node
        else:
            self._member_registry[(cid, member_index)] = node
        cache[key] = node
        return node

    def _expr(self, node: dict, member_index: int | None, owner: str,
              base: str) -> dict:
        kind = node["kind"]
        if kind in ("compare", "in"):
            return self._leaf(node, member_index, owner, base)
        if kind in ("all", "any"):
            children = [self._expr(child, member_index, owner,
                                   f"{base}.{kind}[{i}]")
                        for i, child in enumerate(node["children"])]
            result, reason = _rule_result([c["result"] for c in children], kind)
            return {
                "node": kind, "condition_id": owner, "location": base,
                "result": result, "reason": reason, "children": children,
                "counts": _result_counts(children),
            }
        if kind == "not":
            child = self._expr(node["child"], member_index, owner,
                               f"{base}.not")
            if child["result"] == TRUE:
                result = FALSE
            elif child["result"] == FALSE:
                result = TRUE
            else:
                result = UNKNOWN
            return {
                "node": "not", "condition_id": owner, "location": base,
                "result": result,
                "reason": f"not: 子节点为 {child['result']} → {result}"
                          "（未知保留）",
                "child": child,
            }
        if kind == "ref":
            target = node["target"]
            resolved = self._condition(target, member_index)
            return {
                "node": "ref", "condition_id": owner, "location": base,
                "target": target, "target_scope": resolved["scope"],
                "condition_pointer": (
                    f"/condition_nodes/subject/{target}"
                    if resolved["scope"] == "subject"
                    else f"/condition_nodes/members[{target},"
                         f"{member_index}]"),
                "result": resolved["result"],
                "reason": f"引用具名条件 {target}（{resolved['scope']} 作用域）"
                          f"→ {resolved['result']}",
            }
        if kind == "count":
            return self._count(node, member_index, owner, base)
        return {
            "node": "invalid", "condition_id": owner, "location": base,
            "result": UNKNOWN,
            "reason": "条款节点无效（应在条款校验阶段被拒绝）",
        }

    # -- leaves ------------------------------------------------------------
    def _leaf(self, node: dict, member_index: int | None, owner: str,
              base: str) -> dict:
        scope = node["scope"]
        field_name = node["field"]
        if member_index is None:
            container = self._application.subject
        else:
            container = self._application.items[member_index]
        read = read_field(container, field_name, node["declared_type"], True)
        document_path = (json_pointer("subject", field_name)
                         if member_index is None
                         else json_pointer("items", member_index, field_name))
        common = {
            "node": node["kind"], "condition_id": owner, "location": base,
            "scope": scope, "field": field_name,
            "declared_type": node["declared_type"],
            "json_pointer": document_path,
            "member_index": member_index,
            "input": {
                "presence": read.presence,
                "raw_value": read.raw_value,
                "observed_type": read.observed_type,
                "typed_value": _serialize_value(read.value) if read.known else None,
            },
        }
        if node["kind"] == "in":
            common["operator"] = "in"
            common["allowed_values"] = node["values"]
        else:
            common["operator"] = node["op"]
            common["constant"] = node.get("constant")
        if not read.known:
            common.update({
                "result": UNKNOWN,
                "unknown": {
                    "reason": read.unknown_reason,
                    "detail": _unknown_detail(read),
                },
                "reason": f"{field_name}: {_unknown_detail(read)} → 未知（待补证）",
            })
            return common
        if node["kind"] == "in":
            # Enum membership compares typed constants, so a decimal field
            # matches "3.0" exactly as Decimal("3.0") (and 3.00 equals 3).
            satisfied = any(
                compare(read.value, constant, node["declared_type"], "eq")
                for constant in node["values"])
        else:
            satisfied = compare(read.value, node.get("constant"),
                                node["declared_type"], node["op"])
            result = satisfied
        common["result"] = TRUE if satisfied else FALSE
        expected = (f"属于 {node['values']}" if node["kind"] == "in"
                    else f"{node['op']} {node.get('constant')}")
        common["reason"] = (
            f"{field_name} = {_serialize_value(read.value)!r}（{read.declared_type}）"
            f" {expected} → {common['result']}")
        return common

    # -- member count ------------------------------------------------------
    def _count(self, node: dict, member_index: int | None, owner: str,
               base: str) -> dict:
        if member_index is not None:
            return {"node": "count", "condition_id": owner, "location": base,
                    "result": UNKNOWN,
                    "reason": "count 只能出现在主体作用域"}
        members: list[dict] = []
        for index, item in enumerate(self._application.items):
            tree = self._expr(node["predicate"], index, owner,
                              f"{base}.count.predicate")
            members.append({
                "member_index": index,
                "json_pointer": json_pointer("items", index),
                "result": tree["result"],
                "predicate": tree,
            })
        t = sum(1 for m in members if m["result"] == TRUE)
        u = sum(1 for m in members if m["result"] == UNKNOWN)
        f_count = sum(1 for m in members if m["result"] == FALSE)
        complete = self._application.complete
        lower = t
        upper = t + u if complete else None
        k = node["at_least"]
        if k == 0:
            result = TRUE
            reason = "至少 0 人满足：阈值 0 恒为满足"
        elif lower >= k:
            result = TRUE
            reason = (f"已知满足 {t} 人（下界 {lower} ≥ {k}）→ 满足"
                      + ("" if complete else "；名单未列全也不影响，未列成员只会增加"))
        elif upper is not None and upper < k:
            result = FALSE
            reason = f"人数上界 {upper} < {k} → 不满足"
        else:
            result = UNKNOWN
            bound = f"[{lower}, ∞)" if upper is None else f"[{lower}, {upper}]"
            reason = (f"人数区间 {bound}，阈值 {k} 未被确定："
                      "下界不足且上界（或未知上界）可能达到 → 待补证")
        interval = {"lower": lower,
                    "upper": upper,
                    "notation": (f"[{lower}, ∞)" if upper is None
                                 else f"[{lower}, {upper}]"),
                    "members_complete": complete}
        return {
            "node": "count", "condition_id": owner, "location": base,
            "at_least": k,
            "predicate_location": f"{base}.count.predicate",
            "members_complete": complete,
            "total_members": len(members),
            "member_counts": {"true": t, "false": f_count, "unknown": u},
            "possible_interval": interval,
            "members": members,
            "result": result,
            "reason": reason,
        }

    # -- aggregates --------------------------------------------------------
    def _collect_unknowns(self, roots: list[dict]) -> list[dict]:
        found: list[dict] = []
        seen: set[tuple] = set()

        def walk(node: dict) -> None:
            if node.get("node") in ("compare", "in") and node.get("unknown"):
                key = (node["condition_id"], node["json_pointer"],
                       node["unknown"]["reason"])
                if key not in seen:
                    seen.add(key)
                    found.append({
                        "condition_id": node["condition_id"],
                        "location": node["location"],
                        "field": node["field"],
                        "scope": node["scope"],
                        "member_index": node.get("member_index"),
                        "json_pointer": node["json_pointer"],
                        "reason": node["unknown"]["reason"],
                        "detail": node["unknown"]["detail"],
                    })
            for child in node.get("children", []) or []:
                walk(child)
            if "child" in node:
                walk(node["child"])
            if node.get("node") == "count":
                for member in node["members"]:
                    walk(member["predicate"])
            if "resolved" in node:
                walk(node["resolved"]["tree"])
            if "tree" in node:
                walk(node["tree"])

        for root in roots:
            walk(root)
        return found


def merge_ref_expansions(tree: dict, registry_subject: dict,
                         registry_members: list) -> dict:
    """Return a copy of ``tree`` where ref nodes carry the target's subtree.

    Used only for human rendering; the stored result keeps references so one
    condition is never persisted twice.
    """
    import copy
    node = copy.deepcopy(tree)
    member_lookup = {(n["condition_id"], n["member_index"]): n
                     for n in registry_members}

    def visit(item: dict) -> None:
        if item.get("node") == "ref":
            target = item["target"]
            if item.get("target_scope") == "subject":
                resolved = registry_subject.get(target)
            else:
                resolved = member_lookup.get(
                    (target, item.get("member_index")))
            if resolved is not None:
                item["resolved"] = resolved
        for child in item.get("children", []) or []:
            visit(child)
        if "child" in item:
            visit(item["child"])
        if item.get("node") == "count":
            for member in item["members"]:
                visit(member["predicate"])

    visit(node)
    return node


def explain_result(explanation: dict) -> dict:
    """Return a fully expanded copy of the stored result (for reading only).

    Ref nodes gain a ``resolved`` member (the referenced condition node),
    matching the pre-dedup shape, so downstream readers can navigate every
    branch uniformly.  The stored result itself keeps references.
    """
    registry = explanation["condition_nodes"]
    subject = {n["condition_id"]: n for n in registry["subject"]}
    merged = merge_ref_expansions(explanation["explanation"]["tree"], subject,
                                  registry["members"])
    return {"conditions": subject, "members": registry["members"],
            "tree": merged}


def render_result(explanation: dict) -> list[str]:
    """Render a stored result with refs expanded (human-readable only)."""
    return render_tree(explain_result(explanation)["tree"])


def _unknown_detail(read) -> str:
    if read.unknown_reason == UNKNOWN_MISSING:
        return "字段缺失（missing_field）"
    if read.unknown_reason == UNKNOWN_NULL:
        return "字段为 null（null_value）"
    if read.unknown_reason == UNKNOWN_TYPE:
        return f"实值类型不符（type_mismatch：{read.type_detail}）"
    return str(read.unknown_reason)


def _result_counts(nodes: list[dict]) -> dict:
    return {TRUE: sum(1 for n in nodes if n["result"] == TRUE),
            FALSE: sum(1 for n in nodes if n["result"] == FALSE),
            UNKNOWN: sum(1 for n in nodes if n["result"] == UNKNOWN)}


def render_tree(node: dict, indent: int = 0) -> list[str]:
    """A compact human-readable rendering of one explanation tree."""
    prefix = "  " * indent
    label = node.get("result")
    lines = [f"{prefix}{node.get('node')} [{label}] {node.get('location')}"]
    if node.get("node") in ("compare", "in"):
        value = node.get("input", {}).get("raw_value")
        shown = json_dumps(value) if node.get("input", {}).get("presence") == "present" else "（缺失）"
        detail = node.get("reason", "")
        lines.append(f"{prefix}  · {node.get('field')} @ {node.get('json_pointer')}"
                     f" 实值={shown}  {detail}")
    elif node.get("node") == "count":
        lines.append(f"{prefix}  · 阈值={node.get('at_least')} 人数区间="
                     f"{node['possible_interval']['notation']} 已知真/假/未知="
                     f"{node['member_counts']['true']}/"
                     f"{node['member_counts']['false']}/"
                     f"{node['member_counts']['unknown']}")
        for member in node["members"]:
            lines.append(f"{prefix}  · 成员[{member['member_index']}] "
                         f"({member['json_pointer']}) → {member['result']}")
            lines.extend(render_tree(member["predicate"], indent + 2))
    elif node.get("node") == "ref":
        lines.append(f"{prefix}  · → 条件 {node.get('target')} "
                     f"（展开见 {node.get('condition_pointer')}）")
        if "resolved" in node:
            lines.extend(render_tree(node["resolved"]["tree"], indent + 1))
    if "children" in node:
        for child in node["children"]:
            lines.extend(render_tree(child, indent + 1))
    if "child" in node:
        lines.extend(render_tree(node["child"], indent + 1))
    if node.get("reason") and node.get("node") in ("all", "any", "not"):
        lines.append(f"{prefix}  · {node['reason']}")
    return lines


def json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, default=str)

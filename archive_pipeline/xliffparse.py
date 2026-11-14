"""Namespaced XLIFF 1.2 parsing, inline-code expansion and code-tree checks.

The intake job only accepts a deliberately small XLIFF subset:

* root element ``xliff`` in the namespace ``urn:oasis:names:tc:xliff:document:1.2``
  (a different prefix is fine — the namespace URI is what matters);
* one or more ``file`` elements, each with ``original``, ``source-language``
  and ``target-language``;
* a single ``body`` per file, possibly containing nested ``group`` elements;
* ``trans-unit`` elements with exactly one ``source`` and one ``target``;
* inline content limited to plain text, paired ``g`` elements and empty ``x``
  elements carrying ``equiv-text``.

Everything else (``ph``, ``bpt``, ``ept``, ``it``, ``mrk``, ``sub`` …) is
reported as *unsupported*, never silently stripped.  DTDs and external
entities are refused before any content is read: a ``StartDoctypeDeclHandler``
that always raises and an ``ExternalEntityRefHandler`` that returns 0 make it
impossible for the parser to expand a file or network entity.

Character positions
-------------------
Text is decoded by expat into Python ``str``; offsets in the JSON binding map
are Python string indices on the archived ``body`` (half-open), **not** UTF-8
byte offsets.  Every Chinese character above U+007F is one index, so
``body[start:end]`` is the exact decoded slice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from xml.parsers import expat

XLIF_NAMESPACE = "urn:oasis:names:tc:xliff:document:1.2"
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"

SUPPORTED_INLINE = {"g", "x"}
KNOWN_INLINE = SUPPORTED_INLINE | {
    "ph", "bpt", "ept", "it", "sub", "mrk", "bin", "bin-unit", "img",
}

LANG_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


class XliffSyntaxError(Exception):
    """The document is not well-formed XML, or a forbidden construct appears."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def normalize_language(tag: str | None) -> str | None:
    """Return the lower-cased language tag, or None if the shape is invalid.

    Accepted shape: a first segment of 2–8 ASCII letters, then zero or more
    segments of 1–8 ASCII letters/digits, separated by a single hyphen.  Empty
    segments are rejected.  Case is not significant; storage is lower-case, so
    ``en-US`` and ``en-GB`` stay distinct while ``ZH-cn`` and ``zh-CN`` merge.
    """
    if not isinstance(tag, str):
        return None
    if not LANG_TAG.match(tag):
        return None
    return tag.lower()


@dataclass
class Element:
    """One parsed XML element (namespace URI kept separately from the prefix)."""

    ns: str | None
    name: str
    attrs: dict[str, str]
    line: int
    column: int
    children: list[Any] = field(default_factory=list)  # Element | str

    @property
    def qname(self) -> str:
        return f"{{{self.ns}}}{self.name}" if self.ns else self.name

    def elements(self):
        for child in self.children:
            if isinstance(child, Element):
                yield child

    def iter_elements(self):
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.iter_elements()

    def find_all(self, name: str) -> list["Element"]:
        return [el for el in self.iter_elements()
                if el.ns == XLIF_NAMESPACE and el.name == name]

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                parts.append(child.text())
        return "".join(parts)


def _split(name: str) -> tuple[str | None, str]:
    if " " in name:
        ns, local = name.split(" ", 1)
        return ns, local
    return None, name


def _split_attr(name: str) -> tuple[str | None, str]:
    if " " in name[1:] and name[0] != " ":
        ns, local = name.split(" ", 1)
        return ns, local
    return None, name


def parse_xliff(raw: bytes) -> Element:
    """Parse one XLIFF 1.2 document into an :class:`Element` tree.

    Raises :class:`XliffSyntaxError` for malformed XML, a DOCTYPE, an
    external entity reference, or a root element outside the XLIFF namespace.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("parse_xliff expects bytes")
    parser = expat.ParserCreate(namespace_separator=" ")
    root_holder: dict[str, Element | None] = {"root": None, "terminal": None}
    stack: list[Element] = []
    pending_text: list[str] = []
    unclosed: list[Element] = []

    def flush_text() -> None:
        if not pending_text or not stack:
            pending_text.clear()
            return
        text = "".join(pending_text)
        pending_text.clear()
        if text:
            stack[-1].children.append(text)

    def start(name: str, attrs: dict[str, str]) -> None:
        flush_text()
        before = parser.CurrentLineNumber
        ns, local = _split(name)
        element = Element(ns=ns, name=local,
                          attrs={k: v for k, v in attrs.items()},
                          line=before,
                          column=parser.CurrentColumnNumber)
        if stack:
            stack[-1].children.append(element)
        else:
            root_holder["root"] = element
        stack.append(element)

    def end(_name: str) -> None:
        flush_text()
        if stack:
            unclosed.append(stack.pop())

    def chars(data: str) -> None:
        pending_text.append(data)

    def doctype(*_args) -> None:
        raise XliffSyntaxError(
            "doctype_refused",
            "DTDs are not accepted (internal or external); no entity may be "
            "declared or loaded")

    def external_entity(context, base, system_id, public_id):
        root_holder["terminal"] = system_id
        return 0

    def skipped_entity(name, is_parameter_entity):
        raise XliffSyntaxError(
            "external_entity_refused",
            f"entity {name!r} would be expanded from an external "
            "resource; external entities are not accepted")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = doctype
    parser.ExternalEntityRefHandler = external_entity
    parser.SkippedEntityHandler = skipped_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(bytes(raw), True)
    except XliffSyntaxError:
        raise
    except expat.ExpatError as exc:
        raise XliffSyntaxError(
            "malformed_xml", f"XML syntax error: {exc}") from exc
    flush_text()
    if stack:
        raise XliffSyntaxError(
            "malformed_xml", "XML document ended with unclosed elements")
    if root_holder["terminal"] is not None:
        raise XliffSyntaxError(
            "external_entity_refused",
            f"external entity system id {root_holder['terminal']!r} was not loaded")
    root = root_holder["root"]
    if root is None:
        raise XliffSyntaxError("malformed_xml", "document has no root element")
    if root.ns != XLIF_NAMESPACE or root.name != "xliff":
        raise XliffSyntaxError(
            "wrong_root",
            "root element must be {urn:oasis:names:tc:xliff:document:1.2}xliff, "
            f"got {root.qname!r}")
    return root


# ---------------------------------------------------------------------------
# Inline expansion and code trees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Code:
    """One inline code occurrence: a paired ``g`` or an empty ``x``."""

    kind: str            # "g" or "x"
    code_id: str
    equiv_text: str | None
    parent_path: tuple[int, ...]   # indices of enclosing codes, root = ()
    depth: int

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.code_id,
                "equiv_text": self.equiv_text,
                "parent_path": list(self.parent_path),
                "depth": self.depth}


@dataclass
class Expanded:
    """A source/target expansion plus the structural facts needed to compare."""

    text: str
    codes: list[Code]
    events: tuple = ()
    problems: list[dict] = field(default_factory=list)

    @property
    def code_ids(self) -> list[str]:
        return [c.code_id for c in self.codes]


class InlineError(Exception):
    def __init__(self, code: str, message: str, path: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def _code_id(attrs: dict[str, str], path: str) -> str:
    value = attrs.get("id")
    if value is None or value == "":
        raise InlineError("missing_code_id",
                          "g/x elements must carry a non-empty id", path)
    return value


def _equiv_text(attrs: dict[str, str], path: str) -> str:
    value = attrs.get("equiv-text")
    if value is None:
        raise InlineError("missing_equiv_text",
                          "x elements must carry equiv-text", path)
    return value


def expand(node: Element, *, side: str,
           allowed: set[str] = SUPPORTED_INLINE) -> Expanded:
    """Recursively expand ``g``/``x`` in one source or target element.

    Text and tail text are kept in document order; ``x`` is replaced by its
    ``equiv-text``; ``g`` contributes its children only (the wrapper itself is
    not text).  Whitespace is never collapsed or trimmed.  Any element other
    than the allowed inline set raises :class:`InlineError` with
    ``code="unsupported_inline"`` — it is never stripped and silently skipped.
    """
    pieces: list[str] = []
    codes: list[Code] = []
    events: list[tuple] = []
    seen_ids: dict[str, str] = {}

    def where(path_names: tuple[str, ...], name: str) -> str:
        return "/" + "/".join(path_names + (name,))

    def walk(el: Element, path: tuple[int, ...],
             path_names: tuple[str, ...]) -> None:
        for child in el.children:
            if isinstance(child, str):
                if child:
                    pieces.append(child)
                    events.append(("text", child))
                continue
            qname = child.qname
            location = where(path_names, child.name)
            if child.ns != XLIF_NAMESPACE or child.name not in allowed:
                raise InlineError(
                    "unsupported_inline",
                    f"unsupported inline element {qname!r}; only plain text, "
                    "paired g and empty x are accepted",
                    where(path_names, el.name))
            if child.name == "x":
                if child.children:
                    raise InlineError(
                        "x_not_empty", "x must be an empty element", location)
                cid = _code_id(child.attrs, location)
                equiv = _equiv_text(child.attrs, location)
                if cid in seen_ids:
                    raise InlineError(
                        "duplicate_code_id",
                        f"code id {cid!r} appears more than once in this "
                        f"{side} (also at {seen_ids[cid]})", location)
                seen_ids[cid] = location
                index = len(codes)
                codes.append(Code("x", cid, equiv, path, len(path)))
                pieces.append(equiv)
                events.append(("x", cid, equiv))
                continue
            # g: paired
            cid = _code_id(child.attrs, location)
            if cid in seen_ids:
                raise InlineError(
                    "duplicate_code_id",
                    f"code id {cid!r} appears more than once in this {side} "
                    f"(also at {seen_ids[cid]})", location)
            seen_ids[cid] = location
            index = len(codes)
            codes.append(Code("g", cid, None, path, len(path)))
            events.append(("g_open", cid))
            walk(child, path + (index,), path_names + (child.name,))
            events.append(("g_close", cid))

    walk(node, (), (node.name,))
    return Expanded(text="".join(pieces), codes=codes, events=tuple(events))


@dataclass
class Branch:
    """A fine-grained code-tree node used for structural comparison."""

    kind: str                # "g" (container) or "x" (leaf)
    code_id: str
    equiv_text: str | None
    children: list["Branch"] = field(default_factory=list)


def build_branches(expanded: Expanded) -> list[Branch]:
    """Rebuild the nesting forest from the flat code list.

    ``g`` entries record the parent path they were declared under; the
    comparison below uses this forest so that moving an inner placeholder to a
    different level is a structural difference, not a reorder.
    """
    roots: list[Branch] = []
    by_index: dict[int, Branch] = {}
    for index, code in enumerate(expanded.codes):
        branch = Branch(code.kind, code.code_id, code.equiv_text)
        if code.parent_path:
            parent = by_index.get(code.parent_path[-1])
            if parent is None:
                return []  # malformed path; caller reports separately
            parent.children.append(branch)
        else:
            roots.append(branch)
        by_index[index] = branch
    return roots


def tree_signature(branches: list[Branch]) -> tuple:
    """A comparable, sibling-order-insensitive signature of a code forest.

    Siblings are sorted by ``(kind, id, equiv-text, signature-of-children)``,
    which is exactly the requirement: the same code set, element types and
    direct code parent are required; sibling order may differ, but moving a
    code to another level changes its parent path and therefore fails.
    """
    return tuple(sorted((_branch_key(branch) for branch in branches)))


def _branch_key(branch: Branch) -> tuple:
    return (branch.kind, branch.code_id, branch.equiv_text or "",
            tree_signature(branch.children))


def parent_paths(branches: list[Branch], path: tuple[int, ...] = ()) -> dict[str, tuple]:
    """Map each code id to the tuple of enclosing code ids."""
    mapping: dict[str, tuple] = {}
    for index, branch in enumerate(branches):
        mapping[branch.code_id] = path + (branch.code_id,)
        mapping.update(parent_paths(branch.children, path + (branch.code_id,)))
    return mapping


def code_shape(expanded: Expanded) -> dict:
    """Summary used in diagnostics: ids, kinds, nesting."""
    return {
        "ids": expanded.code_ids,
        "kinds": {code.code_id: code.kind for code in expanded.codes},
        "parents": {code.code_id: list(code.parent_path)
                    for code in expanded.codes},
        "equiv": {code.code_id: code.equiv_text for code in expanded.codes
                  if code.equiv_text is not None},
    }


def compare_structure(source: Expanded, target: Expanded) -> list[dict]:
    """Return human-readable problem records comparing source and target."""
    problems: list[dict] = []
    src_ids = set(source.code_ids)
    tgt_ids = set(target.code_ids)
    if len(src_ids) != len(source.code_ids):
        problems.append({"code": "source_duplicate_code_id",
                         "detail": "source repeats a code id"})
    missing = sorted(src_ids - tgt_ids)
    extra = sorted(tgt_ids - src_ids)
    if missing:
        problems.append({"code": "target_missing_code",
                         "detail": f"target lacks code ids {missing}"})
    if extra:
        problems.append({"code": "target_extra_code",
                         "detail": f"target introduces code ids {extra}"})
    src_kind = {c.code_id: c.kind for c in source.codes}
    for cid in sorted(src_ids & tgt_ids):
        kinds = {c.kind for c in target.codes if c.code_id == cid}
        if kinds and src_kind[cid] not in kinds:
            problems.append({
                "code": "target_code_type_changed",
                "detail": f"code {cid!r} is a {src_kind[cid]} in the source "
                          f"but a {'/'.join(sorted(kinds))} in the target"})
    src_equiv = {c.code_id: c.equiv_text for c in source.codes
                 if c.equiv_text is not None}
    for cid, equiv in sorted(src_equiv.items()):
        values = {c.equiv_text for c in target.codes if c.code_id == cid}
        if values and equiv not in values:
            problems.append({
                "code": "target_equiv_changed",
                "detail": f"x {cid!r} equiv-text differs: source {equiv!r}, "
                          f"target {sorted(values)!r}"})
    if not problems:
        # parent path comparison on the flat declaration order
        src_parent = {c.code_id: c.parent_path for c in source.codes}
        tgt_parent = {c.code_id: c.parent_path for c in target.codes}
        src_branch = tree_signature(build_branches(source))
        tgt_branch = tree_signature(build_branches(target))
        if src_branch != tgt_branch:
            problems.append({
                "code": "target_code_hierarchy_changed",
                "detail": "code parent/child nesting or types differ "
                          "(sibling order may change, a code may not change "
                          "its direct code parent)",
                "source_tree": _tree_json(build_branches(source)),
                "target_tree": _tree_json(build_branches(target)),
                "source_parents": {k: list(v) for k, v in src_parent.items()},
                "target_parents": {k: list(v) for k, v in tgt_parent.items()},
            })
    return problems


def _tree_json(branches: list[Branch]) -> list[dict]:
    return [{"kind": b.kind, "id": b.code_id,
             "children": _tree_json(b.children)} for b in branches]


def events_for_comparison(expanded: Expanded) -> list[dict]:
    """Text/code interleaving used to judge whether two translations are equal.

    The sequence holds text runs, ``g`` open/close events and ``x`` events
    carrying the id and ``equiv-text``.  For a well-nested tree the balanced
    open/close sequence already encodes the nesting, so no declaration-order
    index is included.  Attributes that do not affect meaning (XML namespace
    prefixes, attribute order) never appear here.
    """
    return [list(event) for event in expanded.events]

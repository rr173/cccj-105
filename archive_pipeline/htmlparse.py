"""HTML parsing: a tiny tolerant element tree and two layout adapters.

Both adapters produce the same normalized record shape:

    source, document_key, title, issued_on, body, canonical_url

and a structural fingerprint that covers *business* fields only (title, date,
body, canonical).  Navigation markup and the page footer live outside the
extracted content, so changes there cannot alter the fingerprint and therefore
cannot create a version.

Layout A (``sim-a``): notice list/body inside ``<article class="notice">`` with a
``<dl class="facts">`` metadata table; detail pages carry rel=canonical.
Layout B (``sim-b``): cards inside ``<section class="feed">``; a detail record
lives in ``<section class="entry">`` with ``<div class="meta">`` labels, and the
title is an ``<h2>``.  Layout B has no canonical tags and uses 302 redirects from
short links instead.  Header/footer markup differs completely between the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
TEXT_ONLY = {"script", "style", "textarea", "title"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent: Node | None = parent

    def iter_elements(self) -> Iterable["Node"]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter_elements()

    def find_all(
        self,
        tag: str | None = None,
        *,
        attrs: dict[str, str] | None = None,
        class_contains: str | None = None,
        limit: int | None = None,
    ) -> list["Node"]:
        matches: list[Node] = []
        for el in self.iter_elements():
            if tag and el.tag != tag:
                continue
            if attrs and any(el.attrs.get(k) != v for k, v in attrs.items()):
                continue
            if class_contains and class_contains not in el.attrs.get("class", "").split():
                continue
            matches.append(el)
            if limit is not None and len(matches) >= limit:
                break
        return matches

    def first(self, tag: str | None = None, **kwargs) -> "Node | None":
        found = self.find_all(tag, limit=1, **kwargs)
        return found[0] if found else None

    def text(self) -> str:
        chunks: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                chunks.append(child)
            else:
                chunks.append(child.text())
        return "".join(chunks)

    def normalized_text(self) -> str:
        return re.sub(r"\s+", " ", self.text()).strip()


class TolerantParser(HTMLParser):
    """Builds a forgiving tree; never raises on unknown/malformed markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]
        self.notes: list[str] = []

    def handle_starttag(self, tag, attrs):
        node = Node(tag.lower(), {k.lower(): (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        self.notes.append(f"unmatched closing tag </{tag}>")

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def handle_comment(self, data):
        pass


def parse_html(text: str) -> tuple[Node, list[str]]:
    parser = TolerantParser()
    parser.feed(text)
    parser.close()
    return parser.root, parser.notes


# ---------------------------------------------------------------------------
# Shared normalization
# ---------------------------------------------------------------------------

WS = re.compile(r"[ \t\f\v ]+")
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"), None)
DATES = [
    re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$"),
    re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"),
    re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$"),
]
BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "dt", "dd"}


def normalize_date(value: str | None) -> str | None:
    """Return an ISO ``YYYY-MM-DD`` date or None; unknown formats are rejected."""
    if not value:
        return None
    value = value.strip()
    for pattern in DATES:
        match = pattern.match(value)
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _inline_text(el: Node) -> str:
    """Text of an element with collapsed inline whitespace; <br> becomes a space."""
    pieces: list[str] = []

    def rec(node: Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                pieces.append(child)
            elif child.tag in TEXT_ONLY:
                continue
            elif child.tag == "br":
                pieces.append(" ")
            else:
                rec(child)

    rec(el)
    text = "".join(pieces).translate(ZERO_WIDTH)
    text = text.replace("\r", " ").replace("\n", " ")
    return WS.sub(" ", text).strip()


def normalize_body(node: Node | None) -> str:
    """Normalize body markup into paragraph text joined by blank lines.

    Block elements (p/li/h1-h4) become one paragraph each; nested containers are
    walked recursively.  Inline whitespace collapses, zero-width characters are
    stripped, and empty blocks are dropped.  Markup churn *inside* a paragraph
    (re-wrapping emphasis, attributes, whitespace in tags) therefore cannot
    change the normalized body.  Whether a block exists, its order, and its
    visible text are the business signal.
    """
    if node is None:
        return ""
    blocks: list[str] = []

    def walk(el: Node) -> None:
        pending_inline: list[Node | str] = []

        def flush() -> None:
            if not pending_inline:
                return
            holder = Node("#inline", parent=el)
            holder.children = list(pending_inline)
            text = _inline_text(holder)
            if text:
                blocks.append(text)
            pending_inline.clear()

        for child in el.children:
            if isinstance(child, str):
                pending_inline.append(child)
            elif child.tag in TEXT_ONLY:
                continue
            elif child.tag in BLOCK_TAGS:
                flush()
                text = _inline_text(child)
                if text:
                    blocks.append(text)
            elif child.tag == "br":
                pending_inline.append(" ")
            else:
                if _has_block(child):
                    flush()
                    walk(child)
                else:
                    pending_inline.append(child)
        flush()

    walk(node)
    return "\n\n".join(blocks)


def _has_block(el: Node) -> bool:
    return any(sub.tag in BLOCK_TAGS for sub in el.iter_elements())


def blocks_text(node: Node | None) -> list[str]:
    body = normalize_body(node)
    return [block for block in body.split("\n\n") if block]


@dataclass
class DirectoryPage:
    detail_links: list[dict] = field(default_factory=list)
    next_links: list[str] = field(default_factory=list)
    page_label: str = ""


@dataclass
class ParsedRecord:
    document_key: str
    title: str
    issued_on: str
    body: str
    canonical_url: str | None
    body_blocks: list[str]
    issues: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class AdapterError(ValueError):
    pass


class BaseAdapter:
    name = "base"
    layout_description = ""

    def parse_directory(self, root: Node, page_url: str) -> DirectoryPage:
        raise NotImplementedError

    def parse_detail(self, root: Node, page_url: str) -> ParsedRecord:
        raise NotImplementedError


def _related_links(root: Node) -> list[dict]:
    """Links in the directory's ``.related-links`` block are detail candidates.

    They exist so the demo can exercise URL normalization, scope and robots
    checks with links that look exactly like ordinary detail links.
    """
    container = root.first(class_contains="related-links")
    if container is None:
        return []
    links = []
    for anchor in container.find_all("a"):
        href = anchor.attrs.get("href")
        if href:
            links.append({"href": href, "card_title": anchor.normalized_text()[:200],
                          "tracking_marker": "related"})
    return links


class LayoutAAdapter(BaseAdapter):
    """Directory: article cards with class ``notice``. Detail: ``article.notice``."""

    name = "layout_a"
    layout_description = (
        "layout A: <article class='notice'> records, <dl class='facts'> metadata, "
        "<div class='body'> paragraphs, rel=canonical link, <a rel='next'> pagination"
    )

    def parse_directory(self, root: Node, page_url: str) -> DirectoryPage:
        page = DirectoryPage()
        listing = root.first(class_contains="listing")
        if listing is None:
            raise AdapterError("layout A directory: no .listing container")
        page.page_label = (listing.attrs.get("data-page") or "").strip()
        for article in listing.find_all("article", class_contains="notice"):
            anchor = article.first("a", attrs={"data-detail": "true"}) or article.first("a")
            if anchor is None or "href" not in anchor.attrs:
                continue
            page.detail_links.append(
                {
                    "href": anchor.attrs["href"],
                    "card_title": article.normalized_text()[:200],
                    "tracking_marker": article.attrs.get("data-tracking") or "",
                }
            )
        pagination = root.first(class_contains="pagination")
        if pagination is not None:
            for a in pagination.find_all("a"):
                if "next" in (a.attrs.get("rel") or "").split():
                    if a.attrs.get("href"):
                        page.next_links.append(a.attrs["href"])
        page.detail_links.extend(_related_links(root))
        return page

    def parse_detail(self, root: Node, page_url: str) -> ParsedRecord:
        issues: list[str] = []
        article = root.first("article", class_contains="notice")
        if article is None:
            raise AdapterError("layout A detail: no article.notice")
        facts = article.first("dl", class_contains="facts")
        fields: dict[str, str] = {}
        if facts is not None:
            for dt, dd in zip(facts.find_all("dt"), facts.find_all("dd")):
                fields[dt.normalized_text().lower().rstrip(":：")] = dd.normalized_text()
        title_el = article.first("h1") or article.first(class_contains="title")
        title = title_el.normalized_text() if title_el else ""
        body_el = article.first(class_contains="body")
        body = normalize_body(body_el)
        key = article.attrs.get("data-key") or fields.get("编号") or ""
        if not key:
            issues.append("layout A: missing data-key/编号")
        issued = normalize_date(fields.get("发布日期") or fields.get("date"))
        if issued is None:
            issues.append("layout A: missing or unparseable 发布日期")
        canonical = None
        canonical_node = root.first("link", attrs={"rel": "canonical"})
        if canonical_node is not None and canonical_node.attrs.get("href"):
            canonical = canonical_node.attrs["href"]
        return ParsedRecord(
            document_key=_normalize_key(key),
            title=title,
            issued_on=issued or "",
            body=body,
            canonical_url=canonical,
            body_blocks=blocks_text(body_el),
            issues=issues,
            extra={"metadata_fields": sorted(fields)},
        )


class LayoutBAdapter(BaseAdapter):
    """Directory: section.feed cards. Detail: section.entry with div.meta labels."""

    name = "layout_b"
    layout_description = (
        "layout B: <section class='feed'> cards, <section class='entry'> details, "
        "<div class='meta'> label/value rows, <div class='content'> paragraphs, "
        "no rel=canonical (302 short links), <nav class='pager'> rel=next"
    )

    def parse_directory(self, root: Node, page_url: str) -> DirectoryPage:
        page = DirectoryPage()
        feed = root.first("section", class_contains="feed")
        if feed is None:
            raise AdapterError("layout B directory: no section.feed")
        page.page_label = (feed.attrs.get("data-page") or "").strip()
        for card in feed.find_all("div", class_contains="card"):
            anchor = card.first("a", class_contains="detail-link") or card.first("a")
            if anchor is None or "href" not in anchor.attrs:
                continue
            page.detail_links.append(
                {
                    "href": anchor.attrs["href"],
                    "card_title": card.normalized_text()[:200],
                    "tracking_marker": card.attrs.get("data-tracking") or "",
                }
            )
        pager = root.first("nav", class_contains="pager")
        if pager is not None:
            for a in pager.find_all("a"):
                if "next" in (a.attrs.get("rel") or "").split():
                    if a.attrs.get("href"):
                        page.next_links.append(a.attrs["href"])
        page.detail_links.extend(_related_links(root))
        return page

    def parse_detail(self, root: Node, page_url: str) -> ParsedRecord:
        issues: list[str] = []
        entry = root.first("section", class_contains="entry")
        if entry is None:
            raise AdapterError("layout B detail: no section.entry")
        meta = entry.first("div", class_contains="meta")
        fields: dict[str, str] = {}
        if meta is not None:
            for row in meta.find_all("div", class_contains="row"):
                label_el = row.first(class_contains="label")
                value_el = row.first(class_contains="value")
                if label_el is not None and value_el is not None:
                    fields[label_el.normalized_text().lower().rstrip(":：")] = (
                        value_el.normalized_text()
                    )
        title_el = entry.first("h2") or entry.first(class_contains="title")
        title = title_el.normalized_text() if title_el else ""
        body_el = entry.first("div", class_contains="content")
        body = normalize_body(body_el)
        key = entry.attrs.get("data-ref") or fields.get("ref") or ""
        if not key:
            issues.append("layout B: missing data-ref/Ref")
        issued = normalize_date(fields.get("issued") or fields.get("date"))
        if issued is None:
            issues.append("layout B: missing or unparseable Issued")
        canonical = None
        canonical_node = root.first("link", attrs={"rel": "canonical"})
        if canonical_node is not None and canonical_node.attrs.get("href"):
            canonical = canonical_node.attrs["href"]
        return ParsedRecord(
            document_key=_normalize_key(key),
            title=title,
            issued_on=issued or "",
            body=body,
            canonical_url=canonical,
            body_blocks=blocks_text(body_el),
            issues=issues,
            extra={"metadata_fields": sorted(fields)},
        )


def _normalize_key(key: str) -> str:
    return re.sub(r"\s+", "", key or "").upper()


ADAPTERS: dict[str, BaseAdapter] = {
    "layout_a": LayoutAAdapter(),
    "layout_b": LayoutBAdapter(),
}

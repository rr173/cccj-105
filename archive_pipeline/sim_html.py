"""Deterministic HTML for the two local source simulators.

Layout A (``sim-a``): ``article.notice`` cards, ``dl.facts`` metadata,
``rel=canonical`` on detail pages, absolute link hrefs.
Layout B (``sim-b``): ``section.feed`` cards, label/value ``div.meta`` metadata,
no canonical tags, *relative* short-link hrefs that 302 to long URLs.

Header and footer markup differ structurally between the two sites, and both
carry version counters.  Bumping ``nav_version`` / ``footer_version`` changes the
raw bytes of every page without touching any business field.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path


def esc(value: str) -> str:
    return html.escape(value, quote=True)


@dataclass
class Notice:
    key: str
    slug: str
    title: str
    issued: str
    paragraphs: list[str]
    listed: bool = True
    available: bool = True            # False -> detail requests return 404
    redirect_to: str | None = None    # 302 to this (possibly off-site) URL
    status_override: int | None = None
    # An off-site canonical host, to exercise the canonical scope guard.
    canonical_host: str | None = None
    # Served only when the request carries ?edition=public: a *different*
    # document that lives at the same path as the default edition.
    public_edition: dict | None = None


@dataclass
class SiteState:
    layout: str
    notices: list[Notice]
    page_size: int = 3
    extra_pages: int = 0
    pagination_loop: bool = False
    loop_target_page: int = 2
    nav_version: int = 1
    footer_version: int = 1
    tracking_links: bool = True
    extra_links: list[dict] = field(default_factory=list)
    # Directory page number embargoed in robots.txt (None = no embargo).
    robots_embargo_page: int | None = None

    map: dict[str, Notice] = field(default_factory=dict)

    def notice(self, key: str) -> Notice | None:
        return self.map.get(key)


def load_state(path: Path, layout: str) -> SiteState:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    site = raw.get("sites", {}).get(layout)
    if not site:
        raise SystemExit(f"scenario has no site entry {layout!r}")
    notices = [
        Notice(
            key=item["key"],
            slug=item["slug"],
            title=item["title"],
            issued=item["issued"],
            paragraphs=item.get("paragraphs", []),
            listed=item.get("listed", True),
            available=item.get("available", True),
            redirect_to=item.get("redirect_to"),
            status_override=item.get("status_override"),
            canonical_host=item.get("canonical_host"),
            public_edition=item.get("public_edition"),
        )
        for item in site.get("notices", [])
    ]
    state = SiteState(
        layout=layout,
        notices=notices,
        page_size=int(site.get("page_size", 3)),
        extra_pages=int(site.get("extra_pages", 0)),
        pagination_loop=bool(site.get("pagination_loop", False)),
        loop_target_page=int(site.get("loop_target_page", 2)),
        nav_version=int(site.get("nav_version", 1)),
        footer_version=int(site.get("footer_version", 1)),
        tracking_links=bool(site.get("tracking_links", True)),
        extra_links=list(site.get("extra_links", [])),
        robots_embargo_page=site.get("robots_embargo_page"),
    )
    state.map = {notice.key: notice for notice in notices}
    return state


# ---------------------------------------------------------------------------
# Chrome shared by all pages of a layout (differs between layouts).
# ---------------------------------------------------------------------------
def _header_a(state: SiteState) -> str:
    nav = f"<nav class='site-nav' data-nav-v='{state.nav_version}'>"
    nav += "<a href='/index.html'>门户首页</a> · <a href='/notices'>通告</a>"
    if state.nav_version > 1:
        nav += " · <a href='/archives'>归档</a>"
    nav += "</nav>"
    return f"<header>{nav}<h1>公共研究通告（甲版式）</h1></header>"


def _footer_a(state: SiteState) -> str:
    footer = (
        f"<footer class='site-footer' data-footer-v='{state.footer_version}'>"
        f"<p>研究通告平台 · 维护版本 {state.footer_version}</p>"
    )
    if state.footer_version > 1:
        footer += "<p class='footer-links'><a href='/help'>帮助</a> · <a href='/about'>关于</a></p>"
    footer += "</footer>"
    return footer


def _header_b(state: SiteState) -> str:
    nav = f"<section class='topbar' data-nav-v='{state.nav_version}'>"
    nav += "<span>SIM-B</span> <a href='/'>feed</a> <a href='/about'>about</a>"
    if state.nav_version > 1:
        nav += " <a href='/archive'>archive</a>"
    nav += "</section>"
    return f"<header>{nav}</header>"


def _footer_b(state: SiteState) -> str:
    footer = (
        f"<footer class='b-footer' data-footer-v='{state.footer_version}'>"
        f"<span>Open notice feed</span><span>maintenance {state.footer_version}</span>"
    )
    if state.footer_version > 1:
        footer += "<span><a href='/status'>status</a></span>"
    footer += "</footer>"
    return footer


def _shell(state: SiteState, title: str, head_extra: str, body: str) -> str:
    if state.layout == "layout_a":
        return (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<title>{esc(title)}</title>{head_extra}"
            "<link rel='stylesheet' href='/static/a.css'></head><body>"
            f"{_header_a(state)}<main>{body}</main>{_footer_a(state)}</body></html>"
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{esc(title)}</title>{head_extra}"
        "<link rel='stylesheet' href='/static/b.css'></head><body>"
        f"{_header_b(state)}<main>{body}</main>{_footer_b(state)}</body></html>"
    )


# ---------------------------------------------------------------------------
# Directory pages
# ---------------------------------------------------------------------------
def directory_html(state: SiteState, page: int, origin: str) -> str:
    listed = [notice for notice in state.notices if notice.listed]
    total_pages = max(1, (len(listed) + state.page_size - 1) // state.page_size)
    total_pages += state.extra_pages
    start = (page - 1) * state.page_size
    chunk = listed[start:start + state.page_size] if page <= total_pages else []
    tracking = "&utm_source=feed&utm_medium=list" if state.tracking_links else ""

    if state.layout == "layout_a":
        body = (f"<div class='listing' data-page='{page}'>"
                "<h2 class='listing-title'>最新通告</h2>")
        for notice in chunk:
            # Business params are deliberately unsorted; tracking params follow.
            href = f"/notices/{notice.slug}/{notice.key}?view=full&sort=asc{tracking}"
            body += (
                f"<article class='notice' data-key='{esc(notice.key)}' "
                f"data-tracking='list-{page}'>"
                f"<h3><a data-detail='true' href='{esc(href)}'>"
                f"{esc(notice.title)}</a></h3>"
                f"<p class='summary'>发布 {esc(notice.issued)}</p></article>"
            )
        if page == 1:
            body += _extra_links_block(state, "layout_a")
        body += "</div>"
    else:
        body = f"<section class='feed' data-page='{page}'><h1>Notice feed</h1>"
        for index, notice in enumerate(chunk):
            # Relative href, business params unsorted, tracking params appended.
            href = f"n/{notice.slug}?b=2&a=1{tracking}"
            body += (
                f"<div class='card' data-tracking='feed-{page}-{index}'>"
                f"<a class='detail-link' href='{esc(href)}'>{esc(notice.title)}</a>"
                f"<span class='date'>{esc(notice.issued)}</span></div>"
            )
        if page == 1:
            body += _extra_links_block(state, "layout_b")
        body += "</section>"

    pager = _pager(state, page, total_pages)
    return _shell(state, "研究通告" if state.layout == "layout_a" else "Notices",
                  "", body + pager)


def _extra_links_block(state: SiteState, layout: str) -> str:
    """Counterpart links exercising normalization, scope and robots rules."""
    if not state.extra_links:
        return ""
    out = "<div class='related-links'><h3>关联</h3><ul>"
    for link in state.extra_links:
        cls = "detail-link" if layout == "layout_b" else ""
        extra = "data-detail='true'" if layout == "layout_a" else f"class='{cls}'"
        out += (f"<li data-kind='{esc(link.get('kind', 'related'))}'>"
                f"<a {extra} href='{esc(link['href'])}'>{esc(link.get('label', link['href']))}</a>"
                "</li>")
    out += "</ul></div>"
    return out


def _pager(state: SiteState, page: int, total: int) -> str:
    if page < total or state.pagination_loop:
        nxt = state.loop_target_page if state.pagination_loop else page + 1
        if state.layout == "layout_a":
            href = f"/notices/page/{nxt}?view=list&sort=asc&utm_campaign=pager"
            return ("<div class='pagination'>"
                    f"<span>第 {page} 页 / 共 {total} 页</span>"
                    f"<a rel='next' href='{esc(href)}'>下一页</a></div>")
        href = f"/feed?page={nxt}&sort=asc&view=list&utm_campaign=pager"
        return ("<nav class='pager'>"
                f"<span>Page {page} of {total}</span>"
                f"<a rel='next' href='{esc(href)}'>next</a></nav>")
    if state.layout == "layout_a":
        return "<div class='pagination'><span>末页</span></div>"
    return "<nav class='pager'><span>end</span></nav>"


# ---------------------------------------------------------------------------
# Detail pages
# ---------------------------------------------------------------------------
def detail_html_a(state: SiteState, notice: Notice, origin: str, *,
                  key: str | None = None, title: str | None = None,
                  issued: str | None = None,
                  paragraphs: list[str] | None = None,
                  alternate_host: str | None = None) -> str:
    key = key or notice.key
    title = title or notice.title
    issued = issued or notice.issued
    paragraphs = notice.paragraphs if paragraphs is None else paragraphs
    canonical = f"{origin}/notices/{notice.slug}/{key}"
    if alternate_host:
        # Simulates a canonical tag that escaped to another host.
        canonical = f"{alternate_host}/notices/{notice.slug}/{key}"
    paragraphs_html = "".join(f"<p>{esc(text)}</p>" for text in paragraphs)
    body = (
        f"<article class='notice' data-key='{esc(key)}'>"
        f"<h1 class='notice-title'>{esc(title)}</h1>"
        "<dl class='facts'>"
        f"<dt>编号</dt><dd>{esc(key)}</dd>"
        f"<dt>发布日期</dt><dd>{esc(issued)}</dd>"
        "<dt>来源单位</dt><dd>公共研究通告中心</dd>"
        f"</dl><div class='body'>{paragraphs_html}</div></article>"
    )
    return _shell(state, title, f"<link rel='canonical' href='{esc(canonical)}'>", body)


def detail_html_b(state: SiteState, notice: Notice, origin: str, *,
                  key: str | None = None, title: str | None = None,
                  issued: str | None = None,
                  paragraphs: list[str] | None = None,
                  alternate_host: str | None = None) -> str:
    key = key or notice.key
    title = title or notice.title
    issued = issued or notice.issued
    paragraphs = notice.paragraphs if paragraphs is None else paragraphs
    paragraphs_html = "".join(f"<p>{esc(text)}</p>" for text in paragraphs)
    body = (
        f"<section class='entry' data-ref='{esc(key)}'>"
        f"<h2>{esc(title)}</h2>"
        "<div class='meta'>"
        f"<div class='row'><span class='label'>Ref</span>"
        f"<span class='value'>{esc(key)}</span></div>"
        f"<div class='row'><span class='label'>Issued</span>"
        f"<span class='value'>{esc(issued)}</span></div>"
        "</div>"
        f"<div class='content'>{paragraphs_html}</div></section>"
    )
    head = ""
    if alternate_host:
        head = f"<link rel='canonical' href='{esc(alternate_host)}/archive/{notice.slug}/{key}'>"
    return _shell(state, title, head, body)


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------
def robots_a(state: SiteState) -> str:
    lines = [
        "# sim-a robots.txt",
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /private/",
        "# draft notices must never be crawled",
        "Disallow: /notices/draft-notes",
        "Allow: /notices/",
        "Allow: /notices/page/",
    ]
    if state.robots_embargo_page is not None:
        page = state.robots_embargo_page
        lines.insert(5, f"# listing page {page} is embargoed while it is re-indexed")
        lines.insert(6, f"Disallow: /notices/page/{page}?")
    lines += ["Sitemap: /sitemap.xml", "Crawl-delay: 2"]
    return "\n".join(lines) + "\n"


def robots_b() -> str:
    return (
        "# sim-b robots.txt\n"
        "User-agent: archive-bot\n"
        "# descending sort views are disallowed for every agent\n"
        "Disallow: /feed*sort=desc\n"
        "Allow: /feed\n"
        "Allow: /n/\n"
        "Allow: /archive/\n"
        "Disallow: /internal/\n"
        "\n"
        "User-agent: *\n"
        "Disallow: /\n"
    )

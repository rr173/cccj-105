"""Two local source simulators implemented as real HTTP servers.

Each site runs in its own thread on its own port, reads its scenario JSON on
every request, and provides:

* ``/robots.txt`` with real Allow/Disallow rules, different per site;
* directory pages with ``rel=next`` pagination (optionally looping);
* detail pages in layout A or layout B;
* ``ETag`` / ``Last-Modified`` validators with correct ``304`` behaviour;
* in-scope 302 redirects (layout B short links), a 302 to an *off-site* host,
  a 302 to a robots-disallowed path, and 404/503 behaviours per notice;
* a public-edition variant served when ``?edition=public`` is requested, which
  is a *different business document* at the same path (business parameters must
  not be merged);
* ``/debug/requests`` listing everything the server actually served, so the run
  report can be cross-checked against the crawler's own request manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .sim_html import (
    SiteState,
    detail_html_a,
    detail_html_b,
    directory_html,
    load_state,
    robots_a,
    robots_b,
)

A_PORT = 8811
B_PORT = 8812
A_ORIGIN = f"http://127.0.0.1:{A_PORT}"
B_ORIGIN = f"http://127.0.0.1:{B_PORT}"

A_DIRECTORY_RE = re.compile(r"^/notices(?:/page/(\d+))?$")
A_DETAIL_RE = re.compile(r"^/notices/([^/]+)/([A-Za-z0-9\-]+)$")
B_DETAIL_SHORT_RE = re.compile(r"^/n/([^/]+)$")
B_DETAIL_LONG_RE = re.compile(r"^/archive/([^/]+)/([A-Za-z0-9\-]+)$")


def _etag(raw: bytes) -> str:
    return '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'


def _http_date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")


def _parse_http_date(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class SiteServer:
    def __init__(self, layout: str, port: int, scenario_path: Path):
        self.layout = layout
        self.port = port
        self.scenario_path = Path(scenario_path)
        self.served: list[dict] = []
        self.lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        # page_key -> (etag, bytes, last_modified_epoch).  The key embeds every
        # input shaping the bytes, so a content edit yields a new key/ETag and an
        # old conditional request correctly gets 200.
        self.page_cache: dict[str, tuple[str, bytes, float]] = {}

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port),
                                         self._make_handler())
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        import socket
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"simulator on port {self.port} failed to start")

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=5)

    def state(self) -> SiteState:
        return load_state(self.scenario_path, self.layout)

    def served_log(self) -> list[dict]:
        with self.lock:
            return list(self.served)

    def reset_served(self) -> None:
        with self.lock:
            self.served.clear()

    def reset_page_cache(self) -> None:
        self.page_cache.clear()

    # ------------------------------------------------------------------
    def _make_handler(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = f"sim-{site.layout}/1.0"

            def log_message(self, format, *args):  # noqa: A002 - stdlib signature
                pass

            def do_GET(self):  # noqa: N802 - stdlib name
                site._handle(self)

        return Handler

    def _record(self, handler, status: int) -> None:
        with self.lock:
            self.served.append({
                "ts": round(time.time(), 3),
                "method": "GET",
                "path": handler.path,
                "status": status,
                "if_none_match": handler.headers.get("If-None-Match"),
                "if_modified_since": handler.headers.get("If-Modified-Since"),
                "user_agent": handler.headers.get("User-Agent"),
            })

    def _send(self, handler, status: int, body: bytes = b"",
              headers: dict[str, str] | None = None) -> None:
        headers = dict(headers or {})
        if body and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "text/html; charset=utf-8"
        headers["Content-Length"] = str(len(body))
        headers["Connection"] = "close"
        handler.send_response(status)
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.end_headers()
        if body:
            handler.wfile.write(body)

    def _redirect(self, handler, status: int, location: str) -> None:
        self._record(handler, status)
        self._send(handler, status, b"", {"Location": location})

    # ------------------------------------------------------------------
    def _deliver_html(self, handler, page_key: str, html_body: str) -> None:
        """Serve with validators; honor If-None-Match / If-Modified-Since."""
        now = time.time()
        cached = self.page_cache.get(page_key)
        if cached is None:
            cached = (_etag(html_body.encode("utf-8")),
                      html_body.encode("utf-8"), now)
            self.page_cache[page_key] = cached
        etag, body, stamp = cached

        inm = handler.headers.get("If-None-Match")
        ims = handler.headers.get("If-Modified-Since")
        not_modified = False
        if inm is not None:
            not_modified = etag in [part.strip() for part in inm.split(",")]
        elif ims is not None:
            since = _parse_http_date(ims)
            not_modified = since is not None and int(stamp) <= int(since)

        if not_modified:
            self._record(handler, 304)
            self._send(handler, 304, b"", {"ETag": etag,
                                           "Last-Modified": _http_date(stamp)})
            return
        self._record(handler, 200)
        self._send(handler, 200, body, {
            "ETag": etag, "Last-Modified": _http_date(stamp),
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "max-age=60"})

    # ------------------------------------------------------------------
    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        state = self.state()
        parsed = urlsplit(handler.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        origin = A_ORIGIN if self.layout == "layout_a" else B_ORIGIN

        if path == "/robots.txt":
            text = robots_a(state) if self.layout == "layout_a" else robots_b()
            self._record(handler, 200)
            self._send(handler, 200, text.encode("utf-8"), {
                "Content-Type": "text/plain; charset=utf-8",
                "Cache-Control": "max-age=300"})
            return

        if path == "/debug/requests":
            self._send(handler, 200,
                       json.dumps(self.served_log(), ensure_ascii=False,
                                  indent=2).encode("utf-8"),
                       {"Content-Type": "application/json; charset=utf-8"})
            return

        if path == "/__freeze":
            self.page_cache.clear()
            self._send(handler, 200, b"ok", {"Content-Type": "text/plain"})
            return

        # ---- directory pages -----------------------------------------
        directory = False
        page = 1
        if self.layout == "layout_a":
            if path in {"/", "/index.html", "/notices/index.html"}:
                self._redirect(handler, 302, f"{origin}/notices")
                return
            match = A_DIRECTORY_RE.match(path)
            if match:
                directory, page = True, int(match.group(1) or 1)
        else:
            if path in {"/", "/feed", "/feed/index.html"}:
                directory = True
                page = int(query.get("page", ["1"])[0] or 1)

        if directory:
            body = directory_html(state, page, origin)
            key = (f"{self.layout}:dir:{page}:{state.nav_version}:"
                   f"{state.footer_version}:{state.pagination_loop}:"
                   f"{len(state.extra_links)}:{state.tracking_links}")
            self._deliver_html(handler, key, body)
            return

        if path in {"/static/a.css", "/static/b.css"}:
            self._send(handler, 200, b"body{font-family:serif}",
                       {"Content-Type": "text/css"})
            return

        # ---- detail pages --------------------------------------------
        if self.layout == "layout_a":
            match = A_DETAIL_RE.match(path)
            if not match:
                self._record(handler, 404)
                self._send(handler, 404, b"<h1>404 Not Found</h1>")
                return
            slug, key = match.group(1), match.group(2)
            notice = state.map.get(key)
            self._serve_notice(handler, state, notice, slug, key, origin, query)
            return

        short = B_DETAIL_SHORT_RE.match(path)
        if short:
            slug = short.group(1)
            notice = self._find_by_slug(state, slug)
            if notice is None:
                self._record(handler, 404)
                self._send(handler, 404, b"<h1>404</h1>")
                return
            target = f"{origin}/archive/{slug}/{notice.key}"
            if parsed.query:
                target += f"?{parsed.query}"
            self._redirect(handler, 302, target)
            return

        long = B_DETAIL_LONG_RE.match(path)
        if long:
            slug, key = long.group(1), long.group(2)
            notice = state.map.get(key)
            self._serve_notice(handler, state, notice, slug, key, origin, query)
            return

        self._record(handler, 404)
        self._send(handler, 404, b"<h1>404 Not Found</h1>")

    # ------------------------------------------------------------------
    def _find_by_slug(self, state: SiteState, slug: str):
        for notice in state.notices:
            if notice.slug == slug:
                return notice
        return None

    def _serve_notice(self, handler, state: SiteState, notice, slug: str,
                      key: str, origin: str, query: dict) -> None:
        if notice is None:
            self._record(handler, 404)
            self._send(handler, 404, b"<h1>not found</h1>")
            return
        # Off-site redirect target: must never be requested by the crawler.
        if notice.redirect_to:
            self._redirect(handler, 302, notice.redirect_to)
            return
        if notice.status_override == 503:
            self._record(handler, 503)
            self._send(handler, 503, b"<h1>temporarily unavailable</h1>",
                       {"Retry-After": "60"})
            return
        if not notice.available:
            self._record(handler, 404)
            self._send(handler, 404, b"<h1>withdrawn</h1>")
            return

        edition = None
        if notice.public_edition and query.get("edition", [""])[0] == "public":
            edition = notice.public_edition

        fields = {
            "key": edition.get("key", notice.key) if edition else notice.key,
            "title": edition.get("title", notice.title) if edition else notice.title,
            "issued": edition.get("issued", notice.issued) if edition else notice.issued,
            "paragraphs": edition.get("paragraphs", notice.paragraphs)
            if edition else notice.paragraphs,
        }
        alternate = edition.get("canonical_host") if edition else None
        if alternate is None and notice.canonical_host:
            alternate = notice.canonical_host
        render = detail_html_a if self.layout == "layout_a" else detail_html_b
        body = render(state, notice, origin, alternate_host=alternate, **fields)
        page_key = (f"{self.layout}:detail:{key}:{'public' if edition else 'default'}:"
                    f"{fields['key']}:{fields['title']}:{fields['issued']}:"
                    f"{'|'.join(fields['paragraphs'])}:"
                    f"{state.nav_version}:{state.footer_version}:{alternate}")
        self._deliver_html(handler, page_key, body)


def start_sites(scenario_path: Path) -> tuple[SiteServer, SiteServer]:
    site_a = SiteServer("layout_a", A_PORT, scenario_path)
    site_b = SiteServer("layout_b", B_PORT, scenario_path)
    site_a.start()
    site_b.start()
    return site_a, site_b

"""Scan engine: directory walk, conditional detail fetches, versioning, retraction.

Definitions (repeated in every scan report)

*round / scan*
    One traversal of a source from its configured entry point, ending with an
    explicit ``complete`` verdict.  Only a complete scan is a round for
    retraction purposes.

*business fields*
    ``title``, ``issued_on``, ``body`` in normal form.  ``business_digest`` is
    SHA-256 over them.  A version is appended iff the digest differs from the
    last stored version (or no version exists).  Markup outside the extracted
    content (header, nav, footer, attributes) cannot reach the adapter, so a
    navigation/footer-only edit changes raw bytes and the raw digest but never
    the business digest, hence never creates a version.

*304*
    Treated as "not modified": the stored version stays current, the 304 body
    (usually empty) is archived byte-for-byte under its own digest, and no
    version is written.  304 is never interpreted as an empty document.  A 304
    for a URL with no archived version is a protocol error: validators are
    cleared so the next fetch is unconditional, and the scan is incomplete.

*404 / retraction*
    A 404 is recorded in ``scan_misses`` and the document becomes
    ``temporarily_unreachable``.  After the scan's completeness verdict is final,
    a post-pass certifies ``with_retracted`` (撤稿) only when the **two most
    recent scans of the source are both complete and both recorded a 404 for the
    document**.  503s, network errors, parse failures, robots blocks, pagination
    loops and interrupted scans all make a scan incomplete and contribute no
    evidence.  Retraction is a state, not a deletion: versions remain readable.

*pagination loop*
    Directory URLs are visited per scan by normalized identity.  A ``rel=next``
    that resolves to an already-visited directory URL is an observed cycle: the
    URL is not refetched, the scan stops and is reported incomplete /
    ``paging_loop``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin

from .archive import RawArchive
from .config import Config, SourceConfig
from .fetcher import (
    HttpFetcher,
    OffOrigin,
    RateLimiter,
    RedirectBlocked,
    ResponseTooLarge,
    body_digest,
)
from .htmlparse import ADAPTERS, AdapterError, parse_html
from .robots import RobotsPolicy
from .store import Store
from .urlnorm import InvalidURL, normalize_url, split_origin

RETRACTION_THRESHOLD = 2


class InterruptedScan(RuntimeError):
    """Raised to simulate a crash mid-scan; frontier rows stay pending."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def business_digest(title: str, issued_on: str, body: str) -> str:
    payload = json.dumps(
        {"title": title, "issued_on": issued_on, "body": body},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return sha256_text(payload)


def paragraph_diff(old_body: str, new_body: str) -> dict:
    old = [p for p in old_body.split("\n\n") if p]
    new = [p for p in new_body.split("\n\n") if p]
    return {
        "paragraphs_added": [p for p in new if p not in old],
        "paragraphs_removed": [p for p in old if p not in new],
    }


@dataclass
class PageVisit:
    url: str
    page_no: int
    outcome: str = "pending"
    http_status: int | None = None
    raw_digest: str | None = None
    page_label: str = ""
    redirected_to: str | None = None
    detail_urls: list[str] = field(default_factory=list)
    next_urls: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ScanResult:
    scan_id: int
    source_id: str
    complete: bool = True
    paging_loop: bool = False
    stop_reason: str = "completed"
    pages: list[PageVisit] = field(default_factory=list)
    detail_items: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    robots: dict = field(default_factory=dict)
    retraction_verdicts: list[dict] = field(default_factory=list)
    resumed_urls: list[str] = field(default_factory=list)
    interrupted: bool = False


class ScanEngine:
    def __init__(self, config: Config, store: Store, archive: RawArchive,
                 progress: Callable[[str], None] | None = None):
        self.config = config
        self.store = store
        self.archive = archive
        self.progress = progress or (lambda _msg: None)

    # ==================================================================
    def scan(self, source: SourceConfig, *, interrupt_after: int | None = None,
             resume: bool = True) -> ScanResult:
        scan_id = self.store.create_scan(source.source_id, now_iso())
        result = ScanResult(scan_id=scan_id, source_id=source.source_id)
        limiter = RateLimiter(source.min_interval_ms)
        log = result.requests.append

        robots, robots_info = self._load_robots(source, limiter, log)
        result.robots = robots_info
        fetcher = HttpFetcher(source, self.config.user_agent, limiter, robots, log)

        self.progress(
            f"[{source.source_id}] scan {scan_id} start: entry={source.entry_point} "
            f"adapter={source.adapter} robots={robots_info['status']}"
        )

        processed = 0
        try:
            pages = self._walk_directories(source, fetcher, scan_id, result, robots)
            result.pages = pages
            if result.paging_loop or result.stop_reason != "completed":
                result.complete = False

            detail_urls = self._collect_detail_urls(
                source, pages, result, resume, scan_id)
            self.progress(f"[{source.source_id}] {len(detail_urls)} detail URL(s) to fetch")
            for url_key in detail_urls:
                if interrupt_after is not None and processed >= interrupt_after:
                    result.interrupted = True
                    raise InterruptedScan(
                        f"interrupted after {processed} detail URL(s); "
                        f"{len(detail_urls) - processed} remain pending")
                item = self._process_detail(source, fetcher, scan_id, url_key, result)
                result.detail_items.append(item)
                processed += 1
                if processed % 5 == 0:
                    self.progress(
                        f"[{source.source_id}] {processed}/{len(detail_urls)} details")
        except InterruptedScan as exc:
            result.complete = False
            result.stop_reason = "interrupted"
            result.notes.append(str(exc))
            self.store.finish_scan(
                scan_id, finished_at=now_iso(), status="abandoned", complete=False,
                paging_loop=False, stop_reason="interrupted",
                page_count=len(result.pages),
                detail_count=len(result.detail_items),
                requests=result.requests,
                pages=[page.__dict__ for page in result.pages])
            self.progress(f"[{source.source_id}] scan {scan_id}: INTERRUPTED, resume available")
            return result
        except Exception:
            self.store.finish_scan(
                scan_id, finished_at=now_iso(), status="failed", complete=False,
                paging_loop=False, stop_reason="engine_error",
                page_count=len(result.pages),
                detail_count=len(result.detail_items),
                requests=result.requests,
                pages=[page.__dict__ for page in result.pages])
            raise

        # Completeness verdict is final from here on.
        if result.paging_loop:
            result.complete = False
        if result.complete:
            verdicts = self._apply_retractions(source, scan_id, result)
            result.retraction_verdicts = verdicts
        else:
            result.notes.append(
                "scan incomplete: no 404 in this scan can count toward withdrawal; "
                "documents remain temporarily_unreachable at most")

        self.store.finish_scan(
            scan_id, finished_at=now_iso(),
            status="complete" if result.complete else "incomplete",
            complete=result.complete, paging_loop=result.paging_loop,
            stop_reason=result.stop_reason,
            page_count=len(result.pages),
            detail_count=sum(1 for i in result.detail_items
                             if i["outcome"] in {"ok", "not_modified"}),
            requests=result.requests,
            pages=[page.__dict__ for page in result.pages])
        self.progress(
            f"[{source.source_id}] scan {scan_id} {result.stop_reason} "
            f"(complete={result.complete})"
        )
        return result

    # ==================================================================
    def _load_robots(self, source: SourceConfig, limiter: RateLimiter,
                     log: Callable[[dict], None]) -> tuple[RobotsPolicy, dict]:
        robots_url = normalize_url(urljoin(source.base_url, "/robots.txt"),
                                   source.tracking_params)
        # robots.txt is fetched without robots control and *without* following
        # redirects, so an off-origin Location can never cause a request.
        permissive = RobotsPolicy(groups=[], available=True, status="self")
        fetcher = HttpFetcher(source, self.config.user_agent, limiter, permissive, log)
        info = {
            "url": robots_url,
            "available": False,
            "status": "unavailable",
            "unavailable_policy": source.robots_unavailable_policy,
            "supported_subset": RobotsPolicy.SUPPORTED_SUBSET,
            "rules": [],
        }
        try:
            fetched = fetcher.fetch_no_redirect(robots_url, kind="robots")
        except (OffOrigin, RedirectBlocked, ResponseTooLarge, OSError) as exc:
            info["error"] = repr(exc)
            return RobotsPolicy.from_unavailable("unavailable",
                                                 source.robots_unavailable_policy), info
        info["http_status"] = fetched.status
        if fetched.status == 200:
            policy = RobotsPolicy.from_text(fetched.raw.decode("utf-8", "replace"))
            info.update(available=True, status="200",
                        rules=policy.describe(),
                        crawl_delay=policy.crawl_delay)
            return policy, info
        status = (f"redirect_{fetched.status}" if 300 <= fetched.status < 400
                  else f"http_{fetched.status}")
        if 300 <= fetched.status < 400:
            info["location"] = fetched.headers.get("location", "")
            info["note"] = "redirect not followed; treated as unavailable"
        info["status"] = status
        return RobotsPolicy.from_unavailable(status,
                                             source.robots_unavailable_policy), info

    # ==================================================================
    def _walk_directories(self, source: SourceConfig, fetcher: HttpFetcher,
                          scan_id: int, result: ScanResult,
                          robots: RobotsPolicy) -> list[PageVisit]:
        entry = normalize_url(source.entry_point, source.tracking_params)
        adapter = ADAPTERS[source.adapter]
        pages: list[PageVisit] = []
        visited: set[str] = set()
        queued_keys: set[str] = set()
        queue: list[tuple[str, int]] = [(entry, 1)]
        queued_keys.add(entry)

        while queue:
            url, page_no = queue.pop(0)
            queued_keys.discard(url)
            if url in visited:
                continue
            if len(pages) >= source.max_pages_per_scan:
                result.complete = False
                result.stop_reason = (
                    f"page_budget_exhausted (max_pages_per_scan="
                    f"{source.max_pages_per_scan})")
                result.notes.append(result.stop_reason)
                break
            visited.add(url)
            visit = PageVisit(url=url, page_no=page_no)
            pages.append(visit)

            if not self._scope_ok(source, url, robots, result, "directory page"):
                visit.outcome = "blocked"
                visit.note = "origin or robots check refused the request"
                result.complete = False
                result.stop_reason = "directory_blocked"
                break

            try:
                fetched = fetcher.fetch(url, kind="directory")
            except OffOrigin as exc:
                visit.outcome = "blocked"
                visit.note = str(exc)
                result.complete = False
                result.stop_reason = "directory_robots_disallowed"
                result.notes.append(f"{url}: {exc}")
                break
            except RedirectBlocked as exc:
                visit.outcome = "redirect_blocked"
                visit.note = str(exc)
                result.complete = False
                result.stop_reason = "directory_redirect_blocked"
                result.notes.append(f"{url}: {exc}")
                break
            except (OSError, ResponseTooLarge) as exc:
                visit.outcome = "http_error"
                visit.note = repr(exc)
                result.complete = False
                result.stop_reason = "directory_http_error"
                result.notes.append(f"{url}: {exc}")
                continue

            visit.http_status = fetched.status
            visit.raw_digest = body_digest(fetched.raw)
            self.archive.store(
                source_id=source.source_id, digest=visit.raw_digest, raw=fetched.raw,
                url=url, final_url=fetched.final_url, status=fetched.status,
                headers=fetched.headers, scan_id=scan_id, kind="directory")

            if fetched.redirect_chain:
                self._record_redirects(source, fetched.redirect_chain, scan_id, result)
                final_key = normalize_url(fetched.final_url, source.tracking_params)
                if final_key != url:
                    visit.redirected_to = final_key
                    if final_key in visited:
                        visit.outcome = "redirect_to_visited"
                        visit.note = f"redirects to already visited directory {final_key}"
                        result.complete = False
                        result.paging_loop = True
                        result.stop_reason = (
                            f"paging_loop: {url} redirects to visited {final_key}")
                        result.notes.append(visit.note)
                        break
                    visited.add(final_key)

            if fetched.status != 200:
                visit.outcome = ("not_found" if fetched.status in {404, 410}
                                 else "http_error")
                visit.note = f"HTTP {fetched.status}"
                result.complete = False
                result.stop_reason = f"directory_http_{fetched.status}"
                result.notes.append(f"{url}: HTTP {fetched.status}")
                continue

            try:
                root, _notes = parse_html(fetched.raw.decode("utf-8", "replace"))
                parsed = adapter.parse_directory(root, url)
            except AdapterError as exc:
                visit.outcome = "parse_error"
                visit.note = str(exc)
                result.complete = False
                result.stop_reason = "directory_parse_error"
                result.notes.append(f"{url}: {exc}")
                continue

            visit.outcome = "ok"
            visit.page_label = parsed.page_label

            for link in parsed.detail_links:
                try:
                    detail_url = normalize_url(
                        urljoin(url, link["href"]), source.tracking_params)
                except InvalidURL as exc:
                    result.notes.append(
                        f"{url}: ignored invalid detail link {link['href']!r}: {exc}")
                    continue
                if not self._scope_ok(source, detail_url, robots, result,
                                     f"detail link on {url}"):
                    continue
                visit.detail_urls.append(detail_url)
                self.store.discover_url(source.source_id, detail_url, scan_id,
                                        now_iso())

            if parsed.next_links:
                for href in parsed.next_links:
                    try:
                        next_url = normalize_url(
                            urljoin(url, href), source.tracking_params)
                    except InvalidURL as exc:
                        result.notes.append(
                            f"{url}: ignored invalid next link {href!r}: {exc}")
                        continue
                    if not self._scope_ok(source, next_url, robots, result,
                                         f"next link on {url}"):
                        # A refused *directory* successor means the listing
                        # cannot be walked to its end: the scan is incomplete
                        # even though no loop was observed.
                        visit.next_urls.append(next_url)
                        result.complete = False
                        result.stop_reason = (
                            f"paging_blocked: next directory page {next_url} was "
                            "refused (out of scope or robots-disallowed)")
                        result.notes.append(result.stop_reason)
                        result.notes.append(
                            "unvisited directory successors remain; the listing "
                            "of this scan is incomplete")
                        break
                    visit.next_urls.append(next_url)
                    if next_url in visited:
                        result.complete = False
                        result.paging_loop = True
                        result.stop_reason = (
                            f"paging_loop: page {url} (page {page_no}) links next to "
                            f"already visited directory {next_url}")
                        result.notes.append(result.stop_reason)
                        result.notes.append(
                            "walk terminated at the cycle without refetching; the "
                            "directory set for this scan is incomplete")
                        break
                    if next_url in queued_keys:
                        result.complete = False
                        result.paging_loop = True
                        result.stop_reason = (
                            f"paging_loop: page {url} links to already queued "
                            f"directory {next_url}")
                        result.notes.append(result.stop_reason)
                        break
                    queue.append((next_url, page_no + 1))
                    queued_keys.add(next_url)
                if result.paging_loop or result.stop_reason.startswith("paging_blocked"):
                    break

        return pages

    # ==================================================================
    def _collect_detail_urls(self, source: SourceConfig, pages: list[PageVisit],
                             result: ScanResult, resume: bool,
                             scan_id: int) -> list[str]:
        """URLs to request this scan.

        A crawler requests what the directory actually linked to (the short
        alias on site B), letting the redirect resolve during the fetch; this
        keeps the request manifest truthful and the validators attached to the
        URL that was requested.  Distinct URLs that are known to alias the same
        target are deduplicated so the target is not fetched twice in one scan.
        """
        ordered: list[str] = []
        seen_targets: set[str] = set()
        aliases = {row["from_key"]: row["to_key"]
                   for row in self.store.aliases(source.source_id)}
        # Rows still pending at the start of this scan were left behind by an
        # earlier interrupted scan (a normal scan leaves no pending rows except
        # retriable ones such as a blocked redirect).  Whether or not the
        # directory lists them again, requesting them is the resume contract.
        leftover_pending = {row["url_key"]: row for row in
                            self.store.pending_urls(source.source_id)}
        for page in pages:
            for url in page.detail_urls:
                dedupe_key = aliases.get(url, url)
                if dedupe_key in seen_targets:
                    result.notes.append(
                        f"duplicate listing {url} resolves to already queued "
                        f"{dedupe_key}; requesting it once")
                    continue
                seen_targets.add(dedupe_key)
                ordered.append(url)
                if url in leftover_pending and leftover_pending[url]["discovered_scan"]\
                        is not None and leftover_pending[url]["discovered_scan"] < scan_id:
                    result.resumed_urls.append(url)
                    result.notes.append(
                        f"resumed pending URL left by an interrupted scan: {url}")
        if resume:
            for key, row in leftover_pending.items():
                dedupe_key = aliases.get(key, key)
                if dedupe_key in seen_targets:
                    continue
                seen_targets.add(dedupe_key)
                ordered.append(key)
                result.resumed_urls.append(key)
                result.notes.append(
                    f"resumed pending URL left by an interrupted scan: {key}")
        return ordered

    # ==================================================================
    def _process_detail(self, source: SourceConfig, fetcher: HttpFetcher,
                        scan_id: int, url_key: str, result: ScanResult) -> dict:
        item: dict = {
            "url_key": url_key,
            "kind": "detail",
            "outcome": "unknown",
            "http_status": None,
            "raw_digest": None,
            "document_key": None,
            "version_created": False,
            "version_id": None,
            "version_no": 0,
            "version_reason": "",
            "note": "",
        }
        request_log: list[dict] = []

        def log_both(row: dict) -> None:
            """Scan-level manifest must contain every request, including details."""
            result.requests.append(row)
            request_log.append(row)

        fetcher.log = log_both
        row = self.store.get_url(source.source_id, url_key)
        if row is None:
            row = self.store.discover_url(source.source_id, url_key, scan_id, now_iso())
        etag = row["etag"] or None
        last_modified = row["last_modified"] or None

        try:
            fetched = fetcher.fetch(url_key, kind="detail", etag=etag,
                                    last_modified=last_modified)
        except OffOrigin as exc:
            item["outcome"] = "robots_disallowed"
            item["note"] = str(exc)
            item["requests"] = request_log
            result.blocked.append({"url": url_key, "where": "detail",
                                   "reason": "robots_or_origin"})
            return self._persist_item(source, scan_id, item)
        except RedirectBlocked as exc:
            item["outcome"] = "redirect_blocked"
            item["note"] = str(exc)
            item["requests"] = request_log
            result.blocked.append({"url": url_key, "where": "detail",
                                   "reason": "redirect_blocked"})
            # Stay pending: the next scan/restart should retry it.
            self.store.set_url_state(source.source_id, url_key, state="pending",
                                     updated_at=now_iso())
            return self._persist_item(source, scan_id, item)
        except ResponseTooLarge as exc:
            item["outcome"] = "response_too_large"
            item["note"] = str(exc)
            item["requests"] = request_log
            result.complete = False
            result.stop_reason = "detail_response_too_large"
            return self._persist_item(source, scan_id, item)
        except OSError as exc:
            item["outcome"] = "network_error"
            item["note"] = repr(exc)
            item["requests"] = request_log
            result.complete = False
            result.stop_reason = "detail_network_error"
            result.notes.append(f"{url_key}: network error {exc!r}")
            return self._persist_item(source, scan_id, item)

        item["requests"] = request_log
        item["http_status"] = fetched.status
        digest = body_digest(fetched.raw)
        item["raw_digest"] = digest
        stored = self.archive.store(
            source_id=source.source_id, digest=digest, raw=fetched.raw,
            url=url_key, final_url=fetched.final_url, status=fetched.status,
            headers=fetched.headers, scan_id=scan_id, kind="detail")
        item["archive_path"] = stored["path"]
        item["requested_etag"] = etag or ""
        item["requested_last_modified"] = last_modified or ""

        if fetched.redirect_chain:
            self._record_redirects(source, fetched.redirect_chain, scan_id, result)
            final_key = normalize_url(fetched.final_url, source.tracking_params)
            item["redirected_to"] = final_key
            if final_key != url_key:
                self.store.record_alias(source.source_id, url_key, final_key, scan_id)
                item["requests_alias"] = True

        # ---- 304 ----------------------------------------------------
        if fetched.status == 304:
            # Look up by document key first (an alias URL may differ from the
            # URL that produced the version), then by URL as a fallback.
            doc_key = (row["document_key"] if row else None) or \
                self.store.document_for_url(source.source_id, url_key)
            prior = (self.store.last_version(source.source_id, doc_key)
                     if doc_key else None) or \
                self.store.last_version_of_url(source.source_id, url_key)
            if prior is None:
                item["outcome"] = "304_without_version"
                item["note"] = ("304 with no locally archived version; validators "
                                "cleared so the next fetch is unconditional")
                result.complete = False
                result.stop_reason = "detail_304_without_version"
                result.notes.append(f"{url_key}: {item['note']}")
                self.store.set_url_state(
                    source.source_id, url_key, state="processed",
                    updated_at=now_iso(), etag="", last_modified="",
                    last_status=304, missing_streak=0)
                return self._persist_item(source, scan_id, item)
            item["outcome"] = "not_modified"
            item["document_key"] = prior["document_key"]
            item["version_no"] = prior["version_no"]
            item["content_digest"] = prior["content_digest"]
            item["version_reason"] = "http_304_reused_version"
            item["previous_raw_digest"] = prior["raw_digest"]
            item["note"] = (
                f"HTTP 304: reused archived version {prior['version_no']}; "
                f"no version written; 304 body ({len(fetched.raw)} bytes, "
                f"sha256 {digest[:12]}…) archived")
            for key in self._storage_keys(url_key, item):
                self.store.set_url_state(
                    source.source_id, key, state="processed",
                    updated_at=now_iso(), last_status=304, missing_streak=0,
                    document_key=prior["document_key"],
                    last_version=prior["version_no"])
            self.store.clear_missing(source.source_id, url_key, updated_at=now_iso())
            doc = self.store.get_document(source.source_id, prior["document_key"])
            if doc and doc["state"] in {"with_retracted", "temporarily_unreachable"}:
                # A 304 is a positive existence signal: the resource is still
                # served, it just has not changed.  Reactivate.
                self.store.mark_document_active(source.source_id,
                                                prior["document_key"], url_key)
                item["note"] += f"; document reactivated (was {doc['state']})"
            item["raw_bytes_changed"] = False
            return self._persist_item(source, scan_id, item)

        # ---- 404 / 410 ---------------------------------------------
        if fetched.status in {404, 410}:
            document_key = (row["document_key"] if row else None) or \
                self.store.document_for_url(source.source_id, url_key)
            if not document_key:
                # Never successfully parsed: evidence is kept at URL level so the
                # >2-complete-rounds rule still applies to it, while the absence
                # of archived versions is reported explicitly.
                document_key = f"url:{url_key}"
                self.store.ensure_document(source.source_id, document_key)
            self.store.record_miss(scan_id, source.source_id, document_key,
                                   url_key, fetched.status)
            streak = self.store.bump_missing(source.source_id, url_key,
                                             http_status=fetched.status,
                                             updated_at=now_iso())
            item["outcome"] = "not_found"
            item["document_key"] = document_key
            item["note"] = (
                f"HTTP {fetched.status}; URL miss counter {streak}. "
                "Remains temporarily_unreachable until two consecutive complete "
                "scans observe 404")
            doc = self.store.get_document(source.source_id, document_key)
            if not doc or doc["state"] != "with_retracted":
                self.store.mark_document_missing(
                    source.source_id, document_key,
                    missing_streak=streak, last_url_key=url_key)
            self.store.set_url_state(source.source_id, url_key, state="processed",
                                     updated_at=now_iso(), last_status=fetched.status,
                                     missing_streak=streak)
            return self._persist_item(source, scan_id, item)

        if fetched.status != 200:
            item["outcome"] = ("server_error" if fetched.status >= 500
                               else "http_error")
            item["note"] = (
                f"HTTP {fetched.status}; not counted as a miss, no version written. "
                "This scan is incomplete and does not count as a round")
            result.complete = False
            result.stop_reason = f"detail_http_{fetched.status}"
            result.notes.append(
                f"{url_key}: HTTP {fetched.status}; scan will be marked incomplete "
                "(503/transient errors do not advance the withdrawal counter)")
            self.store.set_url_state(source.source_id, url_key, state="processed",
                                     updated_at=now_iso(), last_status=fetched.status)
            return self._persist_item(source, scan_id, item)

        # ---- 200 ----------------------------------------------------
        try:
            root, _notes = parse_html(fetched.raw.decode("utf-8", "replace"))
            record = ADAPTERS[source.adapter].parse_detail(root, fetched.final_url)
        except (AdapterError, ValueError) as exc:
            item["outcome"] = "parse_error"
            item["note"] = str(exc)
            result.complete = False
            result.stop_reason = "detail_parse_error"
            result.notes.append(f"{url_key}: {exc}")
            self.store.set_url_state(source.source_id, url_key, state="processed",
                                     updated_at=now_iso(), last_status=200)
            return self._persist_item(source, scan_id, item)

        if not record.document_key:
            item["outcome"] = "missing_document_key"
            item["note"] = "; ".join(record.issues)
            result.complete = False
            result.stop_reason = "detail_missing_document_key"
            self.store.set_url_state(source.source_id, url_key, state="processed",
                                     updated_at=now_iso(), last_status=200)
            return self._persist_item(source, scan_id, item)

        key = record.document_key
        item["document_key"] = key
        item["title"] = record.title
        item["issued_on"] = record.issued_on
        item["body_paragraphs"] = record.body.split("\n\n") if record.body else []
        if record.issues:
            item["parse_warnings"] = record.issues
        b_digest = business_digest(record.title, record.issued_on, record.body)
        c_digest = sha256_text(record.body)
        item["business_digest"] = b_digest
        item["content_digest"] = c_digest

        if record.canonical_url:
            try:
                canonical_abs = normalize_url(
                    urljoin(fetched.final_url, record.canonical_url),
                    source.tracking_params)
                scheme, host, port = split_origin(canonical_abs)
                item["canonical_url"] = canonical_abs
                if not source.origin_allowed(scheme, host, port):
                    item["canonical_blocked"] = canonical_abs
                    result.notes.append(
                        f"{url_key}: canonical {canonical_abs} is off-origin; "
                        "recorded but never requested")
            except InvalidURL as exc:
                item["canonical_blocked"] = f"invalid: {exc}"
                result.notes.append(f"{url_key}: invalid canonical: {exc}")

        previous = self.store.last_version(source.source_id, key)
        if previous is None:
            self.store.ensure_document(source.source_id, key)
            version_id = self.store.add_version(
                source_id=source.source_id, document_key=key, url_key=url_key,
                version_no=1, title=record.title, issued_on=record.issued_on,
                body=record.body, canonical_url=item.get("canonical_url"),
                raw_digest=digest, content_digest=c_digest, business_digest=b_digest,
                scan_id=scan_id, created_at=now_iso())
            item.update(version_created=True, version_id=version_id, version_no=1,
                        version_reason="first_observation",
                        note="first version for this document")
        elif b_digest != previous["business_digest"]:
            changed = [name for name, old, new in (
                ("title", previous["title"], record.title),
                ("issued_on", previous["issued_on"], record.issued_on),
                ("body", previous["body"], record.body),
            ) if old != new]
            version_no = int(previous["version_no"]) + 1
            version_id = self.store.add_version(
                source_id=source.source_id, document_key=key, url_key=url_key,
                version_no=version_no, title=record.title,
                issued_on=record.issued_on, body=record.body,
                canonical_url=item.get("canonical_url"),
                raw_digest=digest, content_digest=c_digest, business_digest=b_digest,
                scan_id=scan_id, created_at=now_iso())
            item.update(version_created=True, version_id=version_id,
                        version_no=version_no, changed_fields=changed,
                        version_reason="business_fields_changed:" + ",".join(changed),
                        diff=paragraph_diff(previous["body"], record.body),
                        raw_bytes_changed=previous["raw_digest"] != digest,
                        previous_raw_digest=previous["raw_digest"],
                        note=(f"business fields changed ({','.join(changed)}) -> "
                              f"version {version_no}"))
            doc = self.store.get_document(source.source_id, key)
            if doc and doc["state"] == "with_retracted":
                self.store.mark_document_active(source.source_id, key, url_key)
                item["note"] += "; previously retracted document reactivated"
        else:
            item.update(version_created=False, version_no=previous["version_no"],
                        version_id=None, version_reason="business_fields_unchanged",
                        previous_raw_digest=previous["raw_digest"],
                        diff=paragraph_diff(previous["body"], record.body))
            if previous["raw_digest"] == digest:
                item["version_reason"] = "unchanged_bytes"
                item["raw_bytes_changed"] = False
                item["note"] = "business fields and raw bytes identical; reused version"
            else:
                item["raw_bytes_changed"] = True
                item["note"] = (
                    "business fields identical; raw bytes changed "
                    f"({digest[:12]}… vs {previous['raw_digest'][:12]}…) — "
                    "navigation/footer/markup edit, no version created")

        self.store.set_url_state(
            source.source_id, url_key, state="processed", updated_at=now_iso(),
            etag=fetched.headers.get("etag") or "",
            last_modified=fetched.headers.get("last-modified") or "",
            content_digest=c_digest,
            canonical_url=item.get("canonical_url") or "",
            last_version=item["version_no"], last_status=200,
            document_key=key, missing_streak=0)
        for alias_key in self._storage_keys(url_key, item):
            if alias_key == url_key:
                continue
            self.store.set_url_state(
                source.source_id, alias_key, state="processed",
                updated_at=now_iso(),
                etag=fetched.headers.get("etag") or "",
                last_modified=fetched.headers.get("last-modified") or "",
                content_digest=c_digest,
                canonical_url=item.get("canonical_url") or "",
                last_version=item["version_no"], last_status=200,
                document_key=key, missing_streak=0)
        doc = self.store.get_document(source.source_id, key)
        if not doc or doc["state"] != "with_retracted":
            self.store.mark_document_active(source.source_id, key, url_key)
        item["outcome"] = "ok"
        return self._persist_item(source, scan_id, item)

    # ==================================================================
    def _storage_keys(self, url_key: str, item: dict) -> list[str]:
        """Frontier rows that must mirror this observation's state.

        A request to an alias (short link) resolves to the canonical long URL;
        validators belong to the bytes at the final URL, so both rows carry
        them.  This is what lets the *next* scan issue a conditional GET against
        whichever URL the directory currently lists.
        """
        keys = [url_key]
        if item.get("redirected_to") and item["redirected_to"] != url_key:
            keys.append(item["redirected_to"])
        return keys

    def _persist_item(self, source: SourceConfig, scan_id: int, item: dict) -> dict:
        payload = {k: v for k, v in item.items() if k not in {
            "scan_id", "source_id", "url_key", "kind", "outcome", "http_status",
            "raw_digest", "document_key", "version_created", "version_id",
            "version_no", "version_reason", "requests", "note"}}
        self.store.add_scan_item(
            scan_id=scan_id, source_id=source.source_id, url_key=item["url_key"],
            kind=item.get("kind", "detail"), outcome=item["outcome"],
            http_status=item.get("http_status"), raw_digest=item.get("raw_digest"),
            document_key=item.get("document_key"),
            version_created=item.get("version_created", False),
            version_id=item.get("version_id"), version_no=item.get("version_no"),
            version_reason=item.get("version_reason", ""),
            requests=item.get("requests", []), note=item.get("note", ""),
            detail=payload)
        return item

    # ==================================================================
    def _apply_retractions(self, source: SourceConfig, scan_id: int,
                           result: ScanResult) -> list[dict]:
        """Post-pass, run only when this scan's completeness verdict is final.

        Withdrawal（撤稿）rule, applied literally:

        *count only complete rounds.*  Let ``R_last`` be the most recent **prior**
        complete scan of this source.  A document is certified ``with_retracted``
        iff this scan is complete, observed a 404 for the document, **and**
        ``R_last`` also observed a 404 for it.  That is exactly "连续两轮完整
        扫描均为 404": incomplete scans (503, network error, parse failure,
        robots block, pagination loop, crash) are skipped — per the rule they
        neither count as a round nor reset the count.

        A complete round that served the document normally is *not* skipped, so
        it breaks the chain and resets the evidence.
        """
        scans = self.store.scans(source.source_id)
        if not scans or scans[-1]["scan_id"] != scan_id:
            return []
        prior_complete = [s for s in scans[:-1] if s["complete"]]
        last_round = prior_complete[-1] if prior_complete else None
        verdicts: list[dict] = []
        for document_key in self.store.documents_missed_in_scan(scan_id):
            miss_now = set(self.store.misses_in_scan(scan_id, document_key))
            miss_last = set()
            if last_round is not None:
                miss_last = {
                    row["url_key"] for row in self.store.complete_scans_missing(
                        source.source_id, document_key)
                    if row["scan_id"] == last_round["scan_id"]
                }
            certified = (
                result.complete
                and last_round is not None
                and bool(miss_now) and bool(miss_last)
            )
            verdict = {
                "document_key": document_key,
                "current_scan_id": scan_id,
                "previous_complete_scan_id": (
                    last_round["scan_id"] if last_round else None),
                "skipped_incomplete_scans": [
                    s["scan_id"] for s in scans[:-1]
                    if not s["complete"] and s["status"] != "running"],
                "current_misses": sorted(miss_now),
                "previous_complete_misses": sorted(miss_last),
                "certified": certified,
            }
            if not certified:
                if last_round is None:
                    reason = "no earlier complete round; not enough evidence"
                elif not miss_last:
                    reason = ("the most recent complete round did not observe a "
                              "404 for this document")
                elif not result.complete:
                    reason = "current scan is incomplete"
                else:
                    reason = "current scan did not observe a 404"
                verdict["reason"] = reason
                result.notes.append(
                    f"document {document_key}: 404 observed but withdrawal NOT "
                    f"certified ({reason}); remains temporarily_unreachable")
                verdicts.append(verdict)
                continue

            self.store.mark_document_retracted(
                source.source_id, document_key, retracted_at=now_iso(),
                scan_id=scan_id)
            prior_versions = self.store.versions_for(source.source_id, document_key)
            verdict["archived_versions_preserved"] = len(prior_versions)
            verdict["evidence"] = (
                f"two consecutive complete rounds "
                f"({last_round['scan_id']}, {scan_id}) both observed HTTP 404")
            result.notes.append(
                f"document {document_key}: RETRACTED — 404 in consecutive complete "
                f"rounds {last_round['scan_id']} and {scan_id}; "
                f"{len(prior_versions)} archived version(s) remain readable")
            verdicts.append(verdict)

        # Documents whose URL was not requested at all this scan are reported.
        item_urls = {item["url_key"] for item in result.detail_items}
        for doc in self.store.documents(source.source_id):
            if doc["state"] == "with_retracted":
                continue
            if doc["last_url_key"] and doc["last_url_key"] not in item_urls:
                result.notes.append(
                    f"document {doc['document_key']}: not requested this scan "
                    "(absent from directory or aliased); absence alone is not a 404")
        return verdicts

    # ==================================================================
    def _scope_ok(self, source: SourceConfig, url: str, robots: RobotsPolicy,
                  result: ScanResult, where: str) -> bool:
        try:
            scheme, host, port = split_origin(url)
        except InvalidURL as exc:
            result.blocked.append({"url": url, "where": where,
                                   "reason": f"invalid_url: {exc}"})
            result.notes.append(f"{where}: rejected invalid URL {url}: {exc}")
            return False
        if not source.origin_allowed(scheme, host, port):
            result.blocked.append({"url": url, "where": where,
                                   "reason": "origin_not_allowed"})
            result.notes.append(
                f"{where}: refused off-origin URL {url}; no request made")
            return False
        if not robots.allowed(url, self.config.user_agent):
            result.blocked.append({"url": url, "where": where,
                                   "reason": "robots_disallowed"})
            result.notes.append(
                f"{where}: {url} disallowed by robots.txt; no request made")
            return False
        return True

    def _record_redirects(self, source: SourceConfig, chain: list[dict],
                          scan_id: int, result: ScanResult) -> None:
        for hop in chain:
            self.store.record_alias(source.source_id, hop["from"], hop["to"], scan_id)
            result.notes.append(
                f"redirect {hop['status']}: {hop['from']} -> {hop['to']} "
                "(in-scope target, followed)")

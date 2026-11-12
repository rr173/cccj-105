"""HTTP fetching with scope guards, robots checks, spacing and conditional GET.

Every outgoing request (including each redirect hop, and robots.txt) goes
through:

1. **origin allow-list** — scheme/host/port must exactly match one of the
   source's configured ``allowed_origins``; anything else raises ``OffOrigin``
   and no request is made;
2. **robots policy** for the source principal (caller-supplied policy);
3. **minimum request interval** for the host:port (measured between request
   starts, with the wait recorded in the request log).

Redirect handling is per-hop and never off-origin: a 3xx with an *out-of-scope*
``Location`` is logged as ``redirect_blocked`` and raises ``RedirectBlocked``
without requesting the target.  In-scope redirects are followed up to
``max_redirects`` and each hop is logged.  Redirect loops are detected and
aborted.

Conditional GET is used whenever validators exist: ``If-None-Match`` /
``If-Modified-Since``.  The raw bytes of every response (including 304 bodies,
which may be non-empty) are returned so the caller can archive them by digest.
"""

from __future__ import annotations

import hashlib
import http.client
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlsplit

from .config import SourceConfig
from .robots import RobotsPolicy
from .urlnorm import InvalidURL, normalize_url, split_origin

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class OffOrigin(ValueError):
    """Requested URL (or redirect target) is outside the source's scope."""


class RedirectBlocked(RuntimeError):
    """Redirect target is off-origin, in a loop, or the budget is exhausted."""


class ResponseTooLarge(RuntimeError):
    pass


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


body_digest = sha256_bytes


class RateLimiter:
    """Minimum spacing between request starts, keyed by origin.

    The clock is injectable so tests can measure spacing without sleeping.
    """

    def __init__(self, interval_ms: int, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.interval_s = max(0.0, interval_ms / 1000.0)
        self.sleep = sleep
        self.clock = clock
        self.last_start: dict[str, float] = {}

    def wait(self, key: str) -> float:
        now = self.clock()
        last = self.last_start.get(key)
        delayed = 0.0
        if last is not None and self.interval_s > 0:
            remaining = self.interval_s - (now - last)
            if remaining > 0:
                delayed = remaining
                self.sleep(remaining)
        self.last_start[key] = self.clock()
        return delayed


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]
    raw: bytes
    redirect_chain: list[dict] = field(default_factory=list)


class HttpFetcher:
    def __init__(
        self,
        source: SourceConfig,
        user_agent: str,
        limiter: RateLimiter,
        robots: RobotsPolicy,
        log: Callable[[dict], None],
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.source = source
        self.user_agent = user_agent
        self.limiter = limiter
        self.robots = robots
        self.log = log
        self.sleep = sleep

    # -- guards -------------------------------------------------------
    def origin_of(self, url: str) -> tuple[str, str, int]:
        try:
            scheme, host, port = split_origin(url)
        except InvalidURL as exc:
            raise OffOrigin(f"{url}: {exc}") from exc
        if not self.source.origin_allowed(scheme, host, port):
            raise OffOrigin(
                f"{url}: {scheme}://{host}:{port} is outside allowed origins "
                f"{self.source.allowed_origins}")
        return scheme, host, port

    def _norm(self, url: str) -> str:
        try:
            return normalize_url(url, self.source.tracking_params)
        except InvalidURL:
            return url.split("#", 1)[0]

    # -- logging ------------------------------------------------------
    def _log(self, *, url: str, kind: str, status: int | None, outcome: str,
             wait_ms: float, conditional: dict | None = None, note: str = "",
             redirect_from: str | None = None, requested: bool = True) -> None:
        self.log({
            "ts": round(time.time(), 3),
            "source": self.source.source_id,
            "kind": kind,
            "url": url,
            "normalized": self._norm(url),
            "requested": requested,
            "status": status,
            "outcome": outcome,
            "spacing_wait_ms": round(wait_ms, 1),
            "conditional_headers": conditional or {},
            "redirect_from": redirect_from,
            "note": note,
        })

    # -- single raw request ------------------------------------------
    def _request(self, url: str, *, kind: str, conditional: dict[str, str],
                 wait_ms: float) -> FetchResult:
        scheme, host, port = self.origin_of(url)
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port, timeout=self.source.timeout_seconds)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity",
            "Connection": "close",
            **conditional,
        }
        try:
            conn.request("GET", target, headers=headers)
            response = conn.getresponse()
            raw = response.read(self.source.max_response_bytes + 1)
            if len(raw) > self.source.max_response_bytes:
                self._log(url=url, kind=kind, status=response.status,
                          outcome="response_too_large", wait_ms=wait_ms,
                          conditional=conditional)
                raise ResponseTooLarge(
                    f"{url}: body exceeds max_response_bytes="
                    f"{self.source.max_response_bytes}")
            result = FetchResult(
                requested_url=url, final_url=url, status=response.status,
                headers={k.lower(): v for k, v in response.getheaders()}, raw=raw)
        except (OSError, http.client.HTTPException) as exc:
            self._log(url=url, kind=kind, status=None, outcome="network_error",
                      wait_ms=wait_ms, conditional=conditional, note=repr(exc))
            raise
        finally:
            conn.close()

        outcome = (
            "not_modified" if result.status == 304 else
            "redirect" if result.status in REDIRECT_STATUSES else
            "not_found" if result.status in {404, 410} else
            "server_error" if result.status >= 500 else
            "ok" if result.status == 200 else
            f"http_{result.status}"
        )
        self._log(url=url, kind=kind, status=result.status, outcome=outcome,
                  wait_ms=wait_ms, conditional=conditional)
        return result

    def _spaced_request(self, url: str, *, kind: str, conditional: dict[str, str],
                        extra_note: str = "") -> FetchResult:
        scheme, host, port = self.origin_of(url)
        key = f"{scheme}://{host}:{port}"
        wait_ms = self.limiter.wait(key) * 1000
        return self._request(url, kind=kind, conditional=conditional, wait_ms=wait_ms)

    # -- robots.txt ---------------------------------------------------
    def fetch_no_redirect(self, url: str, *, kind: str) -> FetchResult:
        """Fetch exactly this URL; a 3xx is returned, never followed."""
        return self._spaced_request(url, kind=kind, conditional={})

    # -- main entry ---------------------------------------------------
    def fetch(self, url: str, *, kind: str, etag: str | None = None,
              last_modified: str | None = None) -> FetchResult:
        conditional: dict[str, str] = {}
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified

        current = url
        chain: list[dict] = []
        visited = {self._norm(url)}
        for hop in range(self.source.max_redirects + 1):
            try:
                self.origin_of(current)
            except OffOrigin as exc:
                self._log(url=current, kind=kind, status=None, outcome="origin_blocked",
                          wait_ms=0.0, requested=False, note=str(exc),
                          redirect_from=chain[-1]["from"] if chain else None)
                raise
            if not self.robots.allowed(current, self.user_agent):
                self._log(url=current, kind=kind, status=None,
                          outcome="robots_disallowed", wait_ms=0.0, requested=False,
                          note="blocked by robots.txt",
                          redirect_from=chain[-1]["from"] if chain else None)
                raise OffOrigin(f"{current}: disallowed by robots.txt")

            response = self._spaced_request(current, kind=kind,
                                           conditional=conditional)
            location = response.headers.get("location", "")
            if response.status not in REDIRECT_STATUSES or not location:
                response.requested_url = url
                response.final_url = current
                response.redirect_chain = chain
                return response

            try:
                target_abs = urljoin(current, location)
                target = normalize_url(target_abs, self.source.tracking_params)
            except InvalidURL as exc:
                self._log(url=location, kind=kind, status=None,
                          outcome="redirect_blocked", wait_ms=0.0, requested=False,
                          note=f"invalid Location: {exc}", redirect_from=current)
                raise RedirectBlocked(
                    f"{current}: invalid redirect target {location!r}") from exc

            try:
                self.origin_of(target)
            except OffOrigin as exc:
                self._log(url=target, kind=kind, status=None,
                          outcome="redirect_blocked", wait_ms=0.0, requested=False,
                          note=f"out-of-scope redirect target: {exc}", redirect_from=current)
                raise RedirectBlocked(
                    f"{current} -> {target}: redirect target outside allowed origins"
                ) from exc

            if not self.robots.allowed(target, self.user_agent):
                self._log(url=target, kind=kind, status=None,
                          outcome="redirect_blocked", wait_ms=0.0, requested=False,
                          note="redirect target disallowed by robots.txt",
                          redirect_from=current)
                raise RedirectBlocked(f"{current} -> {target}: robots-disallowed target")

            chain.append({"from": current, "to": target,
                          "status": response.status})
            if target in visited:
                self._log(url=target, kind=kind, status=None, outcome="redirect_loop",
                          wait_ms=0.0, requested=False,
                          note="redirect loop", redirect_from=current)
                raise RedirectBlocked(f"{current}: redirect loop back to {target}")
            if hop >= self.source.max_redirects:
                self._log(url=target, kind=kind, status=None,
                          outcome="redirect_budget_exhausted", wait_ms=0.0,
                          requested=False,
                          note=f"max_redirects={self.source.max_redirects}",
                          redirect_from=current)
                raise RedirectBlocked(
                    f"{url}: exceeded max_redirects={self.source.max_redirects}")
            visited.add(target)
            current = target

        raise RedirectBlocked(f"{url}: exceeded max_redirects={self.source.max_redirects}")

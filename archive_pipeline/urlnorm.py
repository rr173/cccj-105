"""URL normalization for crawler-frontier identities.

Policy (documented in the README as well):

* scheme and host are lower-cased; http/https are treated as distinct;
* default ports (80/443) are never emitted; explicit non-default ports stay;
* the fragment is dropped (it never selects a different resource on the server);
* only query keys named in the source's configured ``tracking_params`` are
  removed, matched case-insensitively; every other query pair is kept;
* remaining query pairs are sorted by (key, value) so parameter order cannot
  produce duplicates, while different business parameters stay distinct;
* an empty query/path is normalized consistently (``?`` and trailing ``/`` on
  the authority-only URL are removed).

Normalization is a *identity* operation: two URLs that normalize to the same
string are considered the same document.  Business parameters are never
merged, because only explicitly configured tracking keys are dropped.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


class InvalidURL(ValueError):
    pass


def split_origin(url: str) -> tuple[str, str, int]:
    """Return (scheme, host, port) with defaults applied; validates http(s)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise InvalidURL(f"unsupported scheme {scheme!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidURL("URL has no host")
    if parts.username or parts.password:
        raise InvalidURL("URL with userinfo is not accepted")
    port = parts.port or _DEFAULT_PORTS[scheme]
    return scheme, host, port


def normalize_url(url: str, tracking_params: frozenset[str] | set[str] = frozenset()) -> str:
    """Normalize an absolute URL for frontier identity.

    Raises InvalidURL for non-http(s), host-less, or userinfo URLs.
    """
    parts = urlsplit(url.strip())
    scheme, host, port = split_origin(url)
    default_port = _DEFAULT_PORTS[scheme]

    netloc = host if port == default_port else f"{host}:{port}"

    path = parts.path or "/"
    # urlsplit already percent-decodes nothing; keep escaped bytes as sent.
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for (k, v) in pairs if k.lower() not in tracking_params]
    kept.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(kept, doseq=True, safe="")

    return urlunsplit((scheme, netloc, path, query, ""))


def query_keys(url: str) -> list[str]:
    return [k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)]


def path_of(url: str) -> str:
    return urlsplit(url).path or "/"


def same_resource(a: str, b: str, tracking_params: frozenset[str]) -> bool:
    return normalize_url(a, tracking_params) == normalize_url(b, tracking_params)

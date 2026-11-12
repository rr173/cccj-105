"""Stable archive identity, independent of the notice number printed on a page.

The problem this module fixes
------------------------------
The HTML of a detail page publishes a business number (``data-key`` /
``data-ref`` / 编号 / Ref).  Two *different business variants* of a notice —
for example a full edition at ``/notices/x/A-1002?view=full`` and a public
edition at ``/notices/x/A-1002?edition=public`` — can print the **same**
number.  Treating that number as the archive identity makes the two variants
share one version chain, so the later observation is appended as a "new
version" of the other and its current content is overwritten.

Identity rules (applied identically at ingestion, version lookup, conditional
reuse and retraction):

*``notice_key``
    The number exactly as published by the page (``A-1002``).  It is retained
    on every version for display and lookup, but it is never the unique
    archive identity.

*``url_key``
    The normalized request URL.  Tracking parameters and fragments are removed
    by :mod:`urlnorm`; every remaining *business* query parameter participates
    in the key, so ``?edition=public`` is a different key from the plain URL.

*``archive_id``
    The stable, queryable archive identity of a business variant within one
    source.  It is derived deterministically from the normalized **final**
    URL of the resource (the URL after real HTTP redirects have been
    followed).  Tracking aliases therefore never create a new identity, while
    different business parameters always do.

*real redirects vs. declared canonicals*
    A real HTTP 3xx (recorded in ``url_aliases``) maps the requested URL onto
    the target's ``archive_id`` — an alias does not create a document.  A
    ``rel=canonical`` declared inside HTML is only recorded; it can never
    merge variants distinguished by business parameters, so it is not part of
    identity computation at all.

The derivation is a plain SHA-256 over the source id and the normalized final
URL, which keeps ids stable across processes, free of primary-key
reassignment, and safe to compute before any database row exists.
"""

from __future__ import annotations

import hashlib

ID_PREFIX = "arc"


def derive_archive_id(source_id: str, final_url_key: str) -> str:
    """Return the stable archive id for a (source, normalized final URL).

    >>> derive_archive_id("sim_a", "http://h/notices/x/A-1002") == \\
    ... derive_archive_id("sim_a", "http://h/notices/x/A-1002")
    True
    >>> derive_archive_id("sim_a", "http://h/x?edition=public") != \\
    ... derive_archive_id("sim_a", "http://h/x")
    True
    """
    digest = hashlib.sha256(
        f"{source_id}\n{final_url_key}".encode("utf-8")).hexdigest()
    return f"{ID_PREFIX}_{digest[:32]}"


def resolve_final_url(
    url_key: str,
    aliases: dict[str, str],
) -> str:
    """Resolve a possibly-aliased request URL to its normalized final URL.

    ``aliases`` maps a normalized request URL to the normalized target URL of
    a *real* HTTP redirect.  Chains are followed with loop protection.  A URL
    that is not an alias resolves to itself.
    """
    current = url_key
    seen: set[str] = set()
    while current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def resolve_archive_id(
    source_id: str,
    url_key: str,
    aliases: dict[str, str],
) -> tuple[str, str]:
    """Return ``(archive_id, final_url_key)`` for a request URL."""
    final_url_key = resolve_final_url(url_key, aliases)
    return derive_archive_id(source_id, final_url_key), final_url_key

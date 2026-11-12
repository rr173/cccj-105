"""Repair of pre-identity databases whose variant histories were merged.

A legacy database keyed versions by the page-published notice number
(``document_key``).  When two business variants printed the same number their
observations were appended onto one version chain, so neither variant kept an
independent, ordered history.

This module rebuilds the variant attribution using only durable evidence that
already exists in the database and the immutable raw archive:

* every legacy ``versions`` row records the exact normalized ``url_key`` the
  version was observed at;
* every legacy ``redirects`` row records a *real* HTTP redirect, so alias URLs
  can be resolved to their target identity instead of creating documents;
* ``scan_items`` / ``scan_misses`` / ``scans`` retain the per-scan facts
  (including incomplete scans) used to rebuild 404 and retraction state.

The repair never touches the content-addressed raw archive on disk and never
deletes a scan fact.  Original legacy rows are renamed to ``*_legacy`` tables
inside the apply transaction, and every new ``versions`` row keeps
``legacy_version_no`` plus the original ``url_key`` so any old reference stays
traceable.

Workflow
--------
1. :func:`build_plan` opens the database read-only and returns a full plan,
   including every proposed identity/version move and a separate
   ``uncertain`` list.  It performs no writes.
2. :func:`apply_plan` runs the whole migration in one SQLite transaction.  A
   failure (including an injected test fault) rolls back completely: the
   database is reopened in its pre-migration shape and the repair can be run
   again safely.  Re-applying a repaired database is a no-op that creates no
   identities, versions or splits a second time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .identity import derive_archive_id, resolve_final_url
from .store import Store, _INDEX_SPECS

SCHEMA_VERSION = 2


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PlannedIdentity:
    source_id: str
    archive_id: str
    final_url_key: str
    notice_keys: list[str] = field(default_factory=list)
    version_rows: list[dict] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    uncertain: bool = False
    uncertainty_reasons: list[str] = field(default_factory=list)


@dataclass
class RepairPlan:
    is_legacy: bool
    sources: list[str]
    identities: list[PlannedIdentity] = field(default_factory=list)
    uncertain: list[dict] = field(default_factory=list)
    legacy_version_count: int = 0
    rebuilt_version_count: int = 0
    miss_count: int = 0
    retractions: list[dict] = field(default_factory=list)
    missing_archive_blobs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "is_legacy": self.is_legacy,
            "sources": self.sources,
            "legacy_version_count": self.legacy_version_count,
            "rebuilt_version_count": self.rebuilt_version_count,
            "miss_count": self.miss_count,
            "identities": [
                {
                    "source_id": i.source_id,
                    "archive_id": i.archive_id,
                    "final_url_key": i.final_url_key,
                    "notice_keys": i.notice_keys,
                    "aliases": i.aliases,
                    "uncertain": i.uncertain,
                    "uncertainty_reasons": i.uncertainty_reasons,
                    "versions": [
                        {"legacy_version_id": v["version_id"],
                         "legacy_version_no": v["version_no"],
                         "notice_key": v.get("document_key"),
                         "url_key": v["url_key"], "scan_id": v["scan_id"],
                         "raw_digest": v["raw_digest"],
                         "business_digest": v["business_digest"]}
                        for v in i.version_rows],
                }
                for i in self.identities
            ],
            "uncertain_records": self.uncertain,
            "retractions": self.retractions,
            "missing_archive_blobs": self.missing_archive_blobs,
        }


def _read_legacy(store: Store) -> dict:
    conn = store.conn
    data = {"versions": [], "redirects": [], "misses": [], "frontier": [],
            "documents": [], "scans": [], "items": []}
    data["versions"] = [dict(r) for r in conn.execute(
        "SELECT * FROM versions ORDER BY scan_id, version_id")]
    if store.table_exists("redirects"):
        data["redirects"] = [dict(r) for r in conn.execute(
            "SELECT * FROM redirects")]
    if store.table_exists("scan_misses"):
        data["misses"] = [dict(r) for r in conn.execute(
            "SELECT * FROM scan_misses")]
    if store.table_exists("url_frontier"):
        data["frontier"] = [dict(r) for r in conn.execute(
            "SELECT * FROM url_frontier")]
    if store.table_exists("document_meta"):
        data["documents"] = [dict(r) for r in conn.execute(
            "SELECT * FROM document_meta")]
    data["scans"] = [dict(r) for r in conn.execute(
        "SELECT scan_id, source_id, complete, status FROM scans")]
    data["items"] = [dict(r) for r in conn.execute(
        "SELECT scan_item_id, scan_id, source_id, url_key, document_key, "
        "outcome, http_status, raw_digest FROM scan_items")]
    return data


def build_plan(store: Store, archive) -> RepairPlan:
    """Read-only analysis of a legacy (or already repaired) database."""
    if not store.is_legacy():
        return RepairPlan(is_legacy=False, sources=[],
                          legacy_version_count=_count_versions(store))
    data = _read_legacy(store)
    plan = RepairPlan(is_legacy=True,
                      sources=sorted({v["source_id"] for v in data["versions"]}
                                     | {f["source_id"] for f in data["frontier"]}))
    plan.legacy_version_count = len(data["versions"])

    alias_maps: dict[str, dict[str, str]] = {}
    for row in data["redirects"]:
        alias_maps.setdefault(row["source_id"], {})[row["from_key"]] = row["to_key"]

    # Group every observed version by the identity of its *final* URL.  Real
    # redirects collapse onto the target; nothing else merges URLs.
    grouped: dict[tuple[str, str], PlannedIdentity] = {}
    conflicting_notice: dict[tuple[str, str], set[str]] = {}
    for version in data["versions"]:
        source_id = version["source_id"]
        aliases = alias_maps.get(source_id, {})
        final_url = resolve_final_url(version["url_key"], aliases)
        archive_id = derive_archive_id(source_id, final_url)
        key = (source_id, archive_id)
        planned = grouped.get(key)
        if planned is None:
            planned = PlannedIdentity(source_id, archive_id, final_url)
            grouped[key] = planned
        if version["url_key"] != final_url:
            planned.aliases.append(version["url_key"])
        notice = version.get("document_key")
        if notice and notice not in planned.notice_keys:
            planned.notice_keys.append(notice)
            if len(planned.notice_keys) > 1:
                conflicting_notice.setdefault(key, set()).update(
                    planned.notice_keys)
        planned.version_rows.append(version)

    # Frontier-only URLs (observed but never yielding a version, e.g. a
    # persistent 404) still need an identity so retraction evidence survives.
    for row in data["frontier"]:
        source_id = row["source_id"]
        aliases = alias_maps.get(source_id, {})
        final_url = resolve_final_url(row["url_key"], aliases)
        archive_id = derive_archive_id(source_id, final_url)
        key = (source_id, archive_id)
        planned = grouped.get(key)
        if planned is None:
            planned = PlannedIdentity(source_id, archive_id, final_url)
            grouped[key] = planned
        if row.get("document_key") and row["document_key"] not in planned.notice_keys:
            planned.notice_keys.append(row["document_key"])
        if row["url_key"] != final_url:
            planned.aliases.append(row["url_key"])

    # Evidence checks: raw blobs must exist; ordering must be reconstructable.
    blob_present: dict[str, bool] = {}

    def has_blob(digest: str) -> bool:
        if digest not in blob_present:
            blob_present[digest] = archive.metadata(digest) is not None
        return blob_present[digest]

    for planned in grouped.values():
        rows = sorted(planned.version_rows,
                      key=lambda r: (r["scan_id"], r["version_id"]))
        planned.version_rows = rows
        reasons: list[str] = []
        missing = [v["raw_digest"] for v in rows if not has_blob(v["raw_digest"])]
        if missing:
            reasons.append("missing_raw_archive:" + ",".join(sorted(set(missing))))
        seen_nos = [v["version_no"] for v in rows]
        if len(seen_nos) != len(set(seen_nos)):
            # A shared legacy chain can legitimately repeat numbers per
            # variant after re-numbering; that is resolved below, not an error.
            pass
        if (planned.source_id, planned.archive_id) in conflicting_notice:
            reasons.append("conflicting_published_notice_keys:"
                           + ",".join(sorted(conflicting_notice[
                               (planned.source_id, planned.archive_id)])))
        if not rows:
            reasons.append("no_archived_version_frontier_only")
        planned.uncertain = bool(reasons)
        planned.uncertainty_reasons = reasons
        plan.identities.append(planned)

    plan.identities.sort(key=lambda p: (p.source_id, p.final_url_key))
    plan.rebuilt_version_count = sum(len(p.version_rows) for p in plan.identities)

    for planned in plan.identities:
        if planned.uncertain:
            plan.uncertain.append({
                "source_id": planned.source_id,
                "archive_id": planned.archive_id,
                "final_url_key": planned.final_url_key,
                "reasons": planned.uncertainty_reasons,
                "legacy_version_ids": [v["version_id"] for v in planned.version_rows],
            })
    plan.missing_archive_blobs = [u for u in plan.uncertain
                                  if any(r.startswith("missing_raw_archive")
                                         for r in u["reasons"])]

    # Rebuild 404/retraction evidence from the legacy facts.
    complete_scans = {s["scan_id"] for s in data["scans"] if s["complete"]}
    miss_count = 0
    for miss in data["misses"]:
        aliases = alias_maps.get(miss["source_id"], {})
        final_url = resolve_final_url(miss["url_key"], aliases)
        archive_id = derive_archive_id(miss["source_id"], final_url)
        entry = {
            "source_id": miss["source_id"], "archive_id": archive_id,
            "notice_key": miss.get("document_key"),
            "url_key": miss["url_key"], "scan_id": miss["scan_id"],
            "http_status": miss["http_status"],
            "in_complete_scan": miss["scan_id"] in complete_scans,
        }
        plan.retractions.append(entry)
        miss_count += 1
    plan.miss_count = miss_count
    return plan


def _count_versions(store: Store) -> int:
    row = store.conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()
    return int(row["n"]) if row else 0


def apply_plan(store: Store, plan: RepairPlan, *,
               fail_after_sql: str | None = None) -> dict:
    """Apply a plan atomically. Returns a summary; raises on failure.

    ``fail_after_sql`` is a test hook: the named SQL statement, when first
    executed inside the migration transaction, raises ``RuntimeError`` so the
    rollback/no-half-state guarantee can be proven deterministically.
    """
    if not plan.is_legacy:
        return {"status": "noop", "reason": "database already at identity schema",
                "identities": 0, "versions": 0}

    conn = store.conn
    fault = {"armed": fail_after_sql is not None, "name": fail_after_sql}

    def maybe_fault(name: str) -> None:
        if fault["armed"] and fault["name"] == name:
            fault["armed"] = False
            raise RuntimeError(f"injected failure after {name}")

    run_id = store.start_repair_run(now_iso(), plan.to_dict())
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:            # 1) preserve the original tables verbatim, inside the transaction.
            conn.execute("ALTER TABLE versions RENAME TO versions_legacy")
            maybe_fault("rename_versions")
            conn.execute("ALTER TABLE scan_misses RENAME TO scan_misses_legacy")
            conn.execute("ALTER TABLE url_frontier RENAME TO url_frontier_legacy")
            conn.execute("ALTER TABLE document_meta RENAME TO document_meta_legacy")
            maybe_fault("rename_frontier")
            conn.execute("ALTER TABLE redirects RENAME TO redirects_legacy")

            # 2) recreate current-schema tables (SCHEMA already created the
            #    others at Store construction).  Individual execute() calls are
            #    mandatory here: sqlite3's executescript() issues an implicit
            #    COMMIT, which would break the all-or-nothing transaction.
            for statement in _CURRENT_TABLES_STATEMENTS:
                conn.execute(statement)
            # scan_items survives the rename-free path: add the identity
            # column to the existing table.
            if not any(r["name"] == "archive_id"
                       for r in conn.execute("PRAGMA table_info(scan_items)")):
                conn.execute("ALTER TABLE scan_items ADD COLUMN archive_id TEXT")
            maybe_fault("recreate_tables")

            # 3) aliases first, so URL identity rows can reference them.
            for row in conn.execute("SELECT * FROM redirects_legacy"):
                conn.execute(
                    "INSERT INTO url_aliases(source_id, from_key, to_key, scan_id) "
                    "VALUES (?,?,?,?)",
                    (row["source_id"], row["from_key"], row["to_key"], row["scan_id"]))

            # 4) one document identity + url mapping per planned variant.
            for planned in plan.identities:
                evidence = ("quarantined" if planned.uncertain else "confirmed")
                state = "quarantined" if planned.uncertain else "active"
                notice = planned.notice_keys[0] if planned.notice_keys else None
                conn.execute(
                    "INSERT INTO document_identities(source_id, archive_id, notice_key, "
                    "state, evidence_state, created_at) VALUES (?,?,?,?,?,?)",
                    (planned.source_id, planned.archive_id, notice, state,
                     evidence, now_iso()))
                if planned.uncertain:
                    conn.execute(
                        "INSERT INTO quarantine_notes(source_id, archive_id, reason, "
                        "detail_json, created_at) VALUES (?,?,?,?,?)",
                        (planned.source_id, planned.archive_id,
                         ";".join(planned.uncertainty_reasons),
                         json.dumps({"final_url_key": planned.final_url_key,
                                     "legacy_version_ids": [v["version_id"]
                                                            for v in planned.version_rows]},
                                    ensure_ascii=False),
                         now_iso()))
                # Final URL row plus every observed alias URL row.
                url_rows = {planned.final_url_key}
                url_rows.update(planned.aliases)
                url_rows.update(v["url_key"] for v in planned.version_rows)
                for url in sorted(url_rows):
                    is_alias = int(url != planned.final_url_key)
                    conn.execute(
                        "INSERT INTO url_identities(source_id, url_key, archive_id, "
                        "final_url_key, is_alias, state, updated_at) "
                        "VALUES (?,?,?,?,?, 'processed', ?)",
                        (planned.source_id, url, planned.archive_id,
                         planned.final_url_key, is_alias, now_iso()))

            maybe_fault("insert_identities")

            # 5) versions: each variant keeps its own modification order,
            #    re-numbered 1..n within the variant; legacy numbers retained.
            for planned in plan.identities:
                for new_no, legacy in enumerate(planned.version_rows, start=1):
                    conn.execute(
                        "INSERT INTO versions(source_id, archive_id, notice_key, "
                        "document_key, url_key, version_no, legacy_version_no, title, "
                        "issued_on, body, canonical_url, raw_digest, content_digest, "
                        "business_digest, scan_id, created_at) "
                        "SELECT ?, ?, document_key, document_key, url_key, ?, ?, title, "
                        "issued_on, body, canonical_url, raw_digest, content_digest, "
                        "business_digest, scan_id, created_at "
                        "FROM versions_legacy WHERE version_id=?",
                        (planned.source_id, planned.archive_id, new_no,
                         legacy["version_no"], legacy["version_id"]))

            maybe_fault("insert_versions")

            # 6) scan items: annotate with the resolved identity; original
            #    document_key/url columns stay untouched.
            for item in conn.execute(
                    "SELECT scan_item_id, source_id, url_key FROM scan_items"):
                aliases = {r["from_key"]: r["to_key"] for r in conn.execute(
                    "SELECT from_key, to_key FROM url_aliases WHERE source_id=?",
                    (item["source_id"],))}
                final_url = resolve_final_url(item["url_key"], aliases)
                archive_id = derive_archive_id(item["source_id"], final_url)
                conn.execute(
                    "UPDATE scan_items SET archive_id=? WHERE scan_item_id=?",
                    (archive_id, item["scan_item_id"]))

            # 7) miss evidence, rebuilt against identities.
            for entry in plan.retractions:
                conn.execute(
                    "INSERT INTO scan_misses(scan_id, source_id, archive_id, "
                    "notice_key, url_key, http_status) VALUES (?,?,?,?,?,?)",
                    (entry["scan_id"], entry["source_id"], entry["archive_id"],
                     entry["notice_key"], entry["url_key"], entry["http_status"]))

            # 8) document state from legacy meta where attribution is unique.
            for meta in conn.execute("SELECT * FROM document_meta_legacy"):
                # Legacy meta is keyed by notice number.  Map it only when the
                # number now resolves to exactly one confirmed identity.
                matches = conn.execute(
                    "SELECT archive_id FROM document_identities WHERE source_id=? "
                    "AND notice_key=? AND evidence_state='confirmed'",
                    (meta["source_id"], meta["document_key"])).fetchall()
                if len(matches) != 1:
                    continue
                archive_id = matches[0]["archive_id"]
                conn.execute(
                    "UPDATE document_identities SET state=?, missing_streak=?, "
                    "retracted_at=?, retracted_scan=?, previous_state=?, last_url_key=? "
                    "WHERE source_id=? AND archive_id=?",
                    (meta["state"], meta["missing_streak"], meta["retracted_at"],
                     meta["retracted_scan"], meta["previous_state"],
                     meta["last_url_key"], meta["source_id"], archive_id))

            # 9) frontier validator state carried onto the final URL row.
            for row in conn.execute("SELECT * FROM url_frontier_legacy"):
                aliases = {r["from_key"]: r["to_key"] for r in conn.execute(
                    "SELECT from_key, to_key FROM url_aliases WHERE source_id=?",
                    (row["source_id"],))}
                final_url = resolve_final_url(row["url_key"], aliases)
                conn.execute(
                    "UPDATE url_identities SET state=?, missing_streak=?, last_status=?, "
                    "etag=?, last_modified=?, last_version=?, content_digest=?, "
                    "canonical_url=?, notice_key=?, discovered_scan=? "
                    "WHERE source_id=? AND url_key=?",
                    (row["state"], row["missing_streak"], row["last_status"],
                     row["etag"], row["last_modified"], row["last_version"],
                     row["content_digest"], row["canonical_url"],
                     row["document_key"], row["discovered_scan"],
                     row["source_id"], final_url))

            maybe_fault("copy_frontier_state")
            # Identity-column indexes can now be (re)created on the new tables.
            for _index_name, table, _column, sql in _INDEX_SPECS:
                if any(r["name"] == table for r in
                       conn.execute("SELECT name FROM sqlite_master "
                                    "WHERE type='table' AND name=?", (table,))):
                    conn.execute(sql)
            # Mark the schema level inside the same transaction: if anything
            # above failed this rolls back together with the table changes.
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            # A statement error aborts the transaction implicitly in some
            # SQLite builds; only roll back while one is still active.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except Exception as exc:
        store.finish_repair_run(run_id, now_iso(), "failed", repr(exc))
        raise
    store.finish_repair_run(run_id, now_iso(), "applied",
                            f"{len(plan.identities)} identities, "
                            f"{plan.rebuilt_version_count} versions")
    return {
        "status": "applied", "run_id": run_id,
        "identities": len(plan.identities),
        "confirmed_identities": sum(1 for p in plan.identities if not p.uncertain),
        "quarantined_identities": sum(1 for p in plan.identities if p.uncertain),
        "versions": plan.rebuilt_version_count,
        "misses": plan.miss_count,
    }


_CURRENT_TABLES_STATEMENTS = (
    """CREATE TABLE versions (
    version_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         TEXT NOT NULL,
    archive_id        TEXT NOT NULL,
    notice_key        TEXT,
    document_key      TEXT,
    url_key           TEXT NOT NULL,
    version_no        INTEGER NOT NULL,
    legacy_version_no INTEGER,
    title             TEXT NOT NULL,
    issued_on         TEXT NOT NULL,
    body              TEXT NOT NULL,
    canonical_url     TEXT,
    raw_digest        TEXT NOT NULL,
    content_digest    TEXT NOT NULL,
    business_digest   TEXT NOT NULL,
    scan_id           INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (source_id, archive_id, version_no)
)""",
    """CREATE TABLE scan_misses (
    scan_id        INTEGER NOT NULL,
    source_id      TEXT NOT NULL,
    archive_id     TEXT NOT NULL,
    notice_key     TEXT,
    url_key        TEXT NOT NULL,
    http_status    INTEGER NOT NULL,
    PRIMARY KEY (scan_id, archive_id, url_key)
)""",
    "CREATE INDEX idx_versions_arc ON versions(source_id, archive_id, version_no)",
    "CREATE INDEX idx_versions_notice ON versions(source_id, notice_key)",
    "CREATE INDEX idx_misses_arc ON scan_misses(source_id, archive_id)",
)

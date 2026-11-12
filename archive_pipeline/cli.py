"""Command-line entry point for the archive pipeline.

    python -m archive_pipeline.cli scan --source sim_a
    python -m archive_pipeline.cli scan --all
    python -m archive_pipeline.cli status
    python -m archive_pipeline.cli show-scan 15
    python -m archive_pipeline.cli versions --source sim_a
    python -m archive_pipeline.cli diff --source sim_a --document A-1002
    python -m archive_pipeline.cli identities --source sim_a
    python -m archive_pipeline.cli repair --preview            # no writes
    python -m archive_pipeline.cli repair --apply              # explicit apply

Identity note
-------------
The page-published notice number (``--document``) is not unique once business
variants exist.  When a number maps to more than one variant the lookup prints
every candidate with its stable ``archive_id`` and distinguishing URL instead
of silently picking one; pass ``--archive-id`` to address an exact variant.

The CLI operates on the persistent state in ``work/`` (or ``--workdir``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .archive import RawArchive
from .config import load_config
from .engine import ScanEngine
from .repair import apply_plan, build_plan
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "sources.json"


def _open(args):
    config = load_config(args.config)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    store = Store(workdir / "pipeline.sqlite3")
    archive = RawArchive(workdir / "archive")
    return config, store, archive


def cmd_scan(args) -> int:
    config, store, archive = _open(args)
    if store.is_legacy():
        print(
            "error: this database uses the pre-identity schema with merged "
            "variant histories.\nRun 'python -m archive_pipeline.cli repair "
            "--preview' then 'repair --apply' before scanning.",
            file=sys.stderr)
        store.close()
        return 3
    engine = ScanEngine(config, store, archive, progress=print)
    targets = [s.source_id for s in config.sources] if args.all else args.source
    if not targets:
        raise SystemExit("specify --source ID or --all")
    failures = 0
    for source_id in targets:
        source = config.source(source_id)
        result = engine.scan(source, interrupt_after=args.interrupt_after)
        summary = {
            "scan_id": result.scan_id,
            "source": source_id,
            "complete": result.complete,
            "paging_loop": result.paging_loop,
            "stop_reason": result.stop_reason,
            "details": {i["url_key"]: {"outcome": i["outcome"],
                                       "version_created": i["version_created"],
                                       "archive_id": i.get("archive_id")}
                        for i in result.detail_items},
            "notes": result.notes,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not result.complete:
            failures += 1
    store.close()
    return 1 if failures else 0


def cmd_status(args) -> int:
    config, store, archive = _open(args)
    payload = {"schema": "legacy" if store.is_legacy() else "identity",
               "sources": {}}
    for source in config.sources:
        scans = store.scans(source.source_id)
        payload["sources"][source.source_id] = {
            "scans": [{k: scan[k] for k in
                       ("scan_id", "complete", "status", "stop_reason",
                        "page_count", "detail_count")} for scan in scans],
            "identities": store.identities(source.source_id),
            "pending_urls": [row["url_key"]
                             for row in store.pending_urls(source.source_id)],
            "versions": len(store.all_versions(source.source_id)),
            "quarantine": [n for n in store.quarantine_notes()
                           if n["source_id"] == source.source_id],
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_show_scan(args) -> int:
    config, store, archive = _open(args)
    scan = store.scan(args.scan_id)
    if scan is None:
        print(f"scan {args.scan_id} not found", file=sys.stderr)
        return 2
    scan["requests"] = json.loads(scan.pop("requests_json"))
    scan["pages"] = json.loads(scan.pop("pages_json"))
    scan["items"] = store.scan_items(args.scan_id)
    for item in scan["items"]:
        item["requests"] = json.loads(item.pop("requests_json"))
        item["detail"] = json.loads(item.pop("detail_json"))
    print(json.dumps(scan, ensure_ascii=False, indent=2, default=str))
    store.close()
    return 0


def _resolve_identities(store: Store, source_id: str, document: str | None,
                        archive_id: str | None) -> tuple[list[dict], int]:
    """Return (identities, exit_code). Never silently picks one of several."""
    if archive_id:
        identity = store.get_identity(source_id, archive_id)
        if identity is None:
            print(f"no identity {archive_id} in {source_id}", file=sys.stderr)
            return [], 2
        return [identity], 0
    if not document:
        raise SystemExit("specify --document NOTICE_KEY or --archive-id ID")
    matches = store.identities_for_notice(source_id, document)
    if not matches:
        # The number may exist on versions even without an identity row.
        versions = store.versions_for_notice(source_id, document)
        if not versions:
            print(f"no variants published notice number {document} in {source_id}",
                  file=sys.stderr)
            return [], 2
        ids = sorted({v["archive_id"] for v in versions})
        matches = [store.get_identity(source_id, aid) for aid in ids]
        matches = [m for m in matches if m]
    if len(matches) > 1:
        print(f"notice number {document!r} is published by {len(matches)} business "
              f"variants in {source_id}; refusing to pick one:\n", file=sys.stderr)
        for identity in matches:
            last = store.last_version(source_id, identity["archive_id"])
            url = last["url_key"] if last else identity["last_url_key"]
            print(f"  --archive-id {identity['archive_id']}  "
                  f"state={identity['state']}  url={url}", file=sys.stderr)
        print("\nre-run with one of the --archive-id values above.",
              file=sys.stderr)
        return matches, 4
    return matches, 0


def cmd_identities(args) -> int:
    config, store, archive = _open(args)
    for source in config.sources:
        if args.source and source.source_id != args.source:
            continue
        for identity in store.identities(source.source_id):
            last = store.last_version(source.source_id, identity["archive_id"])
            line = (f"{source.source_id} {identity['archive_id']} "
                    f"notice={identity['notice_key'] or '-'} "
                    f"[{identity['state']}/{identity['evidence_state']}] "
                    f"versions={len(store.versions_for(source.source_id, identity['archive_id']))}")
            if last:
                line += f" last_url={last['url_key']}"
            print(line)
    store.close()
    return 0


def cmd_versions(args) -> int:
    config, store, archive = _open(args)
    if args.document or args.archive_id:
        for source in config.sources:
            if args.source and source.source_id != args.source:
                continue
            matches, code = _resolve_identities(
                store, source.source_id, args.document, args.archive_id)
            if code:
                store.close()
                return code
            for identity in matches:
                _print_identity_versions(store, source.source_id, identity)
        store.close()
        return 0
    for source in config.sources:
        if args.source and source.source_id != args.source:
            continue
        for identity in store.identities(source.source_id):
            _print_identity_versions(store, source.source_id, identity)
    store.close()
    return 0


def _print_identity_versions(store: Store, source_id: str, identity: dict) -> None:
    print(f"{source_id} {identity['archive_id']} notice={identity['notice_key'] or '-'} "
          f"[{identity['state']}/{identity['evidence_state']}]")
    for version in store.versions_for(source_id, identity["archive_id"]):
        legacy = (f" legacy_v={version['legacy_version_no']}"
                  if version["legacy_version_no"] else "")
        print(f"  v{version['version_no']} scan={version['scan_id']}{legacy} "
              f"issued={version['issued_on']} title={version['title']}")
        print(f"     url={version['url_key']}")
        print(f"     raw={version['raw_digest']} "
              f"business={version['business_digest']}")


def cmd_diff(args) -> int:
    config, store, archive = _open(args)
    matches, code = _resolve_identities(
        store, args.source, args.document, args.archive_id)
    if code:
        store.close()
        return code
    from .engine import paragraph_diff
    for identity in matches:
        versions = store.versions_for(args.source, identity["archive_id"])
        if not versions:
            print(f"no versions for {identity['archive_id']}", file=sys.stderr)
            continue
        print(f"# {identity['archive_id']} (notice {identity['notice_key'] or '-'})")
        previous = None
        for version in versions:
            if previous is None:
                print(f"v{version['version_no']} (initial)")
            else:
                diff = paragraph_diff(previous["body"], version["body"])
                print(f"v{previous['version_no']} -> v{version['version_no']}")
                for paragraph in diff["paragraphs_removed"]:
                    print(f"  - {paragraph}")
                for paragraph in diff["paragraphs_added"]:
                    print(f"  + {paragraph}")
            previous = version
    store.close()
    return 0


def cmd_repair(args) -> int:
    config, store, archive = _open(args)
    plan = build_plan(store, archive)
    if not plan.is_legacy:
        print(json.dumps({
            "status": "noop",
            "reason": "database is already at the identity schema (or is empty); "
                      "nothing to repair",
            "repair_runs": store.repair_runs(),
        }, ensure_ascii=False, indent=2))
        store.close()
        return 0
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    if args.preview or not args.apply:
        print("\nPreview only: no rows were changed. Re-run with --apply to migrate.",
              file=sys.stderr)
        store.close()
        return 0
    try:
        summary = apply_plan(store, plan, fail_after_sql=args.fail_point)
    except Exception as exc:  # controlled or unexpected failure: rolled back
        print(json.dumps({"status": "failed_rolled_back", "error": repr(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        store.close()
        return 5
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archive_pipeline.cli")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--workdir", default=str(ROOT / "work"))
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="run a scan round")
    scan.add_argument("--source", action="append", default=[])
    scan.add_argument("--all", action="store_true")
    scan.add_argument("--interrupt-after", type=int, default=None,
                      help="stop after N detail fetches, leaving frontier pending")
    scan.set_defaults(func=cmd_scan)

    status = sub.add_parser("status", help="print scan/identity state")
    status.set_defaults(func=cmd_status)

    show = sub.add_parser("show-scan", help="dump one scan's manifest")
    show.add_argument("scan_id", type=int)
    show.set_defaults(func=cmd_show_scan)

    identities = sub.add_parser("identities", help="list stable variant identities")
    identities.add_argument("--source")
    identities.set_defaults(func=cmd_identities)

    versions = sub.add_parser("versions", help="list versions")
    versions.add_argument("--source")
    versions.add_argument("--document", help="page-published notice number")
    versions.add_argument("--archive-id", help="exact stable variant identity")
    versions.set_defaults(func=cmd_versions)

    diff = sub.add_parser("diff", help="print version diffs for a variant")
    diff.add_argument("--source", required=True)
    diff.add_argument("--document", help="page-published notice number")
    diff.add_argument("--archive-id", help="exact stable variant identity")
    diff.set_defaults(func=cmd_diff)

    repair = sub.add_parser("repair", help="repair merged legacy histories")
    repair.add_argument("--preview", action="store_true",
                        help="read-only plan (the default)")
    repair.add_argument("--apply", action="store_true",
                        help="apply the plan atomically")
    repair.add_argument("--fail-point",
                        help="test hook: fail after this migration SQL step")
    repair.set_defaults(func=cmd_repair)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

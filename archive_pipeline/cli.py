"""Command-line entry point for the archive pipeline.

    python -m archive_pipeline.cli scan --source sim_a
    python -m archive_pipeline.cli scan --all
    python -m archive_pipeline.cli status
    python -m archive_pipeline.cli show-scan 15
    python -m archive_pipeline.cli versions --source sim_a
    python -m archive_pipeline.cli diff --source sim_a --document A-1001

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
                                       "version_created": i["version_created"]}
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
    payload = {"sources": {}}
    for source in config.sources:
        scans = store.scans(source.source_id)
        documents = store.documents(source.source_id)
        payload["sources"][source.source_id] = {
            "scans": [{k: scan[k] for k in
                       ("scan_id", "complete", "status", "stop_reason",
                        "page_count", "detail_count")} for scan in scans],
            "documents": documents,
            "pending_urls": [row["url_key"]
                             for row in store.pending_urls(source.source_id)],
            "versions": len(store.all_versions(source.source_id)),
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


def cmd_versions(args) -> int:
    config, store, archive = _open(args)
    for source in config.sources:
        if args.source and source.source_id != args.source:
            continue
        for doc in store.documents(source.source_id):
            print(f"{source.source_id} {doc['document_key']} [{doc['state']}]")
            for version in store.versions_for(source.source_id, doc["document_key"]):
                print(f"  v{version['version_no']} scan={version['scan_id']} "
                      f"issued={version['issued_on']} title={version['title']}")
                print(f"     raw={version['raw_digest']} "
                      f"business={version['business_digest']}")
    store.close()
    return 0


def cmd_diff(args) -> int:
    config, store, archive = _open(args)
    versions = store.versions_for(args.source, args.document)
    if not versions:
        print(f"no versions for {args.source}/{args.document}", file=sys.stderr)
        return 2
    previous = None
    for version in versions:
        if previous is None:
            print(f"v{version['version_no']} (initial)")
        else:
            from .engine import paragraph_diff
            diff = paragraph_diff(previous["body"], version["body"])
            print(f"v{previous['version_no']} -> v{version['version_no']}")
            for paragraph in diff["paragraphs_removed"]:
                print(f"  - {paragraph}")
            for paragraph in diff["paragraphs_added"]:
                print(f"  + {paragraph}")
        previous = version
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

    status = sub.add_parser("status", help="print scan/document state")
    status.set_defaults(func=cmd_status)

    show = sub.add_parser("show-scan", help="dump one scan's manifest")
    show.add_argument("scan_id", type=int)
    show.set_defaults(func=cmd_show_scan)

    versions = sub.add_parser("versions", help="list versions")
    versions.add_argument("--source")
    versions.set_defaults(func=cmd_versions)

    diff = sub.add_parser("diff", help="print version diffs for a document")
    diff.add_argument("--source", required=True)
    diff.add_argument("--document", required=True)
    diff.set_defaults(func=cmd_diff)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

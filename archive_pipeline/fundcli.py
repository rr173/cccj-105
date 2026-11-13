"""CLI for the funding-table structuring job — independent of web scanning.

    python -m archive_pipeline.fundcli process [--source ID] [--archive-id ID]
    python -m archive_pipeline.fundcli rejections [--source ID] [--archive-id ID] [--history]
    python -m archive_pipeline.fundcli facts [--source ID] [--archive-id ID] \
        [--version N] [--table N] [--row N] [--year Y]

The job consumes the identity-schema pipeline database and the local raw
archive under ``--workdir`` (default ``work/``); it never contacts a site.
Table selection and header aliases come from ``--tables-config`` (default
``config/funding_tables.json``).  Results live in ``work/funding.sqlite3``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .funding import (
    DEFAULT_TABLES_CONFIG,
    FundingError,
    FundingInterrupted,
    FundingJob,
    FundingStore,
)

ROOT = Path(__file__).resolve().parent.parent

_JSON_FIELDS = ("grid_json", "candidates_json", "totals_json",
                "header_path_json", "row_header_path_json")


def _decode(row: dict) -> dict:
    out = dict(row)
    for field in _JSON_FIELDS:
        if field in out and isinstance(out[field], str) and out[field]:
            out[field] = json.loads(out[field])
    return out


def cmd_process(args) -> int:
    try:
        job = FundingJob(args.workdir, args.tables_config,
                         progress=lambda msg: print(msg, file=sys.stderr))
    except FundingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        summary = job.process(source_id=args.source, archive_id=args.archive_id)
    except FundingInterrupted as exc:
        job.close()
        print(f"interrupted: {exc}", file=sys.stderr)
        return 6
    job.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_rejections(args) -> int:
    db = Path(args.workdir) / "funding.sqlite3"
    if not db.exists():
        print("error: no funding database; run 'process' first", file=sys.stderr)
        return 2
    store = FundingStore(db)
    payload = {
        "version_errors": store.version_errors(args.source, args.archive_id),
        "rejected_tables": [_decode(o) for o in store.outcomes(
            args.source, args.archive_id, status="rejected")],
    }
    if args.history:
        payload["history"] = [_decode(h) for h in store.history(
            args.source, args.archive_id)]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_facts(args) -> int:
    db = Path(args.workdir) / "funding.sqlite3"
    if not db.exists():
        print("error: no funding database; run 'process' first", file=sys.stderr)
        return 2
    store = FundingStore(db)
    rows = store.facts(source_id=args.source, archive_id=args.archive_id,
                       version_no=args.version, table_index=args.table,
                       row_index=args.row, year=args.year)
    print(json.dumps([_decode(row) for row in rows], ensure_ascii=False, indent=2))
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archive_pipeline.fundcli")
    parser.add_argument("--workdir", default=str(ROOT / "work"))
    parser.add_argument("--tables-config", default=str(DEFAULT_TABLES_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    process = sub.add_parser("process", help="process archived versions into facts")
    process.add_argument("--source")
    process.add_argument("--archive-id")
    process.set_defaults(func=cmd_process)

    rejections = sub.add_parser(
        "rejections", help="show rejected tables and version-level errors")
    rejections.add_argument("--source")
    rejections.add_argument("--archive-id")
    rejections.add_argument("--history", action="store_true",
                            help="include every diagnostic ever produced")
    rejections.set_defaults(func=cmd_rejections)

    facts = sub.add_parser("facts", help="query booked funding facts")
    facts.add_argument("--source")
    facts.add_argument("--archive-id")
    facts.add_argument("--version", type=int)
    facts.add_argument("--table", type=int)
    facts.add_argument("--row", type=int)
    facts.add_argument("--year", type=int)
    facts.set_defaults(func=cmd_facts)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""CLI for the schedule-organizing job — independent of web scanning.

    python -m archive_pipeline.schedcli organize [--source ID] [--vocab PATH]
    python -m archive_pipeline.schedcli current [--project KEY] [--round N]
                                                [--on-date YYYY-MM-DD] [--explain]
    python -m archive_pipeline.schedcli changes --project KEY [--run N]
    python -m archive_pipeline.schedcli pending [--history] [--source ID]

The job consumes the identity-schema pipeline database and the local raw
archive under ``--workdir`` (default ``work/``) read-only; it never contacts
a site.  Vocabulary comes from ``--vocab`` (default
``config/schedule_vocab.json``).  Results live in ``work/schedule.sqlite3``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .schedule import (
    DEFAULT_VOCAB_CONFIG,
    ScheduleError,
    ScheduleInterrupted,
    ScheduleJob,
    ScheduleStore,
)

ROOT = Path(__file__).resolve().parent.parent


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_organize(args) -> int:
    try:
        job = ScheduleJob(args.workdir, args.vocab,
                          progress=lambda msg: print(msg, file=sys.stderr))
    except ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        fail_point = None
        if args.fail_point:
            fail_point = (args.fail_point[0], int(args.fail_point[1]))
        summary = job.organize(source_id=args.source,
                               fail_point=fail_point)
    except ScheduleInterrupted as exc:
        job.close()
        print(f"interrupted: {exc}", file=sys.stderr)
        return 6
    job.close()
    _print(summary)
    return 0


def _explain_node(store: ScheduleStore, node: dict) -> dict:
    detail = store.evidence_for_ledger(node["ledger_id"])
    out = dict(node)
    if detail:
        out["source_notice"] = {
            "source_id": detail["source_id"],
            "notice_key": detail["notice_key"],
            "archive_id": detail["archive_id"],
            "version_no": detail["version_no"],
            "issued_on": detail["issued_on"],
        }
        out["evidence"] = json.loads(detail["evidence_json"])
        out["matching_sentence_evidence"] = [
            ev for ev in detail["evidence"]
            if ev["role_name"] == node["role_name"]
            and ev["round_no"] == node["round_no"]
        ]
    return out


def cmd_current(args) -> int:
    db = Path(args.workdir) / "schedule.sqlite3"
    if not db.exists():
        print("error: no schedule database; run 'organize' first",
              file=sys.stderr)
        return 2
    store = ScheduleStore(db)
    nodes = store.current_nodes(project_key=args.project,
                                 round_no=args.round, on_date=args.on_date)
    if args.explain:
        nodes = [_explain_node(store, node) for node in nodes]
    payload = {
        "rounds": store.rounds(args.project, args.round),
        "nodes": nodes,
    }
    if args.on_date and not args.explain:
        payload["query"] = {"on_date": args.on_date,
                            "match_count": len(nodes)}
    _print(payload)
    store.close()
    return 0


def cmd_changes(args) -> int:
    db = Path(args.workdir) / "schedule.sqlite3"
    if not db.exists():
        print("error: no schedule database; run 'organize' first",
              file=sys.stderr)
        return 2
    store = ScheduleStore(db)
    rows = store.ledger(project_key=args.project, run_id=args.run)
    enriched = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(row["evidence_json"])
        enriched.append(item)
    _print({"project_key": args.project, "run_id": args.run,
            "changes": enriched})
    store.close()
    return 0


def cmd_pending(args) -> int:
    db = Path(args.workdir) / "schedule.sqlite3"
    if not db.exists():
        print("error: no schedule database; run 'organize' first",
              file=sys.stderr)
        return 2
    store = ScheduleStore(db)
    payload = {
        "open": store.pending(current_only=True, source_id=args.source),
    }
    if args.history:
        payload["history"] = store.pending(current_only=False,
                                           source_id=args.source)
    _print(payload)
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archive_pipeline.schedcli")
    parser.add_argument("--workdir", default=str(ROOT / "work"))
    parser.add_argument("--vocab", default=str(DEFAULT_VOCAB_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    organize = sub.add_parser(
        "organize", help="organize archived notices into schedule nodes")
    organize.add_argument("--source")
    organize.add_argument("--fail-point", nargs=2, metavar=("ARCHIVE_ID", "VERSION"),
                          help="test hook: interrupt inside one version's tx")
    organize.set_defaults(func=cmd_organize)

    current = sub.add_parser(
        "current", help="show the currently effective schedule")
    current.add_argument("--project")
    current.add_argument("--round", type=int)
    current.add_argument("--on-date", metavar="YYYY-MM-DD",
                         help="only nodes effective on this date")
    current.add_argument("--explain", action="store_true",
                         help="attach source notice, paragraph and spans")
    current.set_defaults(func=cmd_current)

    changes = sub.add_parser(
        "changes", help="version-scoped change history of one project")
    changes.add_argument("--project", required=True)
    changes.add_argument("--run", type=int)
    changes.set_defaults(func=cmd_changes)

    pending = sub.add_parser(
        "pending", help="material awaiting human confirmation")
    pending.add_argument("--history", action="store_true",
                         help="include every pending judgment ever recorded")
    pending.add_argument("--source")
    pending.set_defaults(func=cmd_pending)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

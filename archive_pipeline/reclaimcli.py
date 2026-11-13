"""CLI for raw-response space reclamation -- read-only analysis, explicit plans.

    python -m archive_pipeline.reclaimcli preview --cutoff 2026-11-12T00:00:00+00:00
    python -m archive_pipeline.reclaimcli plan --cutoff ...        # saves a plan
    python -m archive_pipeline.reclaimcli show --plan rcl_xxxx      # re-open a plan
    python -m archive_pipeline.reclaimcli isolate --plan rcl_xxxx   # move to quarantine
    python -m archive_pipeline.reclaimcli restore --plan rcl_xxxx   # undo, if not purged
    python -m archive_pipeline.reclaimcli purge --plan rcl_xxxx     # permanent delete
    python -m archive_pipeline.reclaimcli journal --plan rcl_xxxx   # per-file truth
    python -m archive_pipeline.reclaimcli plans                     # all saved plans

``--cutoff`` is the grace deadline: only files older than it (filesystem mtime
and metadata ``fetched_at``) can be reclaimable.  ``preview`` never writes.
``plan`` persists a re-openable snapshot but still moves no file.  Isolation
and purge hold the exclusive work-directory lock that collection holds shared,
so a scan that has written raw bytes but not yet committed its facts keeps
reclamation out.

Exit codes: 0 success; 2 usage/precondition error; 3 stale plan (a candidate
gained a reference or a file changed) -- nothing was moved; 4 conflict (some
files kept in both locations / purge blocked); 5 reference source unreadable --
nothing was done.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .reclaim import (
    PlanConflict,
    ReclaimCrash,
    ReclaimError,
    ReferenceError,
    StalePlan,
    create_plan,
    isolate_plan,
    list_plans,
    now_iso,
    open_plan,
    parse_iso,
    plan_journal,
    preview,
    purge_plan,
    restore_plan,
)

ROOT = Path(__file__).resolve().parent.parent


def _cutoff(args) -> "object":
    if not args.cutoff:
        raise SystemExit("--cutoff is required (ISO-8601 with an offset)")
    try:
        return parse_iso(args.cutoff)
    except ValueError as exc:
        raise SystemExit(f"invalid --cutoff: {exc}")


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_preview(args) -> int:
    try:
        payload = preview(args.workdir, args.archive, _cutoff(args))
    except ReferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 5
    except ReclaimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print(payload)
    return 0


def cmd_plan(args) -> int:
    try:
        payload = create_plan(args.workdir, args.archive, _cutoff(args))
    except ReferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 5
    except ReclaimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print(payload)
    return 0


def cmd_show(args) -> int:
    try:
        _print(open_plan(args.workdir, args.plan))
    except ReclaimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_journal(args) -> int:
    try:
        _print(plan_journal(args.workdir, args.plan))
    except ReclaimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_plans(args) -> int:
    _print({"plans": list_plans(args.workdir)})
    return 0


def cmd_isolate(args) -> int:
    try:
        payload = isolate_plan(args.workdir, args.plan,
                               interrupt_after_moves=args.interrupt_after_moves)
    except StalePlan as exc:
        _print({"status": "stale", "error": str(exc),
                "conflicts": exc.conflicts})
        return 3
    except ReferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 5
    except ReclaimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print(payload)
    return 0 if payload.get("status") in {"isolated", "already_isolated"} else 4


def cmd_restore(args) -> int:
    try:
        payload = restore_plan(args.workdir, args.plan)
    except ReclaimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print(payload)
    return 0 if payload.get("status") in {"restored", "already_restored"} else 4


def cmd_purge(args) -> int:
    try:
        payload = purge_plan(args.workdir, args.plan)
    except ReferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 5
    except ReclaimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print(payload)
    return 0 if payload.get("status") in {"purged", "already_purged"} else 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archive_pipeline.reclaimcli")
    parser.add_argument("--workdir", default=str(ROOT / "work"))
    parser.add_argument("--archive", default=None,
                        help="archive root (default <workdir>/archive)")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func, help_text in (
            ("preview", cmd_preview, "classify archive files (no writes)"),
            ("plan", cmd_plan, "save a re-openable plan (no file moves)")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--cutoff", required=True,
                             help="grace cutoff, ISO-8601 with offset")
        command.set_defaults(func=func)

    show = sub.add_parser("show", help="re-open a saved plan")
    show.add_argument("--plan", required=True)
    show.set_defaults(func=cmd_show)

    journal = sub.add_parser(
        "journal", help="per-file states and the action log of a plan")
    journal.add_argument("--plan", required=True)
    journal.set_defaults(func=cmd_journal)

    plans = sub.add_parser("plans", help="list saved plans")
    plans.set_defaults(func=cmd_plans)

    isolate = sub.add_parser("isolate", help="move candidates into quarantine")
    isolate.add_argument("--plan", required=True)
    isolate.add_argument("--interrupt-after-moves", type=int, default=None,
                         help="test hook: stop abruptly after N successful moves")
    isolate.set_defaults(func=cmd_isolate)

    restore = sub.add_parser("restore", help="undo an isolation")
    restore.add_argument("--plan", required=True)
    restore.set_defaults(func=cmd_restore)

    purge = sub.add_parser("purge", help="permanently delete quarantined files")
    purge.add_argument("--plan", required=True)
    purge.set_defaults(func=cmd_purge)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

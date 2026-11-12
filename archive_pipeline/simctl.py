"""Scenario controller: mutate simulator state and restart the sites.

Usage (from the project root)::

    python -m archive_pipeline.simctl start
    python -m archive_pipeline.simctl mutate --site layout_a --set footer_version=2
    python -m archive_pipeline.simctl mutate --site layout_b \\
        --set-notice B-2005 status_override=503
    python -m archive_pipeline.simctl mutate --site layout_a \\
        --set-notice A-1044 available=false
    python -m archive_pipeline.simctl stop

The two simulator *servers* are normally started in-process by the demo runner.
The CLI is used for manual poking; it holds its own servers in a background
process (start/stop) and writes state into the shared scenario JSON.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

DEFAULT_SCENARIO = Path(__file__).resolve().parent.parent / "scenario" / "scenario.json"


def _coerce(value: str):
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def load_scenario(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_scenario(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def cmd_mutate(args) -> int:
    path = Path(args.scenario)
    data = load_scenario(path)
    site = data["sites"].setdefault(args.site, {})

    for pair in args.set_values or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        site[key] = _coerce(value)

    for raw_pair in args.set_notice or []:
        pair = raw_pair if isinstance(raw_pair, str) else " ".join(raw_pair)
        if ":" in pair.split("=")[0]:
            notice_key, sep, assignment = pair.partition(":")
        else:
            notice_key, sep, assignment = pair.partition(" ")
        if not sep:
            raise SystemExit(
                f"--set-notice expects 'KEY:field=value' or 'KEY field=value', "
                f"got {pair!r}")
        field, sep, value = assignment.strip().partition("=")
        if not sep:
            raise SystemExit(f"--set-notice expects field=value, got {pair!r}")
        field = field.strip()
        if not field:
            raise SystemExit(f"--set-notice has empty field name: {pair!r}")
        for notice in site.get("notices", []):
            if notice["key"] == notice_key.strip():
                notice[field] = _coerce(value)
                break
        else:
            raise SystemExit(f"notice {notice_key!r} not found in {args.site}")
    save_scenario(path, data)
    print(f"updated {path} ({args.site})")
    return 0


def cmd_show(args) -> int:
    data = load_scenario(Path(args.scenario))
    print(json.dumps(data["sites"].get(args.site, {}), ensure_ascii=False, indent=2))
    return 0


def cmd_start(args) -> int:
    from .sim_server import start_sites
    sites = start_sites(Path(args.scenario))
    print(f"sim-a listening on http://127.0.0.1:{sites[0].port}")
    print(f"sim-b listening on http://127.0.0.1:{sites[1].port}")
    print("Ctrl-C to stop.")

    def _stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for site in sites:
            site.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulator scenario control")
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="run both simulators")

    show = sub.add_parser("show", help="print scenario state")
    show.add_argument("--site", default="layout_a")

    mutate = sub.add_parser("mutate", help="edit scenario state")
    mutate.add_argument("--site", required=True)
    mutate.add_argument("--set", dest="set_values", action="append",
                        metavar="KEY=VALUE")
    mutate.add_argument("--set-notice", dest="set_notice", action="append",
                        nargs="+", metavar="KEY:FIELD=VALUE")

    args = parser.parse_args(argv)
    if args.command == "start":
        return cmd_start(args)
    if args.command == "show":
        return cmd_show(args)
    return cmd_mutate(args)


if __name__ == "__main__":
    sys.exit(main())

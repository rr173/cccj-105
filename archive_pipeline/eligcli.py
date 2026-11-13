"""CLI for the offline eligibility pre-screen job — no interface, no site access.

    python -m archive_pipeline.eligcli check --clause PATH
    python -m archive_pipeline.eligcli run --clause PATH --applications PATH
    python -m archive_pipeline.eligcli show --application-id ID [--run N]
    python -m archive_pipeline.eligcli runs
    python -m archive_pipeline.eligcli clause

Exit codes:
    0  success (check accepted / run completed / show found)
    2  usage or operational error (missing file, missing result row)
    3  clause rejected — **no judgment was produced**
    4  application file structurally damaged — run marked incomplete, rows
       written before the bad line remain, no success summary
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .eligclause import ClauseError, load_clause
from .eligengine import render_result
from .eligibility import (
    EligibilityError,
    EligibilityJob,
    EligibilityStore,
    explain_application,
)

ROOT = Path(__file__).resolve().parent.parent


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _slim(payload: dict) -> dict:
    """Drop bulky input documents from read-back projections."""
    payload = dict(payload)
    explanation = payload.get("explanation")
    if isinstance(explanation, dict) and "input_document" in explanation:
        explanation = dict(explanation)
        explanation.pop("input_document", None)
        payload["explanation"] = explanation
    return payload


def _pipeline_db(args) -> Path:
    return Path(args.pipeline_db) if args.pipeline_db else Path(args.workdir) / "pipeline.sqlite3"


def cmd_check(args) -> int:
    """Validate the clause fully; print its summary.  Never reads applications."""
    try:
        clause = load_clause(_pipeline_db(args), args.clause)
    except ClauseError as exc:
        _print({"status": "rejected", "clause_path": str(args.clause),
                "issues": exc.issues,
                "message": "条款校验失败；未读取任何申请，未产出任何判定"})
        return 3
    _print({"status": "accepted", "clause": clause.summary(),
            "message": "条款已通过校验：版本存在、引文字符区间与原文逐字相符"})
    return 0


def cmd_clause(args) -> int:
    return cmd_check(args)


def cmd_run(args) -> int:
    echo = None
    progress = None
    if args.progress:
        def echo(item):
            print(f"[{item['seq']}] line {item['line_no']} "
                  f"{item['application_id']}: {item['verdict_label']} "
                  f"({item['summary_reason'] or '-'})", file=sys.stderr)

        def progress(message):
            print(message, file=sys.stderr)
    try:
        job = EligibilityJob(args.workdir, args.clause, progress=progress)
    except ClauseError as exc:
        _print({"status": "rejected", "clause_path": str(args.clause),
                "issues": exc.issues,
                "message": "条款校验失败；未读取任何申请，未产出任何判定"})
        return 3
    except EligibilityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        outcome = job.run(args.applications, echo=echo)
    except EligibilityError as exc:
        job.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    job.close()
    payload = {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "applications_written": outcome.applications_written,
        "summary": outcome.summary,
        "error": outcome.error,
    }
    if outcome.status != "completed":
        payload["message"] = (
            "已写入的结果明确为未完成；不提供成功汇总。"
            if outcome.status == "incomplete"
            else "运行失败；结果未完成。")
    _print(payload)
    if outcome.status == "incomplete":
        return 4
    if outcome.status == "failed":
        return 2
    return 0


def cmd_show(args) -> int:
    try:
        payload = explain_application(args.workdir, args.application_id,
                                      run_id=args.run)
    except EligibilityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if payload is None:
        print(f"application_id {args.application_id!r} not found in "
              f"{args.workdir}", file=sys.stderr)
        return 2
    payload["clause_summary"] = payload.get("clause_summary")
    payload["run_status"] = payload.get("run_status")
    if args.text:
        _render_text(payload)
    elif not args.full:
        payload["explanation_text"] = render_result(payload["explanation"])
    _print(_slim(payload))
    return 0


def _render_text(payload: dict) -> None:
    explanation = payload["explanation"]
    print(f"申请 {payload['application_id']}（run {payload['run_id']}，"
          f"输入第 {payload['line_no']} 行）")
    print(f"结论：{payload['verdict_label']}（{payload['verdict']}），"
          f"根条件 {explanation['root_condition']}")
    binding = explanation["clause_binding"]
    print(f"条款绑定：{binding['source_id']} {binding['archive_id']} "
          f"v{binding['version_no']}（{binding['notice_key']}，"
          f"引文摘要 {binding['body_sha256'][:16]}…）")
    print("解释（全部分支，未短路）：")
    for line in render_result(explanation):
        print("  " + line)
    unknown = explanation["unknown_values"]
    if unknown:
        print("未知值及原因：")
        for item in unknown:
            print(f"  - {item['condition_id']} {item['json_pointer']} "
                  f"（{item['reason']}：{item['detail']}）")


def cmd_runs(args) -> int:
    db = Path(args.workdir) / "eligibility.sqlite3"
    if not db.exists():
        print("error: no eligibility database; run 'run' first",
              file=sys.stderr)
        return 2
    store = EligibilityStore(db)
    rows = store.runs()
    payload = []
    for row in rows:
        summary = json.loads(row["summary_json"]) if row["summary_json"] else None
        error = json.loads(row["error_json"]) if row["error_json"] else None
        payload.append({
            "run_id": row["run_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "clause_path": row["clause_path"],
            "clause_digest": row["clause_digest"],
            "input_path": row["input_path"],
            "applications_written": row["applications_written"],
            "summary": summary,
            "error": error,
            "counts": store.run_counts(row["run_id"]),
        })
    _print(payload)
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archive_pipeline.eligcli",
        description="离线资格预审：条款只读绑定归档正文，逐行处理申请 JSONL")
    parser.add_argument("--workdir", default=str(ROOT / "work"))
    parser.add_argument("--pipeline-db",
                        help="identity-schema pipeline database "
                             "(default: <workdir>/pipeline.sqlite3)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_clause_arg(p, required=True):
        p.add_argument("--clause", required=required, help="clause JSON file")

    check = sub.add_parser("check", help="validate a clause without reading applications")
    add_clause_arg(check)
    check.set_defaults(func=cmd_check)

    clause = sub.add_parser("clause", help="alias of check (inspect a clause)")
    add_clause_arg(clause)
    clause.set_defaults(func=cmd_clause)

    run = sub.add_parser("run", help="pre-screen an application JSONL file")
    add_clause_arg(run)
    run.add_argument("--applications", required=True,
                     help="application JSONL file")
    run.add_argument("--progress", action="store_true",
                     help="echo one compact line per application to stderr")
    run.set_defaults(func=cmd_run)

    show = sub.add_parser("show", help="full explanation of one application")
    show.add_argument("--application-id", required=True)
    show.add_argument("--run", type=int, help="specific run (default: latest)")
    show.add_argument("--text", action="store_true",
                      help="render a readable tree before the JSON")
    show.add_argument("--full", action="store_true",
                      help="omit the extra rendered text field")
    show.set_defaults(func=cmd_show)

    runs = sub.add_parser("runs", help="list runs (completed / incomplete / failed)")
    runs.set_defaults(func=cmd_runs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

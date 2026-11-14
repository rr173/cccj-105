"""CLI for the offline relation job — declarative rules, no user Python/SQL.

    python -m archive_pipeline.relcli check --rules RULES.json [--plan]
    python -m archive_pipeline.relcli run   --rules RULES.json [--budget N]
    python -m archive_pipeline.relcli show  --relation NAME [--run N] \
        [--page N --page-size N | --offset N --limit N] [--json]
    python -m archive_pipeline.relcli plan  --rules RULES.json
    python -m archive_pipeline.relcli runs
    python -m archive_pipeline.relcli summary [--run N]

Exit codes:
    0  success (rules accepted / run completed / page found)
    2  usage or operational error (missing file/database, bad page request)
    3  rule document rejected — nothing was read, nothing was produced
    4  fact/structure failure, budget overflow or interrupted publish — the
       run was rolled back; earlier successful results are untouched
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .relationjob import RelationJob, check_rules, format_plan, read_relation
from .releva import JobError, RequestViolation, ResultStore



def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _db_path(args) -> Path:
    if args.result_db:
        return Path(args.result_db)
    return Path(args.workdir) / "relation.sqlite3"


def _open_store(args) -> ResultStore | None:
    path = _db_path(args)
    if not path.exists():
        print(f"error: no relation result database at {path}; run 'run' first",
              file=sys.stderr)
        return None
    try:
        return ResultStore(path)
    except JobError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _run_id(args, store: ResultStore) -> int | None:
    run = store.run(args.run)
    if run is None:
        print("error: no run found", file=sys.stderr)
        return None
    if run["status"] != "completed":
        print(f"error: run {run['run_id']} is {run['status']}, not a published "
              "result", file=sys.stderr)
        return None
    return run["run_id"]


def cmd_check(args) -> int:
    payload = check_rules(args.rules)
    if payload["status"] != "accepted":
        _print(payload)
        return 3
    if args.plan:
        print(format_plan(payload["rules"]))
    if args.json or not args.plan:
        _print(payload)
    return 0


def cmd_plan(args) -> int:
    payload = check_rules(args.rules)
    if payload["status"] != "accepted":
        _print(payload)
        return 3
    print(format_plan(payload["rules"]))
    _print({"stratification": payload["rules"]["plan"],
            "rules": payload["rules"]["rules"]})
    return 0


def cmd_run(args) -> int:
    job = RelationJob(args.workdir,
                      pipeline_db=(args.pipeline_db or
                                   Path(args.workdir) / "pipeline.sqlite3"),
                      result_db=args.result_db,
                      progress=(lambda message: print(message, file=sys.stderr)
                                if args.progress else None))
    try:
        if args.hard_exit:
            # A real process death hook: os._exit from inside the open
            # transaction, before anything is committed.
            outcome = job.run(args.rules, budget=args.budget,
                              hard_exit_after_load=True)
        else:
            outcome = job.run(args.rules, budget=args.budget,
                              fail_point=args.fail_point)
    finally:
        job.close()
    if outcome.status == "rejected":
        _print({"status": "rejected", "issues": [outcome.error],
                "message": "规则文档被拒绝；未读取任何事实，未改动结果库"})
        return 3
    if outcome.status != "completed":
        _print({"status": "failed", "error": outcome.error,
                "message": "本次作业失败并已整事务回滚；未发布新结果，"
                           "既有成功结果保持不变"})
        return outcome.exit_code
    payload = {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "total_tuples": outcome.summary["total_tuples"],
        "budget": outcome.summary["budget"],
        "relations": outcome.summary["relations"],
        "components": outcome.summary["components"],
        "notice": {k: outcome.summary["notice"][k]
                   for k in ("records", "distinct", "consistent", "sources")},
        "message": "结果已发布：规则摘要、输入摘要、轮次统计与关系行同事务提交",
    }
    _print(payload)
    return 0


def cmd_show(args) -> int:
    store = _open_store(args)
    if store is None:
        return 2
    run_id = _run_id(args, store)
    if run_id is None:
        store.close()
        return 2
    if args.page is not None or args.page_size is not None:
        page = args.page if args.page is not None else 0
        size = args.page_size if args.page_size is not None else 100
        offset, limit = page * size, size
    else:
        offset, limit = args.offset, args.limit
    try:
        payload = read_relation(store, run_id, args.relation,
                                offset=offset, limit=limit)
    except RequestViolation as exc:
        print(f"error: {exc}", file=sys.stderr)
        store.close()
        return 2
    store.close()
    if args.json:
        _print(payload)
        return 0
    print(f"run {payload['run_id']} relation {payload['relation']} "
          f"({payload['kind']}, {payload['origin']})")
    print(f"columns: {', '.join(c['name'] + ':' + c['type'] for c in payload['columns'])}")
    print(f"total {payload['total']} tuples; offset {payload['offset']} "
          f"limit {payload['limit']}")
    print(json.dumps(payload["rows"], ensure_ascii=False, indent=2))
    return 0


def cmd_runs(args) -> int:
    store = _open_store(args)
    if store is None:
        return 2
    rows = []
    for run in store.runs():
        summary = json.loads(run["summary_json"]) if run["summary_json"] else None
        rows.append({"run_id": run["run_id"], "status": run["status"],
                     "started_at": run["started_at"],
                     "finished_at": run["finished_at"],
                     "rules_sha256": run["rules_sha256"],
                     "total_tuples": summary.get("total_tuples") if summary else None,
                     "budget": run["budget"]})
    store.close()
    _print(rows)
    return 0


def cmd_summary(args) -> int:
    store = _open_store(args)
    if store is None:
        return 2
    run = store.run(args.run)
    if run is None:
        print("error: no run found", file=sys.stderr)
        store.close()
        return 2
    run_id = run["run_id"]
    summary = json.loads(run["summary_json"]) if run["summary_json"] else {}
    payload = {
        "run": run,
        "relations": store.relation_stats(run_id),
        "execution": store.execution(run_id),
        "rounds": store.rounds(run_id),
        "component_rounds": summary.get("component_rounds", []),
        "inputs": [json.loads(row["digest_json"])
                   for row in store.inputs(run_id)],
    }
    store.close()
    _print(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archive_pipeline.relcli",
        description="离线关系规则作业：声明式规则 + 归档 notice 关系 + 人工 JSONL 关系")
    parser.add_argument("--workdir", default=str(Path(__file__).resolve()
                                                 .parent.parent / "work"))
    parser.add_argument("--pipeline-db",
                        help="archive pipeline database (default: "
                             "<workdir>/pipeline.sqlite3)")
    parser.add_argument("--result-db",
                        help="result database (default: <workdir>/relation.sqlite3)")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate rules; read no facts")
    check.add_argument("--rules", required=True)
    check.add_argument("--plan", action="store_true",
                       help="print the readable stratification plan")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)

    plan = sub.add_parser("plan", help="alias of check --plan")
    plan.add_argument("--rules", required=True)
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="load facts, evaluate and publish one run")
    run.add_argument("--rules", required=True)
    run.add_argument("--budget", type=int,
                     help="total stored-tuple budget (default: from rules)")
    run.add_argument("--progress", action="store_true")
    run.add_argument("--fail-point",
                     choices=["before_commit", "after_evaluate", "during_load"],
                     help="inject a controlled failure inside the transaction")
    run.add_argument("--hard-exit", action="store_true",
                     help="kill the process inside the open transaction "
                          "(test hook; nothing is committed)")
    run.set_defaults(func=cmd_run)

    show = sub.add_parser("show", help="paginate one relation of a result run")
    show.add_argument("--relation", required=True)
    show.add_argument("--run", type=int, default=None)
    show.add_argument("--offset", type=int, default=0)
    show.add_argument("--limit", type=int, default=100)
    show.add_argument("--page", type=int, default=None)
    show.add_argument("--page-size", type=int, default=None)
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    runs = sub.add_parser("runs", help="list runs")
    runs.set_defaults(func=cmd_runs)

    summary = sub.add_parser("summary", help="metadata of one run")
    summary.add_argument("--run", type=int, default=None)
    summary.set_defaults(func=cmd_summary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

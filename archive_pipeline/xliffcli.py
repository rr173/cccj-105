"""CLI for the XLIFF review-intake job — no network, no translation, no writes back.

    python -m archive_pipeline.xliffcli intake REVIEW.xlf --bindings MAP.json
    python -m archive_pipeline.xliffcli usable [--source ID] [--archive-id ID] \
        [--version N] [--language en-US] [--json]
    python -m archive_pipeline.xliffcli issues [--run N] [--unit-status rejected|draft] \
        [--json]
    python -m archive_pipeline.xliffcli units [--delivery N] [--unit-id X] [--json]
    python -m archive_pipeline.xliffcli groups [--state available|conflict] [--json]
    python -m archive_pipeline.xliffcli candidates [--archive-id ID] [--json]
    python -m archive_pipeline.xliffcli deliveries [--json]
    python -m archive_pipeline.xliffcli runs [--json]

Exit codes:
    0  success (intake accepted / query found something)
    2  usage or operational error (missing file, database, unknown version)
    3  review document or binding map refused — **the corpus was not modified**
    4  XML was well-formed but at least one file/unit broke the contract;
       the other units were intaken, and every problem is listed by unit
    5  controlled interruption before commit — nothing was written

The job reads the identity-schema pipeline database and the archived version
bodies; it never fetches a URL, never calls a translation service and never
writes a translation back into a notice body.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .xliffintake import (
    IntakeInterrupted,
    IntakeJob,
    IntakeRejected,
    MappingError,
    XliffError,
    XliffStore,
)

ROOT = Path(__file__).resolve().parent.parent


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _decode(row: dict, fields: tuple[str, ...]) -> dict:
    out = dict(row)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str) and value:
            try:
                out[field] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return out


_UNIT_JSON = ("group_path_json", "source_codes_json", "target_codes_json",
              "location_json")


def _store(args) -> XliffStore | None:
    db = Path(args.workdir) / "xliff.sqlite3"
    if not db.exists():
        print(f"error: no XLIFF database at {db}; run 'intake' first",
              file=sys.stderr)
        return None
    return XliffStore(db)


def cmd_intake(args) -> int:
    try:
        job = IntakeJob(args.workdir, args.pipeline_db,
                        progress=lambda msg: print(msg, file=sys.stderr))
    except (XliffError, MappingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        outcome = job.intake(args.review, args.bindings,
                             fail_point=args.fail_point)
    except MappingError as exc:
        job.close()
        _print({"status": "refused", "code": exc.code, "detail": exc.detail,
                "message": "映射文件无效；未改动语料库"})
        return exc.exit_code
    except IntakeRejected as exc:
        job.close()
        _print({"status": "refused", "code": exc.code, "detail": exc.detail,
                "location": exc.location,
                "message": "审译文件被拒绝；未改动语料库（XML 语法错误或"
                           "含 DTD/外部实体）"})
        return exc.exit_code
    except IntakeInterrupted as exc:
        job.close()
        _print({"status": "interrupted", "detail": str(exc),
                "message": "提交前强制退出：整份交付已回滚，语料库与旧译文状态"
                           "保持原样"})
        return 5
    except XliffError as exc:
        job.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # unexpected: never leave a half-written delivery
        job.close()
        print(f"error: intake failed and was rolled back: {exc!r}",
              file=sys.stderr)
        return 2
    job.close()
    payload = {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "delivery_id": outcome.delivery_id,
        "summary": outcome.summary,
    }
    if outcome.status == "partial":
        payload["message"] = (
            "XML 完好但部分单元不符合约定：其余单元已接入，问题按单元列出（见"
            " summary.unit_results / issues）")
    elif outcome.status == "duplicate":
        payload["message"] = outcome.summary.get("message")
    _print(payload)
    return outcome.exit_code


def cmd_usable(args) -> int:
    store = _store(args)
    if store is None:
        return 2
    groups = store.group_state(args.source, args.archive_id,
                               state="available")
    available = []
    for group in groups:
        if args.version is not None and group["version_no"] != args.version:
            continue
        if args.language and group["target_language"] != args.language.lower():
            continue
        candidate = store.candidate(group["available_candidate_id"])
        if candidate is None:  # pragma: no cover - defensive
            continue
        sources = store.candidate_sources(candidate["candidate_id"])
        available.append({
            "source_id": candidate["source_id"],
            "archive_id": candidate["archive_id"],
            "version_no": candidate["version_no"],
            "span": [candidate["span_start"], candidate["span_end"]],
            "source_language": candidate["source_language"],
            "target_language": candidate["target_language"],
            "candidate_id": candidate["candidate_id"],
            "source_text": candidate["source_text"],
            "target_text": candidate["target_text"],
            "target_codes": json.loads(candidate["target_codes_json"] or "[]"),
            "delivery_sources": [
                {"delivery_id": s["delivery_id"],
                 "review_path": s["review_path"],
                 "review_sha256": s["review_sha256"],
                 "unit_id": s["unit_id"],
                 "group_path": json.loads(s["group_path_json"] or "[]")}
                for s in sources],
        })
    if args.json:
        _print(available)
    else:
        for row in available:
            print(f"{row['source_id']} {row['archive_id']} v{row['version_no']} "
                  f"[{row['span'][0]},{row['span'][1]}) "
                  f"{row['source_language']}→{row['target_language']} "
                  f"candidate={row['candidate_id']} "
                  f"deliveries={len(row['delivery_sources'])}")
            print(f"  {row['target_text']}")
    store.close()
    return 0


def cmd_groups(args) -> int:
    store = _store(args)
    if store is None:
        return 2
    groups = store.group_state(args.source, args.archive_id, args.state)
    payload = []
    for group in groups:
        item = dict(group)
        item["candidates"] = []
        for candidate in store.candidates(group["source_id"],
                                          group["archive_id"],
                                          group["version_no"]):
            if (candidate["span_start"] != group["span_start"]
                    or candidate["span_end"] != group["span_end"]
                    or candidate["source_language"] != group["source_language"]
                    or candidate["target_language"] != group["target_language"]):
                continue
            item["candidates"].append({
                "candidate_id": candidate["candidate_id"],
                "source_text": candidate["source_text"],
                "target_text": candidate["target_text"],
                "delivery_sources": [
                    {"delivery_id": s["delivery_id"],
                     "review_path": s["review_path"],
                     "unit_id": s["unit_id"]}
                    for s in store.candidate_sources(candidate["candidate_id"])],
            })
        payload.append(item)
    if args.json:
        _print(payload)
    else:
        for item in payload:
            print(f"{item['source_id']} {item['archive_id']} "
                  f"v{item['version_no']} "
                  f"[{item['span_start']},{item['span_end']}) "
                  f"{item['source_language']}→{item['target_language']} "
                  f"{item['state']} candidates={item['candidate_count']}")
            if item.get("reason"):
                print(f"  reason: {item['reason']}")
            for candidate in item["candidates"]:
                print(f"  candidate {candidate['candidate_id']}: "
                      f"{candidate['target_text']}")
    store.close()
    return 0


def cmd_candidates(args) -> int:
    store = _store(args)
    if store is None:
        return 2
    candidates = store.candidates(args.source, args.archive_id, args.version)
    payload = []
    for candidate in candidates:
        payload.append({
            "candidate_id": candidate["candidate_id"],
            "source_id": candidate["source_id"],
            "archive_id": candidate["archive_id"],
            "version_no": candidate["version_no"],
            "span": [candidate["span_start"], candidate["span_end"]],
            "source_language": candidate["source_language"],
            "target_language": candidate["target_language"],
            "event_digest": candidate["event_digest"],
            "source_text": candidate["source_text"],
            "target_text": candidate["target_text"],
            "target_codes": json.loads(candidate["target_codes_json"] or "[]"),
            "delivery_sources": [
                {"delivery_id": s["delivery_id"],
                 "review_path": s["review_path"],
                 "unit_id": s["unit_id"],
                 "delivery_created_at": s["delivery_created_at"]}
                for s in store.candidate_sources(candidate["candidate_id"])],
        })
    _print(payload)
    store.close()
    return 0


def cmd_issues(args) -> int:
    store = _store(args)
    if store is None:
        return 2
    rows = store.units(status=args.unit_status)
    if args.run is not None:
        run = store.run(args.run)
        if run is None:
            print(f"error: run {args.run} not found", file=sys.stderr)
            store.close()
            return 2
        delivery_id = run.get("delivery_id")
        rows = [r for r in rows
                if delivery_id is not None and r["delivery_id"] == delivery_id]
    files = store.files()
    file_by_pk = {f["file_pk"]: f for f in files}
    payload = []
    for row in rows:
        if row["status"] == "candidate":
            continue
        file_row = file_by_pk.get(row["file_pk"], {})
        payload.append({
            "delivery_id": row["delivery_id"],
            "unit_pk": row["unit_pk"],
            "unit_id": row["unit_id"],
            "status": row["status"],
            "reason_code": row["reason_code"],
            "reason_detail": row["reason_detail"],
            "original": file_row.get("original"),
            "group_path": json.loads(row["group_path_json"] or "[]"),
            "span": [row["span_start"], row["span_end"]],
            "source_language": row["source_language"],
            "target_language": row["target_language"],
            "source_text": row["source_text"],
            "target_text": row["target_text"],
            "source_codes": json.loads(row["source_codes_json"] or "null"),
            "target_codes": json.loads(row["target_codes_json"] or "null"),
            "line": row["line"],
            "source_line": row["source_line"],
            "target_line": row["target_line"],
        })
    if args.json:
        _print(payload)
    else:
        for item in payload:
            print(f"[{item['status']}] delivery={item['delivery_id']} "
                  f"unit={item['unit_id']!r} file={item['original']!r} "
                  f"group={'/'.join(item['group_path']) or '-'} "
                  f"span={item['span']} line={item['line']}")
            print(f"  code={item['reason_code']}: {item['reason_detail']}")
    store.close()
    return 0


def cmd_units(args) -> int:
    store = _store(args)
    if store is None:
        return 2
    if args.unit_id:
        rows = store.unit_by_id(args.unit_id, args.delivery)
    else:
        rows = store.units(args.delivery, args.unit_status)
    files = {f["file_pk"]: f for f in store.files()}
    payload = []
    for row in rows:
        item = _decode(row, _UNIT_JSON)
        item["original"] = files.get(row["file_pk"], {}).get("original")
        payload.append(item)
    _print(payload)
    store.close()
    return 0


def cmd_deliveries(args) -> int:
    store = _store(args)
    if store is None:
        return 2
    payload = []
    for delivery in store.deliveries():
        payload.append({
            "delivery_id": delivery["delivery_id"],
            "run_id": delivery["run_id"],
            "review_path": delivery["review_path"],
            "review_sha256": delivery["review_sha256"],
            "mapping_path": delivery["mapping_path"],
            "mapping_sha256": delivery["mapping_sha256"],
            "created_at": delivery["created_at"],
            "files": [
                {"file_index": f["file_index"], "original": f["original"],
                 "status": f["status"], "reason_code": f["reason_code"],
                 "units": f["unit_count"]}
                for f in store.files(delivery["delivery_id"])],
        })
    _print(payload)
    store.close()
    return 0


def cmd_runs(args) -> int:
    store = _store(args)
    if store is None:
        return 2
    payload = []
    for run in store.runs():
        payload.append({
            "run_id": run["run_id"],
            "status": run["status"],
            "review_path": run["review_path"],
            "mapping_path": run["mapping_path"],
            "delivery_id": run["delivery_id"],
            "units_total": run["units_total"],
            "summary": json.loads(run["summary_json"])
            if run["summary_json"] else None,
            "error": json.loads(run["error_json"]) if run["error_json"] else None,
        })
    _print(payload)
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archive_pipeline.xliffcli",
        description="XLIFF 1.2 审译接入：只读归档正文，整理可检索的双语语料")
    parser.add_argument("--workdir", default=str(ROOT / "work"))
    parser.add_argument("--pipeline-db",
                        help="identity-schema pipeline database "
                             "(default: <workdir>/pipeline.sqlite3)")
    sub = parser.add_subparsers(dest="command", required=True)

    intake = sub.add_parser("intake", help="接收一份审译文件与其映射")
    intake.add_argument("review", help="XLIFF 1.2 review file")
    intake.add_argument("--bindings", required=True, help="binding map JSON")
    intake.add_argument("--fail-point", help=argparse.SUPPRESS)
    intake.set_defaults(func=cmd_intake)

    usable = sub.add_parser("usable", help="query the usable bilingual corpus")
    usable.add_argument("--source")
    usable.add_argument("--archive-id")
    usable.add_argument("--version", type=int)
    usable.add_argument("--language", help="target language, e.g. en-US")
    usable.add_argument("--json", action="store_true")
    usable.set_defaults(func=cmd_usable)

    groups = sub.add_parser("groups", help="group availability and conflicts")
    groups.add_argument("--source")
    groups.add_argument("--archive-id")
    groups.add_argument("--state", choices=["available", "conflict"])
    groups.add_argument("--json", action="store_true")
    groups.set_defaults(func=cmd_groups)

    candidates = sub.add_parser("candidates", help="list candidates and sources")
    candidates.add_argument("--source")
    candidates.add_argument("--archive-id")
    candidates.add_argument("--version", type=int)
    candidates.set_defaults(func=cmd_candidates)

    issues = sub.add_parser("issues", help="rejected/draft material with reasons")
    issues.add_argument("--run", type=int)
    issues.add_argument("--unit-status",
                        choices=["rejected", "draft", "candidate"])
    issues.add_argument("--json", action="store_true")
    issues.set_defaults(func=cmd_issues)

    units = sub.add_parser("units", help="stored units incl. XLIFF positions")
    units.add_argument("--delivery", type=int)
    units.add_argument("--unit-id")
    units.add_argument("--unit-status",
                       choices=["rejected", "draft", "candidate"])
    units.set_defaults(func=cmd_units)

    sub.add_parser("deliveries", help="delivery provenance").set_defaults(
        func=cmd_deliveries)
    sub.add_parser("runs", help="intake runs").set_defaults(func=cmd_runs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

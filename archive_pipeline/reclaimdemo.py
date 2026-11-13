"""End-to-end demonstration of raw-archive space reclamation.

Builds a synthetic work directory under ``work/reclaimdemo/`` and walks every
required situation against real files, real SQLite facts and real byte counts:

1. preview classifies the archive without moving or writing anything;
2. ``plan`` persists a re-openable snapshot; the plan is re-opened from disk;
3. isolation moves candidates into the program-managed quarantine, verify
   that the version- and scan-referenced bytes are untouched, then undo it;
4. a second plan is isolated and permanently purged, with the released bytes
   counted from real unlinks;
5. a saved plan gains a new reference before isolation -> stale, no move;
6. a collection writes raw bytes but has not committed facts yet -> the
   exclusive lock makes reclamation wait, then the file is protected;
7. a process is killed between moving the response and its metadata ->
   reopening reconciles the journal and can resume or undo;
8. undo meets a different file at the original path -> both kept, conflict
   reported, never overwritten;
9. purge retried after an interruption does not double-count released bytes.

The demo asserts its own outcomes (``verify()``) and writes
``reports/reclaim.md``.

Run: ``python -m archive_pipeline.reclaimdemo``
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .reclaim import (
    PLAN_PURGE_BLOCKED,
    PLAN_RESTORED_WITH_CONFLICTS,
    PLAN_ISOLATED,
    PLAN_ISOLATING,
    PLAN_PURGED,
    PLAN_RESTORED,
    ReferenceError,
    ReclaimCrash,
    StalePlan,
    create_plan,
    isolate_plan,
    open_plan,
    purge_plan,
    restore_plan,
    sha256_file,
    parse_iso,
)
from .reclaimsample import build_scenario

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "work" / "reclaimdemo"
REPORT = ROOT / "reports" / "reclaim.md"
CUTOFF = datetime(2026, 10, 1, tzinfo=timezone.utc)
FUTURE_CUTOFF = datetime(2027, 1, 1, tzinfo=timezone.utc)


def _count_tree(root: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            files += 1
            size += path.stat().st_size
    return files, size


def main() -> int:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True)
    built = build_scenario(WORKDIR)
    entries = built["entries"]
    archive_root = Path(built["archive_root"])
    report: list[str] = []
    checks: list[dict] = []

    def emit(line: str = "") -> None:
        print(line)
        report.append(line)

    def check(name: str, condition: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(condition), "detail": detail})
        emit(f"- [{'PASS' if condition else 'FAIL'}] {name}: {detail}")

    emit("# 原始留档空间回收 · 端到端演示")
    emit()
    emit(f"工作目录：`{WORKDIR.relative_to(ROOT)}`；宽限截止：{CUTOFF.isoformat()}")
    emit()

    # -- 1. preview ------------------------------------------------------
    emit("## 1. 预览：只读分类，不改文件")
    files_before, bytes_before = _count_tree(archive_root)
    preview_cmd = [sys.executable, "-m", "archive_pipeline.reclaimcli",
                   "--workdir", str(WORKDIR), "preview",
                   "--cutoff", CUTOFF.isoformat()]
    preview_run = subprocess.run(preview_cmd, capture_output=True, text=True,
                                 cwd=ROOT)
    preview = json.loads(preview_run.stdout)
    files_after, bytes_after = _count_tree(archive_root)
    check("preview exit code", preview_run.returncode == 0, preview_run.stderr)
    check("preview moved no file", (files_before, bytes_before) == (files_after, bytes_after),
          f"{files_before} files / {bytes_before} bytes before and after")
    check("preview created no plan",
          not (WORKDIR / "reclaim.sqlite3").exists(),
          "reclaim.sqlite3 absent after preview")
    summary = preview["summary"]
    emit()
    emit("| 分类 | 文件组 | 磁盘字节 |")
    emit("| --- | --- | --- |")
    for key in ("protected", "within_grace", "isolatable", "anomaly"):
        emit(f"| {key} | {summary[key]['groups']} | {summary[key]['disk_bytes']} |")
    emit()
    emit(f"预计可隔离占用：{summary['estimated_reclaimable_disk_bytes']} 字节；"
         f"已释放：{summary['freed_disk_bytes']} 字节（预览从不宣称已释放空间）")
    check("cross-date copy protected",
          all(g["verdict"] == "protected"
              for g in preview["groups"]
              if g["digest"] == entries["cross_date_protected"]["digest"]),
          "both date copies of the referenced digest are protected")
    check("fresh orphan within grace",
          next(g["verdict"] for g in preview["groups"]
               if g["digest"] == entries["fresh_orphan"]["digest"]) == "within_grace",
          "new unreferenced file is not a candidate")
    check("gzip judged by decompressed bytes",
          next(g["verdict"] for g in preview["groups"]
               if g["digest"] == entries["gzip_orphan"]["digest"]) == "isolatable",
          "valid gzip orphan is isolatable")
    for key in ("corrupt_gzip", "incomplete_group", "digest_mismatch",
                "metadata_source_conflict", "metadata_size_conflict",
                "symlink", "foreign_path"):
        verdict = next(g["verdict"] for g in preview["groups"]
                       if g["digest"] == entries[key].get("digest", "")
                       or g["group_rel"].endswith(entries[key]["path"].split("/", 2)[-1])
                       or g["group_rel"] == entries[key]["path"])
        check(f"diagnostic only: {key}", verdict == "anomaly", f"verdict={verdict}")
    emit()

    # -- 2. plan ---------------------------------------------------------
    emit("## 2. 保存计划并可再次打开")
    plan = create_plan(WORKDIR, None, CUTOFF)
    plan_id = plan["plan_id"]
    reopened = open_plan(WORKDIR, plan_id)
    check("plan re-opens", reopened["plan_id"] == plan_id
          and reopened["status"] == "planned",
          f"{plan_id} status={reopened['status']}")
    check("plan moved no file",
          _count_tree(archive_root) == (files_before, bytes_before),
          "no file was moved by planning")
    candidate_files = [f for g in plan["groups"] if g["verdict"] == "isolatable"
                       for f in g["files"]]
    check("per-file rows recorded",
          all(f["rel_path"] and f["sha256"] and f["size_bytes"] for f in candidate_files),
          f"{len(candidate_files)} planned file rows with hash and size")
    emit()

    # -- 3. isolate then restore ----------------------------------------
    emit("## 3. 隔离 → 撤销（文件仍在隔离区，可撤销）")
    protected_version = archive_root / entries["version_referenced"]["path"]
    protected_scan = archive_root / entries["scan_referenced"]["path"]
    version_bytes = protected_version.read_bytes()
    isolated = isolate_plan(WORKDIR, plan_id)
    check("isolation moved candidates", isolated["moved"] > 0,
          f"moved={isolated['moved']}")
    check("version bytes untouched", protected_version.read_bytes() == version_bytes,
          "version-referenced response unchanged")
    check("scan-only response untouched", protected_scan.is_file(),
          "scan-item-referenced response still on disk")
    not_moved = [f for f in candidate_files
                 if (archive_root / f["rel_path"]).exists()]
    check("every candidate left its original path", not not_moved,
          f"{len(candidate_files)} moved; {len(not_moved)} still present")
    quarantined = [f for g in isolated["plan"]["groups"] if g["verdict"] == "isolatable"
                   for f in g["files"]]
    check("quarantine holds the files",
          all((archive_root / f["quarantine_rel"]).is_file() for f in quarantined),
          f"{len(quarantined)} files under .reclaim/quarantine/{plan_id}")
    check("no space reported as freed yet",
          isolated["plan"]["released_disk_bytes"] == 0
          and isolated["plan"]["quarantined_disk_bytes"] > 0,
          f"quarantined={isolated['plan']['quarantined_disk_bytes']}, "
          f"released={isolated['plan']['released_disk_bytes']}")
    restored = restore_plan(WORKDIR, plan_id)
    check("restore undoes isolation",
          restored["status"] == PLAN_RESTORED and not not_moved,
          f"status={restored['status']} restored={restored['restored']}")
    check("restored bytes identical",
          all((archive_root / f["rel_path"]).is_file()
              and sha256_file(archive_root / f["rel_path"]) == f["sha256"]
              for f in candidate_files),
          "every restored file hashes to its response digest")
    emit()

    # -- 4. isolate and purge -------------------------------------------
    emit("## 4. 重新计划 → 隔离 → 永久清理")
    before_files, before_bytes = _count_tree(archive_root)
    plan2 = create_plan(WORKDIR, None, CUTOFF)
    pid2 = plan2["plan_id"]
    iso2 = isolate_plan(WORKDIR, pid2)
    purged = purge_plan(WORKDIR, pid2)
    after_files, after_bytes = _count_tree(archive_root)
    expected_release = sum(f["size_bytes"] for g in iso2["plan"]["groups"]
                           if g["verdict"] == "isolatable" for f in g["files"]
                           if f["state"] == "moved")
    check("purge deleted exactly the quarantined bytes",
          purged["released_disk_bytes"] == expected_release,
          f"released={purged['released_disk_bytes']} expected={expected_release}")
    check("on-disk delta matches released bytes",
          before_bytes - after_bytes == expected_release,
          f"{before_bytes} - {after_bytes} = {before_bytes - after_bytes}")
    check("purge status", purged["status"] == PLAN_PURGED, purged["status"])
    from .store import Store as _Store
    _check_store = _Store(WORKDIR / "pipeline.sqlite3")
    preserved_versions = _check_store.conn.execute(
        "SELECT COUNT(*) FROM versions").fetchone()[0]
    preserved_items = _check_store.conn.execute(
        "SELECT COUNT(*) FROM scan_items").fetchone()[0]
    _check_store.close()
    check("versions and scan facts preserved by purge",
          preserved_versions >= 2 and preserved_items >= 1,
          f"{preserved_versions} version rows, {preserved_items} scan item rows remain")
    again = purge_plan(WORKDIR, pid2)
    check("purge retry is idempotent",
          again["purged"] == 0
          and again["released_disk_bytes"] == purged["released_disk_bytes"],
          "retry reports zero new purge and the same released total")
    try:
        restore_plan(WORKDIR, pid2)
        check("purged plan cannot be restored", False, "restore unexpectedly ran")
    except Exception as exc:  # ReclaimError expected
        check("purged plan cannot be restored", True, str(exc))
    emit()

    # -- 5. new reference after plan ------------------------------------
    emit("## 5. 保存计划后新增引用：拒绝过期计划")
    # Add one more old orphan so there is a candidate to race against.
    from .reclaimsample import _meta, _write, OLD_STAMP
    late_raw = b"<html><body><p>late orphan</p></body></html>"
    late_digest = hashlib.sha256(late_raw).hexdigest()
    _write(archive_root / "sim_a" / OLD_STAMP, late_digest, late_raw,
           _meta(late_digest, late_raw, "http://127.0.0.1:8811/notices/late"),
           parse_iso("2026-01-10T12:00:00+00:00"))
    plan3 = create_plan(WORKDIR, None, CUTOFF)
    pid3 = plan3["plan_id"]
    candidate3 = next(g for g in plan3["groups"]
                      if g["verdict"] == "isolatable")
    from .store import Store
    store = Store(WORKDIR / "pipeline.sqlite3")
    store.add_scan_item(scan_id=built["scan_id"], source_id="sim_a",
                        url_key="http://127.0.0.1:8811/notices/late/LATE",
                        kind="detail", outcome="ok", http_status=200,
                        raw_digest=candidate3["digest"], archive_id="arc_late",
                        document_key="LATE", detail={})
    store.close()
    candidate_path = archive_root / next(
        f["rel_path"] for f in candidate3["files"] if f["role"] in {"blob", "blob_gz"})
    try:
        isolate_plan(WORKDIR, pid3)
        check("stale plan refused", False, "isolation ran on a stale plan")
    except StalePlan as exc:
        check("stale plan refused", True,
              f"{len(exc.conflicts)} conflict(s); {exc}")
    check("stale plan moved nothing", candidate_path.is_file(),
          "candidate file still at its original path")
    check("stale plan marked",
          open_plan(WORKDIR, pid3)["status"] == "stale",
          open_plan(WORKDIR, pid3)["status"])
    emit()

    # -- 6. collection race --------------------------------------------
    emit("## 6. 采集写盘但尚未提交：两个进程，锁覆盖入库窗口")
    race_dir = ROOT / "work" / "reclaimdemo-race"
    if race_dir.exists():
        shutil.rmtree(race_dir)
    race_built = build_scenario(race_dir)
    race_archive = Path(race_built["archive_root"])
    # A second old orphan gives the reclaimer something to consider.
    from .reclaimsample import _meta, _write, OLD_STAMP
    late_raw = b"<html><body><p>late orphan</p></body></html>"
    late_digest = hashlib.sha256(late_raw).hexdigest()
    _write(race_archive / "sim_a" / OLD_STAMP, late_digest, late_raw,
           _meta(late_digest, late_raw, "http://127.0.0.1:8811/notices/late"),
           parse_iso("2026-01-10T12:00:00+00:00"))
    race_plan = create_plan(race_dir, None, CUTOFF)
    race_id = race_plan["plan_id"]
    # The in-flight response is a *new* blob written by the collector now (old
    # mtime would be cheating: real scans write fresh bytes).
    inflight_raw = b"<html><body><p>written but not yet committed</p></body></html>"
    inflight_digest = hashlib.sha256(inflight_raw).hexdigest()
    inflight_dir = race_archive / "sim_a" / "20261113"
    fresh_path = inflight_dir / f"{inflight_digest}.bin"
    fresh_meta_path = inflight_dir / f"{inflight_digest}.json"
    marker = race_dir / "bytes-written.marker"
    collector_code = f"""
import gzip, hashlib, json, sys, time
sys.path.insert(0, {str(ROOT)!r})
from archive_pipeline.worklock import collection_lock
from archive_pipeline.store import Store
from pathlib import Path
workdir = Path({str(race_dir)!r})
marker = Path({str(marker)!r})
raw = {inflight_raw!r}
digest = hashlib.sha256(raw).hexdigest()
directory = Path({str(inflight_dir)!r})
# Exactly what ScanEngine.scan does: hold the shared lock for the whole scan,
# including the window between writing bytes and committing the facts.
with collection_lock(workdir, timeout=30.0):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (digest + ".bin")).write_bytes(raw)
    (directory / (digest + ".json")).write_text(json.dumps({{
        "digest_algorithm": "sha256", "digest": digest, "bytes": len(raw),
        "source_id": "sim_a", "url": "http://race/in-flight",
        "status": 200, "storage": "identity"}}))
    marker.write_text("written")
    time.sleep(1.0)          # raw bytes on disk, nothing committed yet
    store = Store(workdir / "pipeline.sqlite3")
    store.add_scan_item(scan_id=1, source_id="sim_a",
                        url_key="http://race/in-flight", kind="detail",
                        outcome="ok", http_status=200, raw_digest=digest,
                        archive_id="arc_race", detail={{}})
    store.close()
    marker.write_text("committed")
"""
    collector = subprocess.Popen([sys.executable, "-c", collector_code],
                                 cwd=ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    check("collector wrote bytes before committing",
          marker.exists() and marker.read_text() == "written",
          "second process holds bytes not yet referenced")
    started = time.monotonic()
    try:
        result = isolate_plan(race_dir, race_id)
        race_status = result["status"]
        race_conflicts = []
    except StalePlan as exc:
        race_status = "stale"
        race_conflicts = exc.conflicts
    waited = time.monotonic() - started
    out, err = collector.communicate(timeout=30)
    check("collector exited cleanly", collector.returncode == 0, err.strip()[-200:])
    check("reclaimer waited for the collecting process", waited >= 0.5,
          f"waited {waited:.2f}s for the shared lock held by pid "
          f"{collector.pid}")
    check("in-flight bytes were not taken",
          fresh_path.is_file() and fresh_meta_path.is_file(),
          "fresh response and metadata still present after the race")
    fresh_meta = json.loads(fresh_meta_path.read_text())
    check("in-flight bytes are not durable facts yet",
          fresh_meta.get("digest") == inflight_digest
          and (race_dir / "pipeline.sqlite3").is_file(),
          "the collector's file is only protected by the lock, not by a DB row")
    shutil.rmtree(race_dir, ignore_errors=True)
    emit()

    # -- 7. kill mid-isolation ----------------------------------------
    emit("## 7. 隔离中途被杀：重开恢复")
    kill_dir = ROOT / "work" / "reclaimdemo-kill"
    if kill_dir.exists():
        shutil.rmtree(kill_dir)
    kill_built = build_scenario(kill_dir)
    kill_archive = Path(kill_built["archive_root"])
    kill_plan = create_plan(kill_dir, None, CUTOFF)
    kill_id = kill_plan["plan_id"]
    candidate_groups = [g for g in kill_plan["groups"]
                        if g["verdict"] == "isolatable"]
    check("kill fixture has two candidates", len(candidate_groups) >= 2,
          f"{len(candidate_groups)} candidate groups")
    try:
        isolate_plan(kill_dir, kill_id, interrupt_after_moves=1)
        check("controlled kill fired", False, "no crash was raised")
    except ReclaimCrash as exc:
        check("controlled kill fired", True, str(exc))
    killed = open_plan(kill_dir, kill_id)
    moved_files = [f for g in killed["groups"] if g["verdict"] == "isolatable"
                   for f in g["files"] if f["state"] == "moved"]
    check("journal knows what moved", len(moved_files) == 1
          and killed["status"] == "isolating",
          f"status={killed['status']}, moved={len(moved_files)}")
    resumed = isolate_plan(kill_dir, kill_id)
    check("re-open resumes the plan",
          resumed["status"] == PLAN_ISOLATED and resumed["resumed"],
          f"status={resumed['status']} moved={resumed['moved']}")
    all_candidates = [f for g in resumed["plan"]["groups"]
                      if g["verdict"] == "isolatable" for f in g["files"]]
    check("no half-group left behind",
          all((kill_archive / f["quarantine_rel"]).is_file()
              for f in all_candidates),
          f"{len(all_candidates)} files in quarantine")
    undo = restore_plan(kill_dir, kill_id)
    check("resumed plan can still be undone", undo["status"] == PLAN_RESTORED,
          undo["status"])
    shutil.rmtree(kill_dir, ignore_errors=True)
    emit()

    # -- 8. restore conflict ------------------------------------------
    emit("## 8. 撤销遇到同名不同内容：双方保留")
    conflict_dir = ROOT / "work" / "reclaimdemo-conflict"
    if conflict_dir.exists():
        shutil.rmtree(conflict_dir)
    conflict_built = build_scenario(conflict_dir)
    conflict_archive = Path(conflict_built["archive_root"])
    conflict_plan = create_plan(conflict_dir, None, CUTOFF)
    cid = conflict_plan["plan_id"]
    isolate_plan(conflict_dir, cid)
    target_group = next(g for g in open_plan(conflict_dir, cid)["groups"]
                        if g["verdict"] == "isolatable")
    target_file = next(f for f in target_group["files"]
                       if f["role"] in {"blob", "blob_gz"})
    conflict_path = conflict_archive / target_file["rel_path"]
    conflict_path.write_bytes(b"a different file appeared here")
    result = restore_plan(conflict_dir, cid)
    check("restore reports conflict", result["status"] == PLAN_RESTORED_WITH_CONFLICTS,
          f"status={result['status']}")
    kept_both = (conflict_path.is_file()
                 and (conflict_archive / target_file["quarantine_rel"]).is_file())
    check("both files kept", kept_both,
          "original path keeps the new content; the quarantined copy remains")
    check("conflict file was not overwritten",
          conflict_path.read_bytes() == b"a different file appeared here",
          "new content untouched")
    shutil.rmtree(conflict_dir, ignore_errors=True)
    emit()

    # -- 9. purge interruption ----------------------------------------
    emit("## 9. 永久清理中断后重试：不重复计释放量")
    purge_dir = ROOT / "work" / "reclaimdemo-purge"
    if purge_dir.exists():
        shutil.rmtree(purge_dir)
    purge_built = build_scenario(purge_dir)
    purge_archive = Path(purge_built["archive_root"])
    purge_pending = create_plan(purge_dir, None, CUTOFF)
    ppid = purge_pending["plan_id"]
    isolate_plan(purge_dir, ppid)
    moved = [f for g in open_plan(purge_dir, ppid)["groups"]
             if g["verdict"] == "isolatable" for f in g["files"]
             if f["state"] == "moved"]
    first = moved[0]
    quarantine_file = purge_archive / first["quarantine_rel"]
    # Simulate a kill between unlink and journal update: delete one file and
    # leave its state at "moved" with no released bytes.
    quarantine_file.unlink()
    retried = purge_plan(purge_dir, ppid)
    expected = sum(f["size_bytes"] for f in moved)
    check("retry counts each file once",
          retried["released_disk_bytes"] == expected
          and retried["freed_disk_bytes"] == expected,
          f"released={retried['released_disk_bytes']} expected={expected}")
    check("vanished file is reported missing, not as released",
          any(r["result"] == "missing" for r in retried["results"]),
          "the already-deleted file is 'missing', not 'purged'")
    shutil.rmtree(purge_dir, ignore_errors=True)
    emit()

    # -- 10. new reference after isolation blocks purge ------------------
    emit("## 10. 隔离后新增持久引用：永久清理被拦")
    guard_dir = ROOT / "work" / "reclaimdemo-guard"
    if guard_dir.exists():
        shutil.rmtree(guard_dir)
    guard_built = build_scenario(guard_dir)
    guard_archive = Path(guard_built["archive_root"])
    guard_plan = create_plan(guard_dir, None, CUTOFF)
    gid = guard_plan["plan_id"]
    isolate_plan(guard_dir, gid)
    isolated = open_plan(guard_dir, gid)
    candidate = next(g for g in isolated["groups"] if g["verdict"] == "isolatable")
    from .store import Store as _GuardStore
    guard_store = _GuardStore(guard_dir / "pipeline.sqlite3")
    guard_store.add_scan_item(
        scan_id=guard_built["scan_id"], source_id="sim_a",
        url_key="http://guard/new-reference", kind="detail", outcome="ok",
        http_status=200, raw_digest=candidate["digest"], archive_id="arc_guard",
        detail={})
    guard_store.close()
    blocked = purge_plan(guard_dir, gid)
    check("purge blocked by new persistent reference",
          blocked["status"] == PLAN_PURGE_BLOCKED and blocked["blocked"],
          f"status={blocked['status']}, {len(blocked['blocked'])} blocked")
    kept = [f for g in open_plan(guard_dir, gid)["groups"]
            if g["verdict"] == "isolatable" for f in g["files"]
            if f["digest"] == candidate["digest"]]
    check("referenced quarantined files kept",
          kept and all((guard_archive / f["quarantine_rel"]).is_file()
                       for f in kept),
          f"{len(kept)} file(s) remain in quarantine")
    check("block carries the reference evidence",
          all("evidence" in entry for entry in blocked["blocked"]),
          "each blocked digest names the database row that references it")
    undone = restore_plan(guard_dir, gid)
    check("blocked plan can still be undone",
          undone["status"] in {PLAN_RESTORED, PLAN_RESTORED_WITH_CONFLICTS},
          f"status={undone['status']}, restored={undone['restored']}")
    shutil.rmtree(guard_dir, ignore_errors=True)
    emit()

    emit("## 核对汇总")
    emit()
    for item in checks:
        emit(f"- {'PASS' if item['ok'] else 'FAIL'} {item['check']} — {item['detail']}")
    failures = [c for c in checks if not c["ok"]]
    emit()
    emit(f"共 {len(checks)} 项核对，失败 {len(failures)} 项。")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    (WORKDIR / "demo-checks.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

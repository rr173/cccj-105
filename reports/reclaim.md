# 原始留档空间回收 · 端到端演示

工作目录：`work/reclaimdemo`；宽限截止：2026-10-01T00:00:00+00:00

## 1. 预览：只读分类，不改文件
- [PASS] preview exit code: 
- [PASS] preview moved no file: 24 files / 6746 bytes before and after
- [PASS] preview created no plan: reclaim.sqlite3 absent after preview

| 分类 | 文件组 | 磁盘字节 |
| --- | --- | --- |
| protected | 4 | 2462 |
| within_grace | 1 | 647 |
| isolatable | 2 | 1240 |
| anomaly | 7 | 2516 |

预计可隔离占用：1240 字节；已释放：0 字节（预览从不宣称已释放空间）
- [PASS] cross-date copy protected: both date copies of the referenced digest are protected
- [PASS] fresh orphan within grace: new unreferenced file is not a candidate
- [PASS] gzip judged by decompressed bytes: valid gzip orphan is isolatable
- [PASS] diagnostic only: corrupt_gzip: verdict=anomaly
- [PASS] diagnostic only: incomplete_group: verdict=anomaly
- [PASS] diagnostic only: digest_mismatch: verdict=anomaly
- [PASS] diagnostic only: metadata_source_conflict: verdict=anomaly
- [PASS] diagnostic only: metadata_size_conflict: verdict=anomaly
- [PASS] diagnostic only: symlink: verdict=anomaly
- [PASS] diagnostic only: foreign_path: verdict=anomaly

## 2. 保存计划并可再次打开
- [PASS] plan re-opens: rcl_ee54fc3c6ae14dc9 status=planned
- [PASS] plan moved no file: no file was moved by planning
- [PASS] per-file rows recorded: 4 planned file rows with hash and size

## 3. 隔离 → 撤销（文件仍在隔离区，可撤销）
- [PASS] isolation moved candidates: moved=4
- [PASS] version bytes untouched: version-referenced response unchanged
- [PASS] scan-only response untouched: scan-item-referenced response still on disk
- [PASS] every candidate left its original path: 4 moved; 0 still present
- [PASS] quarantine holds the files: 4 files under .reclaim/quarantine/rcl_ee54fc3c6ae14dc9
- [PASS] no space reported as freed yet: quarantined=1240, released=0
- [PASS] restore undoes isolation: status=restored restored=4
- [PASS] restored bytes identical: every restored file hashes to its response digest

## 4. 重新计划 → 隔离 → 永久清理
- [PASS] purge deleted exactly the quarantined bytes: released=1240 expected=1240
- [PASS] on-disk delta matches released bytes: 6746 - 5506 = 1240
- [PASS] purge status: purged
- [PASS] versions and scan facts preserved by purge: 2 version rows, 1 scan item rows remain
- [PASS] purge retry is idempotent: retry reports zero new purge and the same released total
- [PASS] purged plan cannot be restored: plan rcl_c070792fd1164800 was permanently purged; it is not restorable

## 5. 保存计划后新增引用：拒绝过期计划
- [PASS] stale plan refused: 3 conflict(s); plan rcl_67e467ffad164eea is stale: 3 conflict(s) found during revalidation; no file was moved
- [PASS] stale plan moved nothing: candidate file still at its original path
- [PASS] stale plan marked: stale

## 6. 采集写盘但尚未提交：两个进程，锁覆盖入库窗口
- [PASS] collector wrote bytes before committing: second process holds bytes not yet referenced
- [PASS] collector exited cleanly: 
- [PASS] reclaimer waited for the collecting process: waited 1.12s for the shared lock held by pid 5142
- [PASS] in-flight bytes were not taken: fresh response and metadata still present after the race
- [PASS] in-flight bytes are not durable facts yet: the collector's file is only protected by the lock, not by a DB row

## 7. 隔离中途被杀：重开恢复
- [PASS] kill fixture has two candidates: 2 candidate groups
- [PASS] controlled kill fired: simulated process kill after 1 move(s)
- [PASS] journal knows what moved: status=isolating, moved=1
- [PASS] re-open resumes the plan: status=isolated moved=3
- [PASS] no half-group left behind: 4 files in quarantine
- [PASS] resumed plan can still be undone: restored

## 8. 撤销遇到同名不同内容：双方保留
- [PASS] restore reports conflict: status=restored_with_conflicts
- [PASS] both files kept: original path keeps the new content; the quarantined copy remains
- [PASS] conflict file was not overwritten: new content untouched

## 9. 永久清理中断后重试：不重复计释放量
- [PASS] retry counts each file once: released=1240 expected=1240
- [PASS] vanished file is reported missing, not as released: the already-deleted file is 'missing', not 'purged'

## 10. 隔离后新增持久引用：永久清理被拦
- [PASS] purge blocked by new persistent reference: status=purge_blocked, 1 blocked
- [PASS] referenced quarantined files kept: 2 file(s) remain in quarantine
- [PASS] block carries the reference evidence: each blocked digest names the database row that references it
- [PASS] blocked plan can still be undone: status=restored, restored=2

## 核对汇总

- PASS preview exit code — 
- PASS preview moved no file — 24 files / 6746 bytes before and after
- PASS preview created no plan — reclaim.sqlite3 absent after preview
- PASS cross-date copy protected — both date copies of the referenced digest are protected
- PASS fresh orphan within grace — new unreferenced file is not a candidate
- PASS gzip judged by decompressed bytes — valid gzip orphan is isolatable
- PASS diagnostic only: corrupt_gzip — verdict=anomaly
- PASS diagnostic only: incomplete_group — verdict=anomaly
- PASS diagnostic only: digest_mismatch — verdict=anomaly
- PASS diagnostic only: metadata_source_conflict — verdict=anomaly
- PASS diagnostic only: metadata_size_conflict — verdict=anomaly
- PASS diagnostic only: symlink — verdict=anomaly
- PASS diagnostic only: foreign_path — verdict=anomaly
- PASS plan re-opens — rcl_ee54fc3c6ae14dc9 status=planned
- PASS plan moved no file — no file was moved by planning
- PASS per-file rows recorded — 4 planned file rows with hash and size
- PASS isolation moved candidates — moved=4
- PASS version bytes untouched — version-referenced response unchanged
- PASS scan-only response untouched — scan-item-referenced response still on disk
- PASS every candidate left its original path — 4 moved; 0 still present
- PASS quarantine holds the files — 4 files under .reclaim/quarantine/rcl_ee54fc3c6ae14dc9
- PASS no space reported as freed yet — quarantined=1240, released=0
- PASS restore undoes isolation — status=restored restored=4
- PASS restored bytes identical — every restored file hashes to its response digest
- PASS purge deleted exactly the quarantined bytes — released=1240 expected=1240
- PASS on-disk delta matches released bytes — 6746 - 5506 = 1240
- PASS purge status — purged
- PASS versions and scan facts preserved by purge — 2 version rows, 1 scan item rows remain
- PASS purge retry is idempotent — retry reports zero new purge and the same released total
- PASS purged plan cannot be restored — plan rcl_c070792fd1164800 was permanently purged; it is not restorable
- PASS stale plan refused — 3 conflict(s); plan rcl_67e467ffad164eea is stale: 3 conflict(s) found during revalidation; no file was moved
- PASS stale plan moved nothing — candidate file still at its original path
- PASS stale plan marked — stale
- PASS collector wrote bytes before committing — second process holds bytes not yet referenced
- PASS collector exited cleanly — 
- PASS reclaimer waited for the collecting process — waited 1.12s for the shared lock held by pid 5142
- PASS in-flight bytes were not taken — fresh response and metadata still present after the race
- PASS in-flight bytes are not durable facts yet — the collector's file is only protected by the lock, not by a DB row
- PASS kill fixture has two candidates — 2 candidate groups
- PASS controlled kill fired — simulated process kill after 1 move(s)
- PASS journal knows what moved — status=isolating, moved=1
- PASS re-open resumes the plan — status=isolated moved=3
- PASS no half-group left behind — 4 files in quarantine
- PASS resumed plan can still be undone — restored
- PASS restore reports conflict — status=restored_with_conflicts
- PASS both files kept — original path keeps the new content; the quarantined copy remains
- PASS conflict file was not overwritten — new content untouched
- PASS retry counts each file once — released=1240 expected=1240
- PASS vanished file is reported missing, not as released — the already-deleted file is 'missing', not 'purged'
- PASS purge blocked by new persistent reference — status=purge_blocked, 1 blocked
- PASS referenced quarantined files kept — 2 file(s) remain in quarantine
- PASS block carries the reference evidence — each blocked digest names the database row that references it
- PASS blocked plan can still be undone — status=restored, restored=2

共 53 项核对，失败 0 项。

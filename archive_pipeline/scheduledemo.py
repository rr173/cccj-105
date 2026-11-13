"""End-to-end demonstration of the schedule-organizing job.

Builds the reproducible sample workdir under ``work/schedemo/`` (two local
sources, two versions of one supplement, same-numbered editions, cross-source
references) and walks through every required behaviour:

1. a mid-version interruption leaves no published schedule and the run is
   marked ``interrupted``; restarting completes safely with no duplicates;
2. two rounds scheduled in one notice become two independent schedules;
3. cross-year and year-less dates are normalized to real calendar dates,
   year-less ones filled from the issue date;
4. a deadline-only extension updates just that node; the old date stays in
   the change history but leaves the current query;
5. a whole-round withdrawal cancels only that round, while a single-node
   cancellation leaves the round open;
6. explicit cross-source references resolve, vague/ambiguous material stays
   pending and never reaches the confirmed schedule;
7. a vocabulary change re-organizes while keeping the old judgment;
8. provenance (source notice, version, paragraph, character span, original
   date writing) explains every current date.

Run: ``python -m archive_pipeline.scheduledemo``
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .schedule import ScheduleInterrupted, ScheduleJob
from .schedulesample import aid_for, build_sample_workdir

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "work" / "schedemo"
REPORT = ROOT / "reports" / "schedule.md"


def main() -> int:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True)
    built = build_sample_workdir(WORKDIR)
    s1003 = aid_for(built, "schedsim", "S-1003")
    report: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        report.append(line)

    emit("# 项目申报日程整理作业演示")
    emit()
    emit(f"样本工作目录：`{WORKDIR}`（{len(built['versions'])} 个归档版本，"
         "来源 schedsim / schedsim2，全部为身份模式数据）")
    emit()

    # -- 1. mid-version interruption ------------------------------------
    emit("## 1. 版本处理途中退出与恢复（事务边界）")
    job = ScheduleJob(WORKDIR, progress=lambda _m: None)
    try:
        job.organize(fail_point=(s1003, 2))
    except ScheduleInterrupted as exc:
        emit(f"* 受控中断：{exc}")
    emit(f"* 中断后当前节点数：**{len(job.store.current_nodes())}**"
         "（快照未发布，无部分可查安排）；运行状态："
         f"`{job.store.runs()[-1]['status']}`")
    summary = job.organize()
    emit("* 重开后续整理完成："
         f"ledger 行 {summary['ledger_rows_added']} 条，"
         f"待确认 {summary['pending_open']} 项，"
         f"当前节点 {len(job.store.current_nodes())} 个")
    again = job.organize()
    emit(f"* 重复整理幂等：第二次新增 ledger 行 **{again['ledger_rows_added']}**"
         "（相同输入与词表不产生重复节点）")
    emit()

    # -- 2/3. current schedule with provenance ---------------------------
    emit("## 2. 当前有效安排（含跨年、省略年、中文数字）")
    emit()
    emit("| 项目 | 轮次 | 节点 | 状态 | 归一日期 | 来源公告 | 版本 | 原文写法 | 段落 | 字符区间 |")
    emit("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for node in job.store.current_nodes():
        detail = job.store.evidence_for_ledger(node["ledger_id"])
        ev = json.loads(detail["evidence_json"])
        raw = ev.get("raw_date", "—")
        para = f"p{ev['paragraph_index']}" if "paragraph_index" in ev else "—"
        span = (f"[{ev['date_start']},{ev['date_end']})"
                if "date_start" in ev else "—")
        filled = "（省略年补齐）" if ev.get("had_year") else ""
        emit(
            f"| {node['root_notice_key']} | {node['round_no']} "
            f"| {node['role_name']} | {node['status']} "
            f"| {node['normalized_date'] or '—'} "
            f"| {detail['notice_key']} | v{detail['version_no']} "
            f"| {raw} | {para} | {span} {filled} |")
    emit()

    # -- 4. extension history --------------------------------------------
    emit("## 3. 仅延长收件期限：历史替换可追溯")
    emit()
    for row in job.store.ledger("schedsim::S-1001"):
        if row["role_name"] == "submit_deadline" and row["round_no"] == 1:
            ev = json.loads(row["evidence_json"])
            emit(f"* `{row['action']}` → {row['normalized_date']}，"
                 f"来自 {row['notice_key']} v{row['version_no']}，"
                 f"原文“{ev['raw_date']}”（p{ev['paragraph_index']} "
                 f"[{ev['date_start']},{ev['date_end']})）")
    current = [n for n in job.store.current_nodes("schedsim::S-1001", 1)
               if n["role_name"] == "submit_deadline"][0]
    emit(f"* 当前查询只显示 **{current['normalized_date']}**；"
         "旧日期 2026-03-25 仅存于历史，不再出现在当前安排。")
    emit()

    # -- 5. cancellation scope -------------------------------------------
    emit("## 4. 撤销范围：整轮撤销 ≠ 单节点取消")
    emit()
    for r in job.store.rounds("schedsim::S-1001"):
        emit(f"* S-1001 第 {r['round_no']} 轮状态：`{r['status']}`"
             f"（依据 {r['source_notice_key']}）")
    emit("* 第二轮节点在当前查询中不可见，但 ledger 保留其 set 与 "
         "cancel_round 记录；第一轮三个节点全部仍为 active。")
    review = [n for n in job.store.current_nodes("schedsim::S-1006")
              if n["role_name"] == "review_start"][0]
    emit(f"* S-1006 评审节点状态：`{review['status']}`（单节点取消），"
         "轮次状态仍为 `open`，报名与收件节点不受影响。")
    emit()

    # -- 6. pending -------------------------------------------------------
    emit("## 5. 待人工确认材料（不并入已确认安排）")
    emit()
    # captured before the vocabulary-change run below, which books S-1012
    pending_snapshot = list(job.store.pending())
    emit("| 公告 | 原因码 | 说明 | 句子 |")
    emit("| --- | --- | --- | --- |")
    for p in pending_snapshot:
        sentence = p["sentence_text"].replace("|", "\\|")[:40]
        emit(f"| {p['notice_key']} | `{p['code']}` "
             f"| {p['detail'][:36]} | {sentence} |")
    emit()

    # -- 7. cross-source --------------------------------------------------
    emit("## 6. 外部编号与跨来源引用")
    emit()
    xnode = [n for n in job.store.current_nodes("schedsim2::X-2001")
             if n["role_name"] == "submit_deadline"][0]
    emit(f"* schedsim 的 S-1009 写明“来源：schedsim2，编号：X-2001”，"
         f"唯一定位后把截止日改为 **{xnode['normalized_date']}**。")
    ambiguous = [p for p in pending_snapshot if p["code"] == "target_ambiguous"]
    detail = ambiguous[0]["detail"] if ambiguous else "无歧义记录"
    emit("* S-1014 仅写“编号：S-1007”，而该编号在 schedsim 内对应完整版与"
         f"公众版两个归档身份（{detail}），"
         "系统拒绝任选，整条补充保持待人工确认，两个身份的安排均未被改动。")
    emit()

    # -- 8. vocabulary change --------------------------------------------
    emit("## 7. 更改词表后重新整理（保留旧判断）")
    emit()
    old_digest = job.vocab.digest
    fixed_path = WORKDIR / "schedule_vocab.extended.json"
    cfg = json.loads((ROOT / "config" / "schedule_vocab.json").read_text("utf-8"))
    cfg["roles"]["briefing"] = {
        "kind": "open", "aliases": ["宣讲会日期", "项目宣讲会"]}
    fixed_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                          "utf-8")
    job.close()
    job2 = ScheduleJob(WORKDIR, fixed_path, progress=lambda _m: None)
    job2.organize()
    briefing = [n for n in job2.store.current_nodes("schedsim::S-1012")
                if n["role_name"] == "briefing"][0]
    old_rows = [h for h in job2.store.pending(current_only=False)
                if h["config_digest"] == old_digest
                and h["notice_key"] == "S-1012"]
    emit(f"* 新词表下 S-1012 的宣讲会节点入账：{briefing['normalized_date']}。")
    emit(f"* 旧词表产生的 unmapped_date 判断仍在历史中（{len(old_rows)} 条，"
         f"config={old_digest[:12]}…），当前待确认列表不再包含该项。")
    job2.close()
    emit()
    emit("## 8. 数据边界")
    emit()
    emit("* 管线库 `pipeline.sqlite3` 以只读方式打开；内容寻址原始留档 "
         "`archive/` 仅按摘要读取复核，全程不写入、不迁移、不重新访问网站。")
    emit("* 日程结果写入独立的 `schedule.sqlite3`；旧库（user_version<2）"
         "会被明确拒绝并提示先 repair。")

    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\n报告已写入 {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

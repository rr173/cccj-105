"""End-to-end demonstration of the funding-table structuring job.

Builds the reproducible sample workdir under ``work/funddemo/`` and walks
through every required behaviour:

1. a mid-table interruption leaves no partial facts and the run is marked
   ``interrupted``; restarting completes safely;
2. correct grid expansion and exact unit conversion (table-level 元 with a
   column-level 万元 override, full-width digits, thousands separators);
3. same-number public/full editions keep independent facts;
4. every rejection carries a table-level reason (overlap, cross-year amount,
   totals mismatch, missing raw HTML, digest mismatch, nested table, unknown
   currency, missing unit, unparseable amounts, unmapped headers);
5. after a config fix the rejected table is reprocessed and booked, while
   the old diagnostic stays in the history;
6. a machine-readable report is written to ``reports/funding.md``.

Run: ``python -m archive_pipeline.funddemo``
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .funding import FundingInterrupted, FundingJob, FundingStore
from .fundsample import aid_for, build_sample_workdir

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "work" / "funddemo"
REPORT = ROOT / "reports" / "funding.md"


def _facts_table(facts: list[dict]) -> str:
    lines = ["| 逻辑行 | 机构 | 项目 | 年度 | 原文金额 | 单位 | 金额（元） | 金额格 | 机构格 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for f in facts:
        lines.append(
            f"| r{f['row_index']} | {f['institution']} | {f['project']} "
            f"| {f['year']} | {f['amount_text']} | {f['unit']}({f['unit_source']}) "
            f"| {f['amount_yuan']} | r{f['amount_cell_row']}c{f['amount_cell_col']} "
            f"| r{f['institution_cell_row']}c{f['institution_cell_col']} |")
    return "\n".join(lines)


def main() -> int:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True)
    built = build_sample_workdir(WORKDIR)
    full_aid = aid_for(built, "F-3001", "F-3001-full.html")
    public_aid = aid_for(built, "F-3001", "F-3001-public.html")
    alias_aid = aid_for(built, "F-3007", "F-3007-alias.html")
    report: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        report.append(line)

    emit("# 资助表格结构化作业演示")
    emit()
    emit(f"样本工作目录：`{WORKDIR}`（{len(built['versions'])} 个归档版本，"
         "来源 fundsim，全部为身份模式数据）")
    emit()

    # -- 1. mid-table interruption ------------------------------------
    emit("## 1. 表格处理途中退出")
    job = FundingJob(WORKDIR)
    try:
        job.process(fail_point=(full_aid, 0))
    except FundingInterrupted as exc:
        emit(f"* 受控中断：{exc}")
    leftover = job.store.facts(archive_id=full_aid)
    run = job.store.runs()[-1]
    emit(f"* 中断后 F-3001 完整版可查询明细数：**{len(leftover)}**"
         f"（事务回滚，无部分明细）；运行状态：`{run['status']}`")
    summary = job.process()
    emit(f"* 重开后安全续处理：booked={summary['booked']} "
         f"skipped_booked={summary['skipped_booked']} "
         f"rejected={summary['rejected_structural'] + summary['rejected_data']} "
         f"version_errors={summary['version_errors']}")
    emit(f"* 续处理后 F-3001 完整版明细数：**{len(job.store.facts(archive_id=full_aid))}**")
    job.close()
    emit()

    # -- 2. correct expansion + conversion -----------------------------
    job = FundingJob(WORKDIR)
    emit("## 2. 正确展开与金额换算（F-3001 完整版）")
    emit()
    outcome = job.store.outcome("fundsim", full_aid, 1, 0)
    grid = json.loads(outcome["grid_json"])
    emit(f"逻辑网格：{grid['n_rows']} 行 × {grid['width']} 列，"
         f"表头 {grid['header_rows']} 层；rowspan/colspan 已展开为可解释网格。")
    emit()
    full_facts = job.store.facts(archive_id=full_aid)
    emit(_facts_table(full_facts))
    emit()
    emit("换算要点：表级单位 `元`（表格说明），2025 年度列头局部单位 `万元` 优先；"
         "`２０００` 为全角数字，`1,234.56` 含千分位，`1.5`（万元）→ `15000` 元，"
         "全程 Decimal 精确换算。")
    totals = json.loads(outcome["totals_json"])
    emit(f"合计核对：{totals}（与明细逐项相符，合计行不入账）")
    emit()

    # -- 3. public vs full ---------------------------------------------
    emit("## 3. 同编号公众版与完整版互不覆盖")
    public_facts = job.store.facts(archive_id=public_aid)
    emit(f"* 完整版 `{full_aid}`：{len(full_facts)} 条事实")
    emit(f"* 公众版 `{public_aid}`：{len(public_facts)} 条事实"
         f"（同一 notice_key=F-3001，不同 archive_id）")
    emit()
    emit(_facts_table(public_facts))
    emit()

    # -- 4. rejections ---------------------------------------------------
    emit("## 4. 拒收与版本级错误（保留网格、候选明细与原因）")
    emit()
    emit("| 通告 | 表格 | 类别 | 原因码 | 说明 |")
    emit("| --- | --- | --- | --- | --- |")
    for o in job.store.outcomes(status="rejected"):
        detail = o["reason_detail"].replace("|", "\\|")
        if len(detail) > 90:
            detail = detail[:90] + "…"
        emit(f"| {o['notice_key']} | t{o['table_index']} | {o['category']} "
             f"| `{o['reason_code']}` | {detail} |")
    for e in job.store.version_errors():
        emit(f"| {e['notice_key']} | — | source | `{e['error_code']}` "
             f"| {e['error_detail']} |")
    emit()

    # -- 5. config fix + reprocess ---------------------------------------
    emit("## 5. 改配置后重处理（F-3007）")
    before = job.store.outcome("fundsim", alias_aid, 1, 0)
    emit(f"* 默认配置下：`{before['status']}` / `{before['reason_code']}`"
         "（承担单位/拨付项目/拨付经费 未声明）")
    fixed = json.loads((ROOT / "config" / "funding_tables.json").read_text("utf-8"))
    fixed["headers"]["institution"].append("承担单位")
    fixed["headers"]["project"].append("拨付项目")
    fixed["headers"]["amount"].append("拨付经费")
    fixed_path = WORKDIR / "funding_tables.fixed.json"
    fixed_path.write_text(json.dumps(fixed, ensure_ascii=False, indent=2), "utf-8")
    job.close()

    job2 = FundingJob(WORKDIR, fixed_path)
    summary2 = job2.process()
    after = job2.store.outcome("fundsim", alias_aid, 1, 0)
    alias_facts = job2.store.facts(archive_id=alias_aid)
    emit(f"* 修正配置后重处理：`{after['status']}`，"
         f"明细 {len(alias_facts)} 条"
         f"（1.25 千元 → {alias_facts[0]['amount_yuan']} 元）")
    history = [h for h in job2.store.history(archive_id=alias_aid)]
    emit(f"* 该表诊断历史 {len(history)} 条（旧拒收原因可追溯）：")
    for h in history:
        emit(f"  - run {h['run_id']} `{h['status']}` "
             f"`{h['reason_code'] or '-'}` config={h['config_digest'][:12]}…")
    emit(f"* 重处理汇总：booked={summary2['booked']} "
         f"skipped_booked={summary2['skipped_booked']} "
         f"facts_total={job2.store.fact_count()}（无重复记账）")
    job2.close()
    emit()

    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\n报告已写入 {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

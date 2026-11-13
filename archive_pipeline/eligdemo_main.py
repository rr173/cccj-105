"""End-to-end demonstration of the offline eligibility pre-screen job.

Builds the reproducible sample workdir under ``work/eligdemo/`` and walks through:

1. clause inspection (``check``): binding, verified citations, typed scopes;
2. three rejected clauses — a shifted citation, a clause bound to the wrong
   variant of the same notice number, and an authoring error (missing version);
   none of them produces a judgment or even reads an application;
3. a batch pre-screen of the synthetic JSONL file, in input order, with the
   three-way summary;
4. the full explanation of one application (all branches, member counts with
   person intervals, unknown-value reasons, JSON Pointers, verified citations);
5. the team-count example "at least two doctorates with three years each"
   under four scenarios: complete satisfying, incomplete but sufficient,
   missing field on the threshold, and two people each holding half;
6. a damaged second half of a file: bad JSON, duplicate id, broken member
   structure — every case is reported with its line, the run is explicitly
   incomplete, already-written rows remain, and no success summary is stored;
7. a generated large file processed line by line (peak memory is one
   application), with the same order-preserving results;
8. the report is written to ``reports/eligibility.md``.

All data is synthetic; the tool assists screening and never replaces the
human decision based on the traceable clause.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .eligclause import ClauseError, load_clause
from .eligdemo import build_eligibility_sample, sample_applications
from .eligibility import EligibilityJob, explain_application
from .eligengine import explain_result, render_tree

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "work" / "eligdemo"
REPORT = ROOT / "reports" / "eligibility.md"
CLAUSE = ROOT / "scenario" / "eligibility" / "clauses" / "E-9001-v1.json"
BASE_APPLICATIONS = (ROOT / "scenario" / "eligibility" / "applications"
                     / "applications-basic.jsonl")
CITATION_DIR = ROOT / "scenario" / "eligibility" / "clauses" / "citations"


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _explained(payload: dict) -> dict:
    """Fully expanded result (refs resolved) for reading/reporting."""
    return explain_result(payload["explanation"])


def _member_rows(tree: dict) -> list[str]:
    rows = ["| 成员 | JSON Pointer | 结果 | 学历 | 年限 | 判断依据 |",
            "| --- | --- | --- | --- | --- | --- |"]
    for member in tree["members"]:
        predicate = member["predicate"]
        degree = years = "—"
        reason_bits = []
        for child in predicate.get("children", []):
            if child.get("node") in ("compare", "in"):
                if child["field"] == "degree_level":
                    degree = _j(child["input"]["raw_value"])
                elif child["field"] == "years_experience":
                    years = _j(child["input"]["raw_value"])
            elif "resolved" in child:
                resolved = child["resolved"]["tree"]
                if resolved.get("field") == "degree_level":
                    degree = _j(resolved["input"]["raw_value"])
                elif resolved.get("field") == "years_experience":
                    years = _j(resolved["input"]["raw_value"])
            reason_bits.append(f"{child.get('node')}={child['result']}")
        rows.append(f"| {member['member_index']} | `{member['json_pointer']}` "
                    f"| {member['result']} | {degree} | {years} "
                    f"| {'; '.join(reason_bits)} |")
    return rows


def main() -> int:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True)
    info = build_eligibility_sample(WORKDIR)
    report: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        report.append(line)

    def find(application_id: str) -> dict:
        payload = explain_application(WORKDIR, application_id)
        if payload is None:
            raise KeyError(application_id)
        return payload

    def load_root_for(_row) -> str:
        return clause.root

    emit("# 离线资格预审作业演示")
    emit()
    emit(f"样本工作目录：`{WORKDIR}`（{len(info['built']['versions'])} 个归档版本，"
         "来源 `eligsample`，全部为合成资料、身份模式数据）")
    emit()

    # -- 1. clause check -------------------------------------------------
    emit("## 1. 条款检查（先于任何申请读取）")
    emit()
    clause = load_clause(WORKDIR / "pipeline.sqlite3", CLAUSE)
    emit(f"* 条款文件：`{CLAUSE.relative_to(ROOT)}`")
    emit(f"* 绑定：`{clause.binding.source_id}` / "
         f"`{clause.binding.archive_id}` / v{clause.binding.version_no}"
         f"（{clause.binding.notice_key}，发布 {clause.binding.issued_on}）")
    emit(f"* 条款摘要（canonical JSON SHA-256）：`{clause.digest}`")
    emit(f"* 正文摘要（body SHA-256）：`{clause.binding.body_sha256}`")
    emit()
    emit("| 条件 ID | 作用域 | 引文区间 | 引文 | 核对 |")
    emit("| --- | --- | --- | --- | --- |")
    for cid in clause.condition_order:
        condition = clause.conditions[cid]
        citation = condition.citation
        emit(f"| `{cid}` | {condition.scope} | [{citation['start']},"
             f"{citation['end']}) | {citation['quote']} "
             f"| body[start:end] == quote ✔ |")
    emit()

    # -- 2. rejected clauses --------------------------------------------
    emit("## 2. 条款无效时拒绝读取申请、不产出判定")
    emit()
    emit("| 文件 | 错误类别 | 位置与原因 |")
    emit("| --- | --- | --- |")
    rejected = []
    for name, path in [
            ("引文整体错位 1 字符", CITATION_DIR / "E-9001-v1-shifted.json"),
            ("绑定公众版却引用完整版正文", CITATION_DIR / "E-9001-to-public.json"),
            ("绑定不存在的版本（把 v1 文本指向 v2）",
             CITATION_DIR / "E-9001-v2-as-v1.json")]:
        try:
            load_clause(WORKDIR / "pipeline.sqlite3", path)
            raise AssertionError("expected the clause to be rejected")
        except ClauseError as exc:
            codes = sorted({issue["code"] for issue in exc.issues})
            detail = exc.issues[0]["detail"].replace("|", "\\|")
            emit(f"| {path.name} | {', '.join(codes)} | "
                 f"{exc.issues[0]['location']}: {detail} |")
            rejected.append((path, codes, exc.issues))
    emit()
    emit("上述三类（不存在版本 / 同编号串档 / 引文不符）均在条款校验阶段被拒绝，"
         "程序以退出码 3 结束，未读取任何申请。另有条款编录错误（字段拼写、"
         "作用域、常量类型、未定义引用、引用环、`at_least` 非整数等）同样在"
         "此阶段报出，见 §8 与测试。")
    emit()

    # -- 3. batch run ----------------------------------------------------
    emit("## 3. 批量预审（按输入顺序逐行）")
    emit()
    job = EligibilityJob(WORKDIR, CLAUSE)
    outcome = job.run(BASE_APPLICATIONS)
    emit(f"* 运行 `{outcome.run_id}` 状态：`{outcome.status}`；"
         f"写入 {outcome.applications_written} 条")
    counts = outcome.summary["counts"]
    emit(f"* 汇总：满足 **{counts['satisfied']}**，不满足 "
         f"**{counts['not_satisfied']}**，待补证 "
         f"**{counts['needs_evidence']}**（共 {outcome.summary['applications_total']} 份）")
    emit(f"* 条款摘要 `{outcome.summary['clause_digest']}`；"
         f"输入摘要 `{outcome.summary['input']['sha256']}`"
         f"（{outcome.summary['input']['bytes']} 字节，"
         f"{outcome.summary['input']['lines']} 行）")
    emit()
    emit("| 序号 | 输入行 | application_id | 结论 | 根条件 |")
    emit("| --- | --- | --- | --- | --- |")
    for row in job.store.applications(outcome.run_id):
        emit(f"| {row['seq']} | {row['line_no']} | {row['application_id']} "
             f"| {row['verdict_label']} | `{load_root_for(row)}` |")
    job.close()
    emit()

    # -- 4. one full explanation ---------------------------------------
    emit("## 4. 单份申请的完整解释（未短路，覆盖所有分支）")
    emit()
    app_id = "APP-1001"
    payload = find(app_id)
    emit(f"申请 `{app_id}`：**{payload['verdict_label']}**"
         f"（run {payload['run_id']}，输入第 {payload['line_no']} 行）")
    emit()
    emit("```")
    report.extend(render_tree(_explained(payload)["tree"]))
    emit("```")
    emit()
    unknown = payload["explanation"]["unknown_values"]
    emit(f"未知值 {len(unknown)} 处（各自记录原因）：")
    emit()
    emit("| 条件 | JSON Pointer | 原因 |")
    emit("| --- | --- | --- |")
    for item in unknown:
        emit(f"| `{item['condition_id']}` | `{item['json_pointer']}` "
             f"| {item['reason']}（{item['detail']}） |")
    emit()
    emit("这两处未知值并未把总体结论改成待补证：成员计数区间为 `[2, 3]`，"
         "下界已达门槛，名单中未列或未知的成员只可能让满足人数更多。"
         "换言之，只有会改变结论的材料才需要补证。")
    emit()
    counter = find("APP-1007")["explanation"]
    delivered = {f["condition_id"] for f in counter["false_conditions"]}
    unknown_ids = {u["condition_id"] for u in counter["unknown_values"]}
    emit(f"明确反例优先：`APP-1007` 已有诚信反例（`INTEGRITY_OK` 为假），"
         f"同时法人登记日期缺失（`LEGAL_DATE` 仍为未知 {sorted(unknown_ids)}），"
         f"总体仍是**{find('APP-1007')['verdict_label']}**——不会因其他材料"
         "缺失而改判待补证。")
    emit()

    # -- 5. team count scenarios ----------------------------------------
    emit("## 5. “至少两名博士且各有三年经验”：名单与字段怎样改变结论")
    emit()
    scenarios = [
        ("APP-1001", "完整名单；已知满足 2 人、未知 1 人（区间 [2,3]）"),
        ("APP-1003", "名单未列全（complete=false），但已知满足 2 人（下界已达门槛）"),
        ("APP-1004", "完整名单；已知 1 人、未知 1 人（区间 [1,2]，卡住门槛）"),
        ("APP-1005", "两人各满足一半：一人有学历无年限，一人有年限无学历"),
        ("APP-1012", "名单未列全且有未知成员（上界不封顶 [1,∞)）"),
    ]
    emit("| 申请 | 情形 | 人数区间 | 计数结论 | 总条件中的其他分支 | 总体结论 |")
    emit("| --- | --- | --- | --- | --- | --- |")
    for application_id, description in scenarios:
        explanation = _explained(find(application_id))
        root_tree = explanation["tree"]
        team_node = None
        for child in root_tree["children"]:
            resolved = child.get("resolved", {}).get("tree", {})
            if child.get("target") == "COUNT_DOCTORS" and resolved.get("node") == "count":
                team_node = resolved
        assert team_node is not None
        other = [f"{c.get('target') or c.get('node')}={c['result']}"
                 for c in root_tree["children"]]
        emit(f"| {application_id} | {description} "
             f"| {team_node['possible_interval']['notation']} "
             f"（真/假/未知 = {team_node['member_counts']['true']}/"
             f"{team_node['member_counts']['false']}/"
             f"{team_node['member_counts']['unknown']}） "
             f"| {team_node['result']} | {', '.join(other)} "
             f"| {find(application_id)['verdict_label']} |")
    emit()
    for application_id in ("APP-1001", "APP-1004", "APP-1005"):
        explanation = _explained(find(application_id))
        for child in explanation["tree"]["children"]:
            resolved = child.get("resolved", {}).get("tree", {})
            if child.get("target") == "COUNT_DOCTORS":
                emit(f"### {application_id} 团队成员计数")
                emit()
                emit(f"* 结论：`{resolved['result']}`；人数区间 "
                     f"{resolved['possible_interval']['notation']}；"
                     f"{resolved['reason']}")
                emit()
                report.extend(_member_rows(resolved))
                emit()
    emit("“两人各满足一半”即学历与年限分别落在不同人身上，谓词在同一成员上"
         "取 `all`，因此没有任何一名成员完整满足；这是成员谓词的作用域保证，"
         "不能拿甲的学历拼乙的年限。")
    emit()

    # -- 6. damaged file -------------------------------------------------
    emit("## 6. 后半段结构损坏：逐行号报告、运行标记未完成")
    emit()
    valid = [json.dumps(record, ensure_ascii=False)
             for record in sample_applications()[:3]]
    bad_cases = [
        ("bad_json", "第 4 行 JSON 语法损坏"),
        ("duplicate_application_id", "第 5 行重复 application_id"),
        ("invalid_item", "第 6 行成员结构损坏"),
    ]
    for code, description in bad_cases:
        lines = list(valid)
        if code == "bad_json":
            lines.append('{"application_id": "APP-9001", "subject": ')
        elif code == "duplicate_application_id":
            lines.append(json.dumps(sample_applications()[1],
                                    ensure_ascii=False))
        else:
            lines.append(json.dumps({
                "application_id": "APP-9002",
                "subject": {"entity_type": "research_institute",
                            "registered_years": "3",
                            "legal_registered_on": "2021-01-01",
                            "has_integrity_issue": False},
                "items": ["not-an-object"], "complete": True},
                ensure_ascii=False))
        path = WORKDIR / f"damaged-{code}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        job_bad = EligibilityJob(WORKDIR, CLAUSE)
        bad = job_bad.run(path)
        error = bad.error
        emit(f"* `{path.name}`（{description}）：退出状态 `{bad.status}`，"
             f"错误 `{error['code']}` 第 {error['line_no']} 行 —— "
             f"{error['detail']}")
        emit(f"  - 已写结果：{bad.applications_written} 条（前缀 `"
             f"{[row['application_id'] for row in job_bad.store.applications(bad.run_id)]}`），"
             f"保留但明确未完成；成功汇总："
             f"`{job_bad.store.run(bad.run_id)['summary_json']}`")
        job_bad.close()
    emit()
    emit("字段缺失、`null` 或类型不符是个别申请的未知值（逐字段记录原因，"
         "不中断文件）；上面的坏 JSON、重复 ID、成员结构损坏属于文件结构错误，"
         "两者严格区分。")
    emit()

    # -- 7. large generated file ----------------------------------------
    emit("## 7. 生成大文件的逐行处理")
    emit()
    large_path = WORKDIR / "applications-large.jsonl"
    total = _write_large_file(large_path, 2000)
    before = time.time()
    job_large = EligibilityJob(WORKDIR, CLAUSE)
    large = job_large.run(large_path)
    elapsed = time.time() - before
    try:
        peak = _peak_rss_kb()
    except Exception:  # pragma: no cover - platform dependent
        peak = None
    counts = large.summary["counts"]
    emit(f"* 文件 `{large_path.name}`：{large.summary['input']['bytes']} 字节，"
         f"{large.summary['input']['lines']} 行（{total} 份申请）")
    emit(f"* 处理耗时 {elapsed:.2f} s；逐行读取，内存中同时只有当前一份申请与条款；"
         f"运行状态 `{large.status}`")
    emit(f"* 汇总：满足 {counts['satisfied']}，不满足 {counts['not_satisfied']}，"
         f"待补证 {counts['needs_evidence']}")
    emit(f"* 顺序保持：首条 `{large.summary['ordered_application_ids'][0]}`，"
         f"末条 `{large.summary['ordered_application_ids'][-1]}`")
    if peak:
        emit(f"* 进程峰值常驻内存约 {peak // 1024} MB（含解释 JSON 的序列化缓冲，"
             "不随文件行数线性增长）")
    job_large.close()
    emit()

    # -- 8. data statement ----------------------------------------------
    emit("## 8. 数据约定与人工核实")
    emit()
    emit("* 条款文件逐条引用的原句均已与归档正文逐字核对（`body[start:end] == quote`）；"
         "条款改版必须显式提供另一份文件，单次运行始终使用开始时验证的内容。")
    emit("* 归档库以只读方式打开，只用于核对版本与引文；不依赖资助表或日程结果。")
    emit("* 十进制资料与常量均以字符串承载，比较使用 Decimal，不经过浮点。")
    emit("* 本作业为辅助预审：结论、引文与解释均可追溯，最终由工作人员核实决定，"
         "不代替人工审批。")
    emit()

    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\n报告已写入 {REPORT}")
    return 0


def _write_large_file(path: Path, total: int) -> int:
    """Write a deterministic synthetic JSONL file of ``total`` applications."""
    from .eligdemo import member, pi, subject
    base = sample_applications()
    with open(path, "w", encoding="utf-8") as handle:
        for index in range(total):
            record = dict(base[index % len(base)])
            record = json.loads(json.dumps(record))
            record["application_id"] = f"GEN-{index + 1:06d}"
            if index % 7 == 0:
                record["subject"]["registered_years"] = "3.00"
            if index % 11 == 0:
                record["items"] = [pi(), member(years_experience="3.0"),
                                   member()]
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return total


def _peak_rss_kb() -> int | None:
    import resource
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


if __name__ == "__main__":
    raise SystemExit(main())

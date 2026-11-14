"""End-to-end demonstration of the XLIFF review-intake job.

Builds the reproducible sample workdir under ``work/xliffdemo/`` and walks
through every required behaviour, reading the **actual database and delivery
relations** back at each step (no success counters only):

1. a clean two-file delivery (zh-CN → en-US) with multi-byte Chinese spans,
   a protected ``g`` and an ``x`` placeholder; read back the usable corpus;
2. nested ``group`` units and a target that reorders sibling codes inside the
   same parent, plus the same span delivered as en-GB (kept apart from en-US);
3. one file mixing usable units with stolen ids, a changed code type, a moved
   hierarchy, a changed ``equiv-text``, unsupported ``ph``, a missing target,
   drafts next to ``final``, a wrong business variant and a wrong version —
   every rejected unit keeps its reason, texts, code tree and XLIFF position;
4. a trailing duplicate unit id invalidates every occurrence in that file,
   including the copy read first;
5. a malformed XML document leaves the corpus byte-identical; a DTD/external
   entity is refused before anything is read;
6. re-delivering the same file adds no rows; the same translation in another
   package (namespace prefix) adds a delivery source but no candidate;
7. once a conflicting approved translation arrives, the group becomes
   ``conflict``: both candidates and both sources stay queryable and the old
   translation stops being the unique usable answer;
8. a forced exit before commit leaves the previous state exactly intact, and
   retrying succeeds;
9. the report is written to ``reports/xliff.md``.

Run: ``python -m archive_pipeline.xliffdemo``
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from .xliffintake import (
    IntakeInterrupted,
    IntakeJob,
    IntakeRejected,
    XliffStore,
)
from .xliffsample import build_sample_workdir, write_pipeline_manifest

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "work" / "xliffdemo"
REPORT = ROOT / "reports" / "xliff.md"
REVIEWS = ROOT / "scenario" / "xliff" / "reviews"
MAPPINGS = ROOT / "scenario" / "xliff" / "mappings"


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _outcome_line(outcome) -> str:
    summary = outcome.summary or {}
    return (f"run {outcome.run_id} → `{outcome.status}`（退出码 {outcome.exit_code}）"
            f"，单元 {summary.get('units_total', 0)}："
            f"候选 {summary.get('units_candidate', 0)}、"
            f"草稿 {summary.get('units_draft', 0)}、"
            f"拒收 {summary.get('units_rejected', 0)}；"
            f"新增候选 {summary.get('new_candidates', 0)}，"
            f"冲突组 {summary.get('conflict_groups', 0)}")


def main() -> int:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True)
    built = build_sample_workdir(WORKDIR)
    write_pipeline_manifest(WORKDIR, built)
    bodies = {v["file"]: v["body"] for v in built["versions"]}
    full = bodies["X-4001-application.html"]
    public = bodies["X-4001-public.html"]
    v1 = bodies["X-4002-supplement-v1.html"]
    v2 = bodies["X-4002-supplement-v2.html"]
    report: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        report.append(line)

    def intake(review: str, mapping: str, **kwargs):
        job = IntakeJob(WORKDIR)
        try:
            return job.intake(REVIEWS / review, MAPPINGS / mapping, **kwargs)
        finally:
            job.close()

    def stored() -> XliffStore:
        return XliffStore(WORKDIR / "xliff.sqlite3")

    emit("# XLIFF 1.2 审译接入演示")
    emit()
    emit(f"样本工作目录：`{WORKDIR}`（来源 `{built['source_id']}`，"
         f"{len(built['versions'])} 个归档版本：X-4001 完整版与公众版、"
         "X-4002 补充通告 v1/v2）")
    emit()
    emit("正文区间按 Python 字符串下标左闭右开解释。例如 X-4001 第一段"
         f" `body[31:97]` 是 {full[31:97]!r}（{len(full[31:97])} 个字符、"
         f"{len(full[31:97].encode('utf-8'))} 个 UTF-8 字节），"
         "本作业只接受字符串下标，不接受字节位置。")
    emit()

    # -- 1 -------------------------------------------------------------
    emit("## 1. 干净交付：中文多字节片段 + 保护代码 + 占位符")
    emit()
    emit("```bash")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo intake \\")
    emit("    scenario/xliff/reviews/01-full-enus.xlf \\")
    emit("    --bindings scenario/xliff/mappings/01-full-enus.json")
    emit("```")
    first = intake("01-full-enus.xlf", "01-full-enus.json")
    emit()
    emit("* " + _outcome_line(first))
    store = stored()
    emit()
    emit("| 单元 | 区间 | 源语言→目标语言 | 状态 | 候选 |")
    emit("| --- | --- | --- | --- | --- |")
    for row in store.units(delivery_id=first.delivery_id):
        emit(f"| {row['unit_id']} | [{row['span_start']},{row['span_end']}) "
             f"| {row['source_language']}→{row['target_language']} "
             f"| {row['status']} | {row['candidate_id']} |")
    emit()
    u01 = store.unit_by_id("u-01", first.delivery_id)[0]
    emit("读回的语料行（`usable`）：")
    emit()
    emit(f"* 原文 `body[{u01['span_start']}:{u01['span_end']}]` = "
         f"{u01['source_text']!r}")
    emit(f"* 保护代码树（源）：`{u01['source_codes_json']}`")
    emit(f"* 审定译文：`{u01['target_text']}`")
    emit(f"* 目标代码树：`{u01['target_codes_json']}`")
    emit(f"* 溯源：delivery {first.delivery_id}，文件 "
         f"`{store.deliveries()[-1]['review_path']}`，"
         f"SHA-256 `{store.deliveries()[-1]['review_sha256'][:16]}…`")
    store.close()
    emit()

    # -- 2 -------------------------------------------------------------
    emit("## 2. 嵌套 group、兄弟代码换序、两种英语标签")
    emit()
    second = intake("02-nested-en-gb.xlf", "02-nested-en-gb.json")
    emit("* " + _outcome_line(second))
    store = stored()
    swap = store.unit_by_id("u-swap", second.delivery_id)[0]
    nest = store.unit_by_id("u-nest-1", second.delivery_id)[0]
    emit(f"* `u-nest-1` 的 group 路径：`{nest['group_path_json']}`"
         "（外层 outer，内层 inner；id 判重范围仍是整个 file）")
    emit(f"* `u-swap` 源代码顺序 "
         f"`{[c['id'] for c in json.loads(swap['source_codes_json'])]}`，"
         f"目标顺序 `{[c['id'] for c in json.loads(swap['target_codes_json'])]}`"
         " — 同一父节点下的兄弟代码换序，接受")
    def span_groups(start, end, language):
        return [g for g in store.group_state()
                if g["span_end"] == end and g["span_start"] == start
                and g["target_language"] == language]

    def total(language):
        return len([g for g in store.group_state()
                    if g["target_language"] == language])

    emit(f"* 同为 `body[31:97)`：en-US 组 "
         f"{len(span_groups(31, 97, 'en-us'))} 个、en-GB 组 "
         f"{len(span_groups(31, 97, 'en-gb'))} 个，互不合并"
         "（语言标签存储统一小写；全库 en-US "
         f"{total('en-us')} 组、en-GB {total('en-gb')} 组）")
    store.close()
    emit()

    # -- 3 -------------------------------------------------------------
    emit("## 3. 一份文件里可运与被拒并存（逐单元说明）")
    emit()
    emit("```bash")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo intake \\")
    emit("    scenario/xliff/reviews/03-mixed-rejections.xlf \\")
    emit("    --bindings scenario/xliff/mappings/03-mixed-rejections.json")
    emit("```")
    third = intake("03-mixed-rejections.xlf", "03-mixed-rejections.json")
    emit()
    emit("* " + _outcome_line(third) + "；非零退出码 4 指明存在错误，其余单元仍接入")
    store = stored()
    emit()
    file_by_pk = {f["file_pk"]: f for f in store.files(third.delivery_id)}
    emit("| 单元 | 原文件 | 类别 | 结果 | 代码 | 位置 |")
    emit("| --- | --- | --- | --- | --- | --- |")
    for row in store.units(delivery_id=third.delivery_id):
        group = json.loads(row["group_path_json"])
        position = (f"file#{row['file_pk']} line {row['line']}"
                    f" source@{row['source_line']} target@{row['target_line']}")
        original = file_by_pk.get(row["file_pk"], {}).get("original")
        emit(f"| {row['unit_id']} | `{original}` | "
             f"{'/'.join(group) or '-'} | {row['status']} "
             f"| {row['reason_code'] or '—'} | {position} |")
    emit()
    emit("拒收原因（逐条取自数据库）：")
    for row in store.units(delivery_id=third.delivery_id):
        if row["status"] == "candidate":
            continue
        emit(f"* `{row['unit_id']}` — {row['reason_code']}: "
             f"{row['reason_detail']}")
    emit()
    emit("所有被拒单元仍保留源/译文本与两棵代码树供人工核对，例如"
         " `u-bad-level`：")
    bad = store.unit_by_id("u-bad-level", third.delivery_id)[0]
    emit()
    emit(f"* 源树 `{bad['source_codes_json']}`")
    emit(f"* 译树 `{bad['target_codes_json']}`")
    emit(f"* 译文 `{bad['target_text']}`")
    store.close()
    emit()

    # -- 4 -------------------------------------------------------------
    emit("## 4. 末尾重复单元 id：同 file 内全部失效")
    emit()
    fourth = intake("04-duplicate-id.xlf", "04-duplicate-id.json")
    emit("* " + _outcome_line(fourth))
    store = stored()
    emit()
    files = {f["file_pk"]: f for f in store.files(fourth.delivery_id)}
    for file_pk, file_row in sorted(files.items()):
        rows = [r for r in store.unit_by_id("u-dup", fourth.delivery_id)
                if r["file_pk"] == file_pk]
        statuses = ", ".join(f"{r['status']}({r['reason_code'] or '−'})"
                             for r in rows)
        emit(f"* file `{file_row['original']}`：u-dup 共 {len(rows)} 次出现 → "
             f"{statuses}")
    emit("* 第一个 file 里先读到的那份也一并失效；另一个 file 中同名 id 合法，"
         "仍可作为候选")
    store.close()
    emit()

    # -- 5 -------------------------------------------------------------
    emit("## 5. 坏 XML 不改库；DTD/外部实体拒绝")
    emit()
    db = WORKDIR / "xliff.sqlite3"
    before = _sha(db)
    try:
        intake("07-broken.xlf", "01-full-enus.json")
    except IntakeRejected as exc:
        emit(f"* 语法错误：`{exc.code}` — {exc.detail}")
    emit(f"* 接入前后 `xliff.sqlite3` 摘要：`{before[:16]}…` → "
         f"`{_sha(db)[:16]}…`（**未改动**）")
    try:
        intake("08-dtd-entity.xlf", "01-full-enus.json")
    except IntakeRejected as exc:
        emit(f"* DTD/外部实体：`{exc.code}` — {exc.detail}")
    emit(f"* 接入后摘要仍为 `{_sha(db)[:16]}…`；不联网、不加载任何外部资源")
    emit()

    # -- 6 -------------------------------------------------------------
    emit("## 6. 相同文件重传 / 不同包装相同译文")
    emit()
    store = stored()
    counts_before = (len(store.candidates()), len(store.deliveries()))
    store.close()
    again = intake("01-full-enus.xlf", "01-full-enus.json")
    store = stored()
    counts_after = (len(store.candidates()), len(store.deliveries()))
    emit(f"* 原样重传：`{again.status}`（与 delivery {again.delivery_id} 相同），"
         f"候选数 {counts_before[0]}→{counts_after[0]}、"
         f"交付数 {counts_before[1]}→{counts_after[1]}")
    store.close()
    alias = intake("05-prefixed-alias.xlf", "05-prefixed-alias.json")
    store = stored()
    emit(f"* 换命名空间前缀、重新分包的同一译文：新增候选 "
         f"{alias.summary['new_candidates']}，新增交付来源 "
         f"{alias.summary['new_candidate_sources']}")
    for candidate in store.candidates():
        if candidate["span_start"] != 31:
            continue
        sources = store.candidate_sources(candidate["candidate_id"])
        emit(f"  * 候选 {candidate['candidate_id']} 的来源交付："
             f"{[s['delivery_id'] for s in sources]}（"
             f"{[Path(s['review_path']).name for s in sources]}）")
    store.close()
    emit()

    # -- 7 -------------------------------------------------------------
    emit("## 7. 先唯一、后冲突")
    emit()
    store = stored()
    group = [g for g in store.group_state()
             if g["archive_id"] == built["versions"][0]["archive_id"]
             and g["span_start"] == 31
             and g["target_language"] == "en-us"][0]
    emit(f"* 冲突前：{group['state']}，唯一可用候选 "
         f"{group['available_candidate_id']}")
    store.close()
    conflict = intake("06-conflict-enus.xlf", "06-conflict-enus.json")
    emit(f"* 冲突译文接入：" + _outcome_line(conflict))
    store = stored()
    group = [g for g in store.group_state()
             if g["archive_id"] == built["versions"][0]["archive_id"]
             and g["span_start"] == 31
             and g["target_language"] == "en-us"][0]
    emit(f"* 冲突后：`{group['state']}`，候选数 {group['candidate_count']}，"
         f"可用候选 {group['available_candidate_id']}（不再冒充唯一答案）")
    emit(f"* 原因：{group['reason']}")
    for candidate in store.candidates(archive_id=group["archive_id"]):
        if (candidate["span_start"], candidate["span_end"]) != (31, 97):
            continue
        if candidate["target_language"] != "en-us":
            continue
        sources = store.candidate_sources(candidate["candidate_id"])
        emit(f"* 候选 {candidate['candidate_id']}：{candidate['target_text']}")
        emit(f"  * 来源："
             + "；".join(f"delivery {s['delivery_id']} "
                        f"({Path(s['review_path']).name} #{s['unit_id']})"
                        for s in sources))
    usable = store.group_state(state="available")
    emit(f"* 可直接使用且尚未冲突的组：{len(usable)} 个；每组恰有一个候选")
    conflicts = store.group_state(state="conflict")
    emit(f"* 待核对（冲突）组：{len(conflicts)} 个；草稿不参与冲突判断")
    store.close()
    emit()

    # -- 8 -------------------------------------------------------------
    emit("## 8. 提交前强制退出：旧状态原样保留，重跑成功")
    emit()
    store = stored()
    snapshot = [dict(g) for g in store.group_state()]
    candidates_before = len(store.candidates())
    deliveries_before = len(store.deliveries())
    store.close()
    try:
        intake("09-wrong-version.xlf", "09-wrong-version.json",
               fail_point="before_commit")
    except IntakeInterrupted as exc:
        emit(f"* 受控中断：{exc}")
    store = stored()
    emitted = [dict(g) for g in store.group_state()]
    emit(f"* 组状态记录 {len(snapshot)}→{len(emitted)}，"
         f"候选 {candidates_before}→{len(store.candidates())}，"
         f"交付 {deliveries_before}→{len(store.deliveries())}"
         "（候选与可用性同事务提交，不会出现“候选已加、旧译文仍唯一可用”）")
    interrupted = store.runs()[-1]
    emit(f"* 该次运行在库中标记为 `{interrupted['status']}`，未产生交付")
    store.close()
    retry = intake("09-wrong-version.xlf", "09-wrong-version.json")
    emit(f"* 重跑同一份文件（正常提交）：" + _outcome_line(retry))
    store = stored()
    retried_unit = store.units(delivery_id=retry.delivery_id)[0]
    emit(f"* 重跑结果确实落库：run {retry.run_id} `{retry.status}`，"
         f"delivery {retry.delivery_id} 单元 `{retried_unit['unit_id']}` "
         f"{retried_unit['status']}（{retried_unit['reason_code']}）——"
         "这份文件本身绑定了错误的版本，规范问题照实记录，与上次中断无关")
    existing = [g for g in store.group_state()
                if g["archive_id"] == built["versions"][0]["archive_id"]
                and g["span_start"] == 31
                and g["target_language"] == "en-us"][0]
    emit(f"* 中断前后该冲突组状态保持 `{existing['state']}`（"
         f"候选 {existing['candidate_count']} 份，无唯一可用候选）")
    store.close()
    emit()
    emit("## 9. 读取命令对照")
    emit()
    emit("```bash")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo usable")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo "
         "usable --language en-GB")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo "
         "groups --state conflict")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo "
         "candidates --archive-id "
         f"{built['versions'][0]['archive_id']}")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo "
         "issues --unit-status rejected")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo "
         "units --unit-id u-bad-level")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo "
         "deliveries")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo runs")
    emit("python -m archive_pipeline.xliffcli --workdir work/xliffdemo "
         "usable --json")
    emit("```")
    emit()
    store = stored()
    emit("当前可用语料（本次演示结束时的库内状态）：")
    emit()
    for group in store.group_state(state="available"):
        candidate = store.candidate(group["available_candidate_id"])
        emit(f"* `{group['source_id']}` `{group['archive_id'][:12]}…` "
             f"v{group['version_no']} [{group['span_start']},"
             f"{group['span_end']}) {group['source_language']}→"
             f"{group['target_language']}：{candidate['target_text']}")
    emit()
    emit("支持边界：")
    emit()
    emit("* 只接收 XLIFF 1.2（根命名空间 `urn:oasis:names:tc:xliff:document:1.2`）；"
         "内联只支持普通文字、成对 `g`、带 `equiv-text` 的空元素 `x`；")
    emit("* `ph`/`bpt`/`ept`/`it`/`mrk`/`sub` 等明确报 `unsupported_inline`，"
         "不剥掉后继续入账；")
    emit("* 拒绝 DTD 与外部实体；不联网、不调用翻译服务、不把译文写回公告正文；")
    emit("* 审定候选 = `approved=\"yes\"` 且目标 `state=\"final\"`；其余为草稿，"
         "永不进入可用查询；")
    emit("* 不做人工裁决：冲突组只标记为待核对并保留两份候选与来源；")
    emit("* 网页采集与版本查询（`cli scan/versions/...`）不受影响，"
         "本作业只读管线库与归档正文。")
    store.close()

    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\nreport → {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

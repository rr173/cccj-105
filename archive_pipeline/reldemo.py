"""End-to-end demonstration of the offline relation job.

Builds a reproducible workdir under ``work/reldemo/`` and walks through the
whole exercise, reading the **actual result database** back at every step:

1. check the rule document and print the readable stratification plan (edges
   are ``head -> body``, recursion components are named);
2. archive five notice versions and run the organization exercise: ancestor
   organizations, cooperation reachability, exact notice-version mentions and
   the exclusion-filtered association set;
3. compare every derived relation against tuples written by hand in this file
   (never produced by the production evaluator);
4. shuffle the order of facts and rules and compare the result sets;
5. cross-check recursion against the independent brute-force oracle
   (:mod:`archive_pipeline.reloracle`) on several finite random inputs;
6. reject the documented bad rule documents and bad fact files, each with its
   exact location — including a negative cycle with empty input, an unsafe
   variable, mixed types and a bad fact *after* good ones;
7. show that a budget overflow and a real mid-run process death never publish
   a new result and never damage the previous successful run;
8. run a longer directed chain (plus a few back edges) and show the on-disk
   intermediate tables, per-round deltas and real memory use.

Run: ``python -m archive_pipeline.reldemo``
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
from pathlib import Path

from .relationjob import RelationJob, check_rules, format_plan, read_relation
from .reloracle import Oracle
from .relsample import build_sample_workdir

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "work" / "reldemo"
REPORT = ROOT / "reports" / "relations.md"
FACTS = ROOT / "scenario" / "relations" / "facts"
RULES = ROOT / "scenario" / "relations" / "rules" / "org.json"

SOURCE_ID = "relsim"
FULL_AID = "arc_25a6d2f01627d245df9ba57bf2e5831e"
PUBLIC_AID = "arc_6af784ed31085359fffe393e61bd4579"
EVAL_AID = "arc_ed728d5bb344f8aa476bef08729add98"
MAINT_AID = "arc_309df132b62995e8a86c08f47f2c3228"


# ---------------------------------------------------------------------------
# Hand-written expected tuples (independent of the production evaluator)
# ---------------------------------------------------------------------------------


EXPECTED_ANCESTOR = {
    ("A", "R"), ("B", "R"), ("A1", "A"), ("A1", "R"), ("A2", "A"),
    ("A2", "R"), ("B1", "B"), ("B1", "R"), ("C", "B"), ("C", "R"),
    ("X", "R"), ("D", "X"), ("D", "R"),
    ("P", "Q"), ("P", "P"), ("Q", "P"), ("Q", "Q"),
    # S declares itself as its own parent: one self tuple, no growth.
    ("S", "S"),
}

# Repeated-variable self-join: ?x appears in both parent_org columns.
EXPECTED_ORG_SELF = {("S",)}

# Cooperation is treated as undirected and its closure is over the component
# {A1, A2, B1, C, P}: the seeds are A1-B1, B1-C, C-A2, P-A1 (all in both
# directions), so every ordered pair inside that component is reachable,
# including (A1, A1) via A1-B1-A1 ... actually via B1's back edge to A1.
# 5 nodes -> 25 ordered pairs.
EXPECTED_COOP_REACH = {(a, b) for a in ("A1", "A2", "B1", "C", "P")
                       for b in ("A1", "A2", "B1", "C", "P")}

# X is excluded and D is a descendant of X, so both leave the allowed set.
# The visible set additionally removes rows whose org has an excluded
# ancestor (same result here, but produced in a later stratum).

# Hand-written from the JSONL inputs and the archived versions: N-5001 v1
# (same identity as v2 but distinct version number), N-5001 public edition
# (same printed number, different archive identity), N-5002 evaluation notice.
EXPECTED_NOTICE_ORG = {
    (SOURCE_ID, FULL_AID, 1, "A1"),
    (SOURCE_ID, FULL_AID, 1, "C"),
    (SOURCE_ID, FULL_AID, 2, "B1"),
    (SOURCE_ID, PUBLIC_AID, 1, "A2"),
    (SOURCE_ID, EVAL_AID, 1, "A"),
    (SOURCE_ID, EVAL_AID, 1, "X"),
    (SOURCE_ID, EVAL_AID, 1, "D"),
}

# X itself and D (a descendant of the excluded X) drop out; A1, C, B1, A2 and A
# remain.  Version 1 of FULL and version 2 of FULL stay distinct tuples.
EXPECTED_NOTICE_ALLOWED = {
    (SOURCE_ID, FULL_AID, 1, "A1"),
    (SOURCE_ID, FULL_AID, 1, "C"),
    (SOURCE_ID, FULL_AID, 2, "B1"),
    (SOURCE_ID, PUBLIC_AID, 1, "A2"),
    (SOURCE_ID, EVAL_AID, 1, "A"),
    (SOURCE_ID, EVAL_AID, 1, "D"),
}

# D is not excluded itself, but its ancestor X is: the later stratum removes it.
EXPECTED_NOTICE_VISIBLE = EXPECTED_NOTICE_ALLOWED - {
    (SOURCE_ID, EVAL_AID, 1, "D")}

EXPECTED_REACH_EVEN = {("A1",)}
EXPECTED_REACH_ODD = {("B1",)}
EXPECTED_ANCESTOR_EXCLUDED = {("D",)}


def _tuples(rows, columns):
    return {tuple(row[column] for column in columns) for row in rows}


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _copy_inputs(work: Path) -> None:
    shutil.copytree(FACTS, work / "facts")
    shutil.copytree(RULES.parent, work / "rules")


def main() -> int:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True)
    built = build_sample_workdir(WORKDIR)
    _copy_inputs(WORKDIR)
    versions = {v["file"]: v for v in built["versions"]}
    report: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        report.append(line)

    emit("# 离线关系规则作业演示（机构关联演练）")
    emit()
    emit(f"样本工作目录：`{WORKDIR}`（来源 `{SOURCE_ID}`，"
         f"{len(built['versions'])} 个归档版本；规则 "
         f"`scenario/relations/rules/org.json`）")
    emit()
    emit("事实与结果都写入 `relation.sqlite3`；归档库 `pipeline.sqlite3` 只读，"
         "`notice` 关系一次运行内来自同一个读取视图。")
    emit()

    # ------------------------------------------------------------------
    emit("## 1. 先检查规则并查看计划（不读取任何事实）")
    emit()
    emit("```bash")
    emit("python -m archive_pipeline.relcli check \\")
    emit("    --rules scenario/relations/rules/org.json --plan")
    emit("```")
    emit()
    check = check_rules(RULES)
    assert check["status"] == "accepted", check
    emit("```")
    emit(format_plan(check["rules"]).rstrip())
    emit("```")
    emit()
    emit("依赖边全部写成“规则头 → 体原子”；否定边同样由头指向被否定的关系，"
         "并标注 `-not->`。同一强连通分量内出现否定边会在阶段 2 被拒绝。")
    emit()

    # ------------------------------------------------------------------
    emit("## 2. 执行转换并读回演练关系")
    emit()
    emit("```bash")
    emit("python -m archive_pipeline.relcli run --rules scenario/relations/rules/org.json")
    emit("python -m archive_pipeline.relcli --workdir work/reldemo show \\")
    emit("    --relation ancestor --page 0 --page-size 50")
    emit("```")
    emit()
    job = RelationJob(WORKDIR, pipeline_db=WORKDIR / "pipeline.sqlite3")
    try:
        outcome = job.run(RULES)
    finally:
        job.close()
    assert outcome.status == "completed", outcome
    exercise_ok = True
    with RelationJob(WORKDIR) as store_job:
        store = store_job.store
        counts = {row["relation"]: row["tuples"]
                  for row in store.relation_stats(outcome.run_id)}
        emit(f"运行 {outcome.run_id} 完成：总元组 {outcome.summary['total_tuples']}，"
             f"其中推导新增 {outcome.summary['derived_tuples']}，"
             f"预算 {outcome.summary['budget']}；"
             f"notice 行 {outcome.summary['notice']['records']}，"
             f"运行前后归档文件摘要一致：{outcome.summary['notice']['consistent']}。")
        emit()
        emit("| 关系 | 类型 | 元组 |")
        emit("| --- | --- | --- |")
        for row in store.relation_stats(outcome.run_id):
            emit(f"| `{row['relation']}` | {row['kind']} | {row['tuples']} |")
        emit()
        emit("notice 关系只从归档库读 `source_id, archive_id, version_no, title, body`，"
             "包含全部历史版本；公告编号只作为正文/标题内容保留，不参与身份：")
        emit()
        for row in store.relation_rows(outcome.run_id, "notice", limit=10):
            emit(f"* `{row['source_id']}` / `{row['archive_id']}` / v{row['version_no']}"
                 f" — {row['title']}")
        emit()
        emit("同一编号 N-5001 有两个归档身份（完整版 `...5831e` 公众版 `...4579`），"
             "完整版的 v1 与 v2 也各自是独立版本行；mentions 精确绑定到版本。")
        emit()

        # -- independent expected tuples --------------------------------
        checks = []
        for relation, expected, columns in [
            ("ancestor", EXPECTED_ANCESTOR, ["org_id", "ancestor"]),
            ("coop_reach", EXPECTED_COOP_REACH, ["org_a", "org_b"]),
            ("notice_org", EXPECTED_NOTICE_ORG,
             ["source_id", "archive_id", "version_no", "org_id"]),
            ("notice_allowed", EXPECTED_NOTICE_ALLOWED,
             ["source_id", "archive_id", "version_no", "org_id"]),
            ("notice_visible", EXPECTED_NOTICE_VISIBLE,
             ["source_id", "archive_id", "version_no", "org_id"]),
            ("reach_even", EXPECTED_REACH_EVEN, ["org_id"]),
            ("reach_odd", EXPECTED_REACH_ODD, ["org_id"]),
            ("ancestor_excluded", EXPECTED_ANCESTOR_EXCLUDED, ["org_id"]),
            ("org_self", EXPECTED_ORG_SELF, ["org_id"]),
        ]:
            actual = _tuples(store.relation_rows(outcome.run_id, relation,
                                                 limit=1000), columns)
            checks.append((relation, actual == expected,
                           actual - expected, expected - actual))
            exercise_ok &= actual == expected
        emit("### 2.1 与手写预期元组逐集合比对")
        emit()
        emit("| 关系 | 与手写预期一致 | 多出 | 缺少 |")
        emit("| --- | --- | --- | --- |")
        for relation, ok, extra, missing in checks:
            emit(f"| `{relation}` | {'是' if ok else '否'} | {sorted(extra)} "
                 f"| {sorted(missing)} |")
        emit()
        emit("覆盖点：正向自递归（祖先、合作）、互递归（`reach_even`/`reach_odd`）、"
             "同一递归关系在正文出现两次（祖先第二条规则，以及合作传递闭包）、"
             "菱形路径去重（A1→B1→C 与 A1→P→A1→… 都只保留集合元组）、"
             "显式重复变量自连接（`org_self`：同一变量占 `parent_org` 两列）、"
             "空关系与常量（`blocked_notice` 为空、`reach_even` 以常量 A1 起步）、"
             "低层递归收敛后才做否定（`ancestor` 与 `excluded` 的层号关系）、"
             "不同公告版本不串用。")
        emit()

        # -- oracle cross-check on the exercise itself -----------------
        emit("### 2.1b 同一演练现场与独立穷举求值器交叉核对")
        emit()
        reference = Oracle(RULES, WORKDIR / "rules",
                           WORKDIR / "pipeline.sqlite3").evaluate()
        oracle_exercise_ok = True
        for relation, columns in [
            ("ancestor", ["org_id", "ancestor"]),
            ("coop_reach", ["org_a", "org_b"]),
            ("reach_even", ["org_id"]),
            ("reach_odd", ["org_id"]),
            ("notice_org", ["source_id", "archive_id", "version_no", "org_id"]),
            ("notice_allowed", ["source_id", "archive_id", "version_no", "org_id"]),
            ("notice_visible", ["source_id", "archive_id", "version_no", "org_id"]),
            ("ancestor_excluded", ["org_id"]),
            ("org_self", ["org_id"]),
        ]:
            prod = _tuples(store.relation_rows(outcome.run_id, relation,
                                               limit=10000), columns)
            expected = set(reference[relation])
            ok = prod == expected
            oracle_exercise_ok &= ok
            emit(f"* `{relation}`：生产 {len(prod)} 元组，穷举 {len(expected)} "
                 f"元组，{'一致' if ok else '不一致'}")
        emit()

        # -- shuffle invariant -----------------------------------------
        emit("### 2.2 打乱事实与规则顺序后结果集合不变")
        emit()
        shuffled = Path(tempfile.mkdtemp(prefix="rel-shuffle-"))
        try:
            (shuffled / "facts").mkdir()
            for path in FACTS.glob("*.jsonl"):
                lines = [line for line in path.read_text(encoding="utf-8")
                         .splitlines() if line.strip()]
                random.Random("relations").shuffle(lines)
                (shuffled / "facts" / path.name).write_text(
                    "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            facts_rules = json.loads(RULES.read_text(encoding="utf-8"))
            random.Random("rules").shuffle(facts_rules["rules"])
            rule_dir = shuffled / "rules"
            rule_dir.mkdir()
            (rule_dir / "org.json").write_text(
                json.dumps(facts_rules, ensure_ascii=False), encoding="utf-8")
            shuffled_db = shuffled / "pipeline.sqlite3"
            shutil.copy(WORKDIR / "pipeline.sqlite3", shuffled_db)
            before = store.relation_stats(outcome.run_id)
            with RelationJob(shuffled) as shuffled_job:
                shuffled_out = shuffled_job.run(rule_dir / "org.json")
                same = shuffled_out.status == "completed"
                compared = []
                for relation, _expected, columns in [
                    ("ancestor", None, ["org_id", "ancestor"]),
                    ("coop_reach", None, ["org_a", "org_b"]),
                    ("notice_org", None,
                     ["source_id", "archive_id", "version_no", "org_id"]),
                    ("notice_allowed", None,
                     ["source_id", "archive_id", "version_no", "org_id"]),
                    ("notice_visible", None,
                     ["source_id", "archive_id", "version_no", "org_id"]),
                    ("org_self", None, ["org_id"]),
                ]:
                    one = _tuples(store.relation_rows(outcome.run_id, relation,
                                                      limit=1000), columns)
                    two = _tuples(shuffled_job.store.relation_rows(
                        shuffled_out.run_id, relation, limit=1000), columns)
                    compared.append((relation, one == two))
            emit("打乱后各行派生关系与原顺序逐集合比较：")
            emit()
            for relation, same_rows in compared:
                emit(f"* `{relation}`：{'一致' if same_rows else '不一致'}")
            emit(f"* 打乱后的运行状态：`{shuffled_out.status}`；"
                 f"notice 快照一致：{shuffled_out.summary['notice']['consistent']}")
        finally:
            shutil.rmtree(shuffled, ignore_errors=True)
        emit()

        # -- oracle cross-check ----------------------------------------
        emit("### 2.3 独立小型穷举求值器交叉核对")
        emit()
        emit("`reloracle.py` 为测试用参考实现：内存列表、每个分量反复全量重跑"
             "直到不再新增（brute-force 不动点），不共享生产代码。")
        emit()
        emit("| 随机资料 | 关系 | 生产元组 | 穷举元组 | 一致 |")
        emit("| --- | --- | --- | --- | --- |")
        oracle_ok = True
        for seed in (1, 2, 3, 4, 5):
            tmp = Path(tempfile.mkdtemp(prefix="rel-oracle-"))
            try:
                sizes = _write_random_case(tmp, seed)
                used = sizes["relations"]
                with RelationJob(tmp) as oracle_job:
                    oracle_out = oracle_job.run(tmp / "rules" / "random.json")
                    reference = Oracle(tmp / "rules" / "random.json",
                                       tmp / "rules", tmp / "pipeline.sqlite3")
                    ref = reference.evaluate()
                    for relation, columns in used:
                        prod = _tuples(oracle_job.store.relation_rows(
                            oracle_out.run_id, relation, limit=5000), columns)
                        expected = set(ref[relation])
                        ok = prod == expected
                        oracle_ok &= ok
                        emit(f"| seed {seed} | `{relation}` | {len(prod)} "
                             f"| {len(expected)} | {'是' if ok else '否'} |")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        emit()
        emit(f"全部一致：{'是' if oracle_ok else '否'}")
        emit()

        # -- rejections ---------------------------------------------
        emit("### 2.4 规则与坏事实的定位拒绝")
        emit()
        emit("| 样本 | 拒绝码 | 位置 | 是否读取事实 |")
        emit("| --- | --- | --- | --- |")
        for label, document, code, location in _bad_rule_cases():
            case_dir = Path(tempfile.mkdtemp(prefix="rel-badrule-"))
            try:
                (case_dir / "rules").mkdir()
                path = case_dir / "rules" / "bad.json"
                path.write_text(json.dumps(document, ensure_ascii=False),
                                encoding="utf-8")
                result = check_rules(path)
                issues = result["issues"][0] if result["issues"] else {}
                ok = issues.get("code") == code and \
                    issues.get("location") == location
                emit(f"| {label} | `{issues.get('code')}` | "
                     f"`{issues.get('location')}` | 否（check 阶段） |")
                oracle_ok &= ok
            finally:
                shutil.rmtree(case_dir, ignore_errors=True)
        # a rule that passes check but the fact file is bad later
        emit()
        emit("坏事实按文件与行号拒绝；前半段好事实、后半段坏事实时**整次回滚**，"
             "不会把部分输入当作结果：")
        emit()
        fact_cases = [
            ("缺列", ['{"u":"a","v":"b"}', '{"u":"a"}'], "missing_column"),
            ("多列", ['{"u":"a","v":"b"}',
                    '{"u":"a","v":"b","extra":1}'], "extra_column"),
            ("null", ['{"u":"a","v":"b"}', '{"u":null,"v":"c"}'], "null_value"),
            ("类型不符（布尔冒充整数）",
             ['{"u":"a","v":"b"}', '{"u":"a","v":true}'], "bad_rule"),
        ]
        emit("| 样本 | 结果 |")
        emit("| --- | --- |")
        for label, lines, code in fact_cases:
            w = Path(tempfile.mkdtemp(prefix="rel-badfact-"))
            try:
                _write_typed_bad_case(w, label, lines)
                with RelationJob(w) as bad_job:
                    bad = bad_job.run(w / "rules" / "typed.json")
                emit(f"| {label} | `{bad.status}` {bad.exit_code} "
                     f"`{bad.error['error']['code']}` 行 "
                     f"{bad.error['error']['line_no']} |")
            finally:
                shutil.rmtree(w, ignore_errors=True)
        # boolean constant in a rule
        emit()
        emit("规则里的布尔常量不能被当作整数：")
        emit()
        bool_dir = Path(tempfile.mkdtemp(prefix="rel-boolconst-"))
        try:
            (bool_dir / "rules").mkdir()
            path = bool_dir / "rules" / "bool.json"
            path.write_text(json.dumps({
                "format": "relation-rules/1", "budget": 10,
                "relations": {
                    "p": {"columns": [["u", "string"]],
                          "source": {"kind": "jsonl", "path": "../p.jsonl"}},
                    "d": {"columns": [["n", "int"]]}},
                "rules": [{"head": {"relation": "d", "args": [{"const": True}]},
                           "body": [{"relation": "p", "args": ["?u"]}]}]}),
                encoding="utf-8")
            result = check_rules(path)
            issue = result["issues"][0]
            emit(f"* `{issue['code']}` at `{issue['location']}` — "
                 f"{issue['detail']}")
        finally:
            shutil.rmtree(bool_dir, ignore_errors=True)
        emit()

        # -- failure isolation -----------------------------------------
        emit("### 2.5 预算超限与真实中途退出")
        emit()
        emit("先用当前成功运行为基准：")
        emit()
        emit(f"* 旧结果运行号 {outcome.run_id}，`ancestor` 元组 "
             f"{store.relation_count(outcome.run_id, 'ancestor')}，"
             f"`notice` 行 {store.relation_count(outcome.run_id, 'notice')}")
        anc_hash = _relation_fingerprint(store, outcome.run_id)
        emit(f"* 关系行指纹 SHA-256（排序后 JSON）：`{anc_hash}`")
        emit()
        emit("超预算：预算 10，事实本身已超，运行明确失败且不发布：")
        emit()
        with RelationJob(WORKDIR) as budget_job:
            over = budget_job.run(RULES, budget=10)
            emit(f"* 状态 `{over.status}`，退出码 {over.exit_code}，"
                 f"错误 `{over.error['kind']}`：{over.error['detail']}")
            emit(f"* 运行列表：{[(r['run_id'], r['status']) for r in budget_job.store.runs()]}")
        emit()
        emit("真实中途退出（`--hard-exit` 在打开的事务里 `os._exit(9)`）：")
        emit()
        proc = subprocess.run(
            [sys.executable, "-m", "archive_pipeline.relcli",
             "--workdir", str(WORKDIR), "run", "--rules", str(RULES),
             "--hard-exit"], cwd=str(ROOT), capture_output=True, text=True)
        emit(f"* 子进程退出码 {proc.returncode}（无输出、无提交）")
        with RelationJob(WORKDIR) as after_job:
            emit(f"* 重开数据库后的运行列表："
                 f"{[(r['run_id'], r['status']) for r in after_job.store.runs()]}")
            emit(f"* 最新成功运行仍是 {after_job.store.latest_successful_run()['run_id']}")
            emit(f"* 旧运行关系指纹仍为 `{_relation_fingerprint(after_job.store, outcome.run_id)}`")
            emit(f"* 读回 `notice_visible` 仍有 "
                 f"{after_job.store.relation_count(outcome.run_id, 'notice_visible')} 行")
        emit()
        emit("随后正常再执行并分页读取每个关系：")
        emit()
        with RelationJob(WORKDIR) as final_job:
            final = final_job.run(RULES)
            emit(f"* 新运行 {final.run_id} `{final.status}`")
            for relation in ("org", "parent_org", "coop", "excluded",
                             "mention", "notice", "ancestor", "coop_reach",
                             "reach_even", "reach_odd", "org_self", "notice_org",
                             "notice_allowed", "ancestor_excluded",
                             "notice_visible", "blocked_notice"):
                page = read_relation(final_job.store, final.run_id, relation,
                                     offset=0, limit=3)
                emit(f"* `{relation}`：共 {page['total']} 行，首 3 行 "
                     f"{_j(page['rows'])}")
        emit()

    # ---------------------------------------------------------------------
    emit("## 3. 较长有向链 + 少量回边：磁盘中间表、每轮增量与内存")
    emit()
    chain_dir = Path(tempfile.mkdtemp(prefix="rel-chain-"))
    try:
        chain = _build_chain_case(chain_dir, nodes=120, extra_edges=12)
        emit(f"构造 {chain['nodes']} 个节点的有向链，另加 {chain['back_edges']} "
             f"条跨节点回边（随机取端点），规则为传递闭包 `reach`：")
        emit()
        emit("```bash")
        emit("python -m archive_pipeline.relcli --workdir <tmp> run \\")
        emit("    --rules <tmp>/rules/chain.json --progress")
        emit("```")
        emit()
        emit("| 轮次 | 本轮新增元组 | 增量连接 | 全量连接 |")
        emit("| --- | --- | --- | --- |")
        with RelationJob(chain_dir) as chain_job:
            tracemalloc.start()
            chain_out = chain_job.run(chain_dir / "rules" / "chain.json")
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            rounds = chain_job.store.rounds(chain_out.run_id, "reach")
            for row in rounds:
                plan = json.loads(row["join_plan_json"])
                delta = sum(1 for p in plan if p["mode"] == "delta")
                full = sum(1 for p in plan if p["mode"] == "full")
                emit(f"| {row['round_no']} | {row['delta_rows']} | {delta} "
                     f"| {full} |")
            total = chain_job.store.relation_count(chain_out.run_id, "reach")
            on_disk = _measure_facts(chain_dir / "relation.sqlite3")
            emit()
            emit(f"* 传递闭包元组总数 {total}（{chain['nodes']} 个节点；"
                 "链本身的纯可达对为 7140，回边把大量节点连成环，"
                 "自反与额外可达对因此明显增多）")
            emit(f"* 结果库中 `facts` 表占用（页面计数 × 页大小）："
                 f"{on_disk['facts_bytes']} 字节；`rule_relations`、"
                 f"`round_stats` 等元数据 {on_disk['meta_bytes']} 字节")
            emit(f"* 本次 Python 进程的 tracemalloc 峰值：{peak} 字节"
                 f"（中间集合不积累为全库容器）")
            emit("* 中间关系全程保存在结果库 `facts` 表（磁盘）；每轮的"
                 "`rel_<name>_delta` 是 SQLite 临时表（`temp_store=FILE`），"
                 "全量侧是按 rowid 水位冻结的临时视图，本轮新元组对其不可见，"
                 "求值结束即删除")
            emit(f"* 实际每轮增量连接见 `round_stats.join_plan_json`："
                 f"{_j(rounds[1]['join_plan_json'])}")
    finally:
        shutil.rmtree(chain_dir, ignore_errors=True)
    emit()

    emit("## 4. 支持范围与统计含义")
    emit()
    emit("* 类型只有 `string` 与有符号 64 位 `int`；布尔不是整数，"
         "字符串按原值比较（不折叠大小写、不改空白）。")
    emit("* `null`、缺列、多列与类型不符逐行拒绝并定位（文件:行:列）。")
    emit("* 事实按集合语义：重复行只算一条（`INSERT OR IGNORE` + 主键）。")
    emit("* 正递归与相互递归按最小不动点求值；否定采用分层否定，"
         "同分量负边直接拒绝。")
    emit("* `delta_rows` 是**该关系该轮新插入的唯一元组数**；"
         "`variants` 是该轮执行的增量变体数；`joins_delta` / `joins_full` "
         "统计体原子分别读增量表/全量快照的次数。")
    emit("* `total_tuples` 是本次存储的元组总数（基础事实 + 全部推导关系）；"
         "`derived_tuples` 只是推导新增部分；预算对 `total_tuples` 生效。")
    emit("* 这些计数只用于审计求值路径，正确性以与手写预期/穷举求值器的"
         "集合比对为准，耗时与“成功条数”不代替核对。")
    emit()
    emit("已知边界：不提供事实删除后的在线维护、聚合、函数生成新值与无限数值"
         "递归；规则与事实均为离线批量输入；作业为单进程。")
    emit()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\n报告已写入 {REPORT}")
    return 0 if (oracle_ok and oracle_exercise_ok and exercise_ok) else 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _relation_fingerprint(store, run_id: int) -> str:
    payload = []
    for relation in sorted({row["relation"] for row in store.relation_stats(run_id)}):
        payload.append([relation, store.relation_rows(run_id, relation, limit=10000)])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()


def _measure_facts(path: Path) -> dict:
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        facts_pages = 0
        meta_pages = 0
        for row in conn.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
            if row[0] == "facts" or row[0].startswith("idx_facts"):
                facts_pages += row[1]
            else:
                meta_pages += row[1]
        return {"facts_bytes": int(facts_pages), "meta_bytes": int(meta_pages)}
    finally:
        conn.close()


def _write_random_case(tmp: Path, seed: int) -> dict:
    """A small finite random dataset (nodes, orgs, edges, exclusions)."""
    rng = random.Random(seed)
    nodes = [f"n{i}" for i in range(rng.randint(3, 6))]
    edges = sorted({(rng.choice(nodes), rng.choice(nodes))
                    for _ in range(rng.randint(2, 8))})
    excluded = sorted({rng.choice(nodes) for _ in range(rng.randint(0, 2))})
    (tmp / "facts").mkdir(parents=True)
    (tmp / "rules").mkdir()
    (tmp / "facts" / "edge.jsonl").write_text(
        "\n".join(json.dumps({"u": u, "v": v}) for u, v in edges) + "\n",
        encoding="utf-8")
    (tmp / "facts" / "node.jsonl").write_text(
        "\n".join(json.dumps({"node": node}) for node in nodes) + "\n",
        encoding="utf-8")
    (tmp / "facts" / "excl.jsonl").write_text(
        "\n".join(json.dumps({"node": node}) for node in excluded) + "\n",
        encoding="utf-8")
    document = {
        "format": "relation-rules/1", "budget": 5000,
        "relations": {
            "edge": {"columns": [["u", "string"], ["v", "string"]],
                     "source": {"kind": "jsonl", "path": "../facts/edge.jsonl"}},
            "node": {"columns": [["node", "string"]],
                     "source": {"kind": "jsonl", "path": "../facts/node.jsonl"}},
            "excl": {"columns": [["node", "string"]],
                     "source": {"kind": "jsonl", "path": "../facts/excl.jsonl"}},
            "reach": {"columns": [["u", "string"], ["v", "string"]]},
            "reach_safe": {"columns": [["u", "string"], ["v", "string"]]},
            "same": {"columns": [["u", "string"], ["v", "string"]]},
        },
        "rules": [
            {"head": {"relation": "reach", "args": ["?u", "?v"]},
             "body": [{"relation": "edge", "args": ["?u", "?v"]}]},
            {"head": {"relation": "reach", "args": ["?u", "?v"]},
             "body": [{"relation": "reach", "args": ["?u", "?w"]},
                      {"relation": "reach", "args": ["?w", "?v"]}]},
            {"head": {"relation": "reach", "args": ["?u", "?u"]},
             "body": [{"relation": "reach", "args": ["?u", "?v"]},
                      {"relation": "reach", "args": ["?v", "?u"]}]},
            {"head": {"relation": "reach_safe", "args": ["?u", "?v"]},
             "body": [{"relation": "reach", "args": ["?u", "?v"]},
                      {"not": {"relation": "excl", "args": ["?u"]}}]},
            {"head": {"relation": "same", "args": ["?u", "?v"]},
             "body": [{"relation": "edge", "args": ["?u", "?v"]},
                      {"eq": ["?u", "?v"]}]},
        ]}
    (tmp / "rules" / "random.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8")
    from .store import Store
    Store(tmp / "pipeline.sqlite3").close()
    return {"relations": [("reach", ["u", "v"]),
                          ("reach_safe", ["u", "v"]),
                          ("same", ["u", "v"])]}


def _build_chain_case(tmp: Path, nodes: int, extra_edges: int) -> dict:
    (tmp / "facts").mkdir(parents=True)
    (tmp / "rules").mkdir()
    edges = [(f"n{i}", f"n{i + 1}") for i in range(nodes - 1)]
    rng = random.Random("chain")
    for _ in range(extra_edges):
        a = rng.randrange(nodes)
        b = rng.randrange(nodes)
        if a != b:
            edges.append((f"n{a}", f"n{b}"))
    (tmp / "facts" / "edge.jsonl").write_text(
        "\n".join(json.dumps({"u": u, "v": v}) for u, v in edges) + "\n",
        encoding="utf-8")
    document = {
        "format": "relation-rules/1", "budget": 100000,
        "relations": {
            "edge": {"columns": [["u", "string"], ["v", "string"]],
                     "source": {"kind": "jsonl", "path": "../facts/edge.jsonl"}},
            "reach": {"columns": [["u", "string"], ["v", "string"]]}},
        "rules": [
            {"head": {"relation": "reach", "args": ["?u", "?v"]},
             "body": [{"relation": "edge", "args": ["?u", "?v"]}]},
            {"head": {"relation": "reach", "args": ["?u", "?v"]},
             "body": [{"relation": "reach", "args": ["?u", "?w"]},
                      {"relation": "reach", "args": ["?w", "?v"]}]},
        ]}
    (tmp / "rules" / "chain.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8")
    from .store import Store
    Store(tmp / "pipeline.sqlite3").close()
    return {"nodes": nodes, "back_edges": extra_edges}


def _bad_rule_cases():
    base_relations = {
        "p": {"columns": [["u", "string"], ["v", "string"]],
              "source": {"kind": "jsonl", "path": "../p.jsonl"}},
        "n": {"columns": [["k", "string"], ["num", "int"]],
              "source": {"kind": "jsonl", "path": "../n.jsonl"}},
    }

    def doc(relations, rules):
        merged = dict(base_relations)
        merged.update(relations)
        return {"format": "relation-rules/1", "budget": 100,
                "relations": merged, "rules": rules}

    empty_file = {"format": "relation-rules/1", "budget": 100,
                  "relations": dict(base_relations), "rules": [
                      {"head": {"relation": "a", "args": ["?x"]},
                       "body": [{"relation": "p", "args": ["?x", {"const": "q"}]}]}]}
    empty_file["relations"]["a"] = {"columns": [["x", "string"]]}
    empty_file["relations"]["b"] = {"columns": [["x", "string"]]}
    empty_file["rules"] = [
        {"head": {"relation": "a", "args": ["?x"]},
         "body": [{"relation": "p", "args": ["?x", {"const": "q"}]}]},
        {"head": {"relation": "a", "args": ["?x"]},
         "body": [{"relation": "b", "args": ["?x"]},
                  {"not": {"relation": "a", "args": ["?x"]}}]},
        {"head": {"relation": "b", "args": ["?x"]},
         "body": [{"relation": "a", "args": ["?x"]}]},
    ]
    yield ("否定闭环（当前输入为空也拒绝）", empty_file,
           "negative_cycle", "rules[1]")
    yield ("无正关系绑定的变量",
           doc({"a": {"columns": [["x", "string"]]}}, [
               {"head": {"relation": "a", "args": ["?x"]},
                "body": [{"relation": "p", "args": ["?x", "?y"]},
                         {"eq": ["?x", "?z"]}]}]),
           "unsafe_variable", "rules[0].body[1]")
    yield ("混合类型：字符串与整数比较",
           doc({"a": {"columns": [["x", "string"]]}}, [
               {"head": {"relation": "a", "args": ["?x"]},
                "body": [{"relation": "p", "args": ["?x", "?y"]},
                         {"eq": ["?x", {"const": 3}]}]}]),
           "comparison_type_mismatch", "rules[0].body[1]")
    yield ("基础关系作为规则头",
           doc({}, [
               {"head": {"relation": "p", "args": ["?u", "?v"]},
                "body": [{"relation": "p", "args": ["?u", "?v"]}]}]),
           "base_relation_as_head", "rules[0]")
    yield ("未声明关系",
           doc({"a": {"columns": [["x", "string"]]}}, [
               {"head": {"relation": "a", "args": ["?x"]},
                "body": [{"relation": "ghost", "args": ["?x"]}]}]),
           "unknown_relation", "rules[0].body[0]")
    yield ("元数不符",
           doc({"a": {"columns": [["x", "string"]]}}, [
               {"head": {"relation": "a", "args": ["?x"]},
                "body": [{"relation": "p", "args": ["?x"]}]}]),
           "arity_mismatch", "rules[0].body[0]")
    yield ("未定义的推导关系",
           doc({"a": {"columns": [["x", "string"]]},
                "b": {"columns": [["x", "string"]]}}, [
               {"head": {"relation": "a", "args": ["?x"]},
                "body": [{"relation": "p", "args": ["?x", "?y"]}]}]),
           "undefined_derived_relation", "$.relations['b']")
    yield ("裸字符串（不能靠长相猜常量）",
           doc({"a": {"columns": [["x", "string"]]}}, [
               {"head": {"relation": "a", "args": ["?x"]},
                "body": [{"relation": "p", "args": ["?x", "nope"]}]}]),
           "bare_string_term", "rules[0].body[0].args[1]")


def _write_typed_bad_case(tmp: Path, label: str, lines: list[str]) -> None:
    (tmp / "facts").mkdir(parents=True)
    (tmp / "rules").mkdir()
    (tmp / "facts" / "p.jsonl").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")
    (tmp / "facts" / "int.jsonl").write_text('{"k":"a","num":1}\n',
                                             encoding="utf-8")
    document = {
        "format": "relation-rules/1", "budget": 100,
        "relations": {
            "p": {"columns": [["u", "string"], ["v", "string"]],
                  "source": {"kind": "jsonl", "path": "../facts/p.jsonl"}},
            "int_r": {"columns": [["k", "string"], ["num", "int"]],
                      "source": {"kind": "jsonl", "path": "../facts/int.jsonl"}},
            "d": {"columns": [["u", "string"], ["v", "string"]]}},
        "rules": [
            {"head": {"relation": "d", "args": ["?u", "?v"]},
             "body": [{"relation": "p", "args": ["?u", "?v"]}]}]}
    (tmp / "rules" / "typed.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8")
    from .store import Store
    Store(tmp / "pipeline.sqlite3").close()


if __name__ == "__main__":
    sys.exit(main())

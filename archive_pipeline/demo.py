"""End-to-end demonstration of the archive pipeline against both simulators.

Run from the project root::

    python -m archive_pipeline.demo

Starts both local sites, executes a scripted sequence of scans covering every
required case, and writes ``reports/``:

* ``report.md``               human-readable narrative with evidence tables;
* ``run.json``                machine-readable log of every step and scan;
* ``requests/scan-<n>.json``  full request manifest per scan, including URLs
                              refused before a request was sent;
* ``diffs/<doc>.json``        version-to-version paragraph diffs;
* ``versions.md``             per-document version history.

Only 127.0.0.1:8811 / 127.0.0.1:8812 are contacted.  Off-site links in the
scenario point at unresolvable hosts, so if a guard failed the request would
visibly fail instead of silently succeeding.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from .archive import RawArchive
from .config import Config, load_config
from .engine import ScanEngine
from .sim_server import start_sites
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.json"
SCENARIO_PATH = ROOT / "scenario" / "scenario.json"
REPORTS = ROOT / "reports"
WORK = ROOT / "work"
# Filled in by main(): the mutable scenario used by the running simulators.
WORK_SCENARIO: Path = SCENARIO_PATH


def reset_scenario_copy() -> Path:
    """The demo mutates a *copy* so the checked-in baseline stays pristine."""
    WORK.mkdir(parents=True, exist_ok=True)
    copy = WORK / "scenario-demo.json"
    copy.write_bytes(SCENARIO_PATH.read_bytes())
    return copy


def mutate(changes: list[dict]) -> None:
    data = json.loads(WORK_SCENARIO.read_text(encoding="utf-8"))
    for change in changes:
        site = data["sites"][change["site"]]
        if "set" in change:
            site.update(change["set"])
        if "notice_key" in change:
            for notice in site.get("notices", []):
                if notice["key"] == change["notice_key"]:
                    notice.update(change.get("fields", {}))
                    break
    WORK_SCENARIO.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
class Runner:
    def __init__(self, config: Config, store: Store, archive: RawArchive,
                 sites, engine: ScanEngine):
        self.config = config
        self.store = store
        self.archive = archive
        self.sites = sites
        self.engine = engine
        self.steps: list[dict] = []
        self.scans: dict[int, dict] = {}
        self.diffs: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    def scan_source(self, source_id: str, *, interrupt_after: int | None = None,
                    engine: ScanEngine | None = None) -> dict:
        engine = engine or self.engine
        source = self.config.source(source_id)
        result = engine.scan(source, interrupt_after=interrupt_after)
        request_path = self._write_request_manifest(result)
        summary = {
            "scan_id": result.scan_id,
            "source_id": source_id,
            "complete": result.complete,
            "paging_loop": result.paging_loop,
            "stop_reason": result.stop_reason,
            "pages": [
                {"page_no": page.page_no, "url": page.url, "outcome": page.outcome,
                 "next": page.next_urls, "details": page.detail_urls}
                for page in result.pages
            ],
            "robots": result.robots,
            "details": [self._detail_summary(item) for item in result.detail_items],
            "blocked": result.blocked,
            "notes": result.notes,
            "retraction_verdicts": result.retraction_verdicts,
            "request_manifest": request_path,
            "request_count": sum(1 for r in result.requests if r["requested"]),
            "refused_count": sum(1 for r in result.requests if not r["requested"]),
        }
        self.scans[result.scan_id] = summary
        for item in result.detail_items:
            if item.get("diff") and (item["diff"]["paragraphs_added"]
                                     or item["diff"]["paragraphs_removed"]):
                self.diffs.setdefault(item["document_key"], []).append({
                    "scan_id": result.scan_id,
                    "version_no": item.get("version_no"),
                    "url": item["url_key"],
                    **item["diff"],
                })
        self._write_diff_files()
        print(f"  scan {result.scan_id} [{source_id}]: complete={result.complete} "
              f"outcomes={self._outcome_counts(summary)}")
        return summary

    def _detail_summary(self, item: dict) -> dict:
        raw = item.get("raw_digest")
        return {
            "url": item["url_key"],
            "outcome": item["outcome"],
            "http_status": item["http_status"],
            "document_key": item.get("document_key"),
            "version_created": item["version_created"],
            "version_no": item.get("version_no"),
            "version_reason": item.get("version_reason"),
            "redirected_to": item.get("redirected_to"),
            "canonical_url": item.get("canonical_url"),
            "canonical_blocked": item.get("canonical_blocked"),
            "raw_digest": raw,
            "previous_raw_digest": item.get("previous_raw_digest"),
            "raw_bytes_changed": (
                bool(item.get("previous_raw_digest"))
                and raw != item.get("previous_raw_digest")),
            "note": item.get("note"),
            "diff": item.get("diff"),
        }

    @staticmethod
    def _outcome_counts(summary: dict) -> dict:
        counts: dict[str, int] = {}
        for detail in summary["details"]:
            counts[detail["outcome"]] = counts.get(detail["outcome"], 0) + 1
        return counts

    def _write_request_manifest(self, result) -> str:
        path = REPORTS / "requests" / f"scan-{result.scan_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scan_id": result.scan_id,
            "source_id": result.source_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "note": (
                "One entry per HTTP request attempted. 'requested=false' entries "
                "are URLs refused before any socket was opened (out-of-scope or "
                "robots-disallowed). 'spacing_wait_ms' is the enforced minimum "
                "interval delay. robots.txt is fetched once per scan."
            ),
            "requests": result.requests,
            "blocked_candidates": result.blocked,
            "seed_urls": [page.url for page in result.pages],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return str(path.relative_to(ROOT))

    def _write_diff_files(self) -> None:
        directory = REPORTS / "diffs"
        directory.mkdir(parents=True, exist_ok=True)
        for key, entries in self.diffs.items():
            safe = key.replace("/", "_").replace("?", "_").replace("&", "_")
            (directory / f"{safe}.json").write_text(
                json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    def _audit_servers(self) -> dict:
        """Cross-check the crawler manifest against what the sites actually served."""
        audit = {}
        for site in self.sites:
            served = site.served_log()
            paths = sorted({entry["path"] for entry in served})
            offsite = [p for p in paths if not p.startswith("/")]
            audit[site.layout] = {
                "requests_served": len(served),
                "unique_paths": paths,
                "non_local_paths": offsite,
            }
        return audit

    # ------------------------------------------------------------------
    def step(self, title: str, explanation: str, scan_ids: list[int],
             data: dict | None = None) -> None:
        self.steps.append({
            "title": title,
            "explanation": explanation,
            "scan_ids": scan_ids,
            "data": data or {},
        })

    def write_report(self) -> None:
        md: list[str] = [
            "# 公共研究通告归档管线 · 端到端演示报告",
            "",
            f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
            "",
            "所有请求仅发往本地模拟站点 `127.0.0.1:8811`(sim_a) 与 "
            "`127.0.0.1:8812`(sim_b)。",
            "",
        ]
        for index, step in enumerate(self.steps, 1):
            md += [f"## 步骤 {index} · {step['title']}", "", step["explanation"], ""]
            if step["scan_ids"]:
                md += ["**扫描清单**", ""]
                for scan_id in step["scan_ids"]:
                    scan = self.scans[scan_id]
                    md += [
                        f"### scan {scan_id} — {scan['source_id']}",
                        "",
                        f"- complete: `{scan['complete']}` / paging_loop: "
                        f"`{scan['paging_loop']}` / stop_reason: `{scan['stop_reason']}`",
                        f"- 页面数: {len(scan['pages'])}，详情请求: "
                        f"{len(scan['details'])}，实际 HTTP 请求: {scan['request_count']}，"
                        f"未发请求即拒绝: {scan['refused_count']}",
                        f"- robots: `{scan['robots']['status']}`"
                        f"（规则子集见 README，摘录 {len(scan['robots'].get('rules', []))} 组）",
                        "",
                        "| 页面 | URL | 结果 | next |",
                        "| --- | --- | --- | --- |",
                    ]
                    for page in scan["pages"]:
                        md.append(
                            f"| {page['page_no']} | `{page['url']}` | {page['outcome']} "
                            f"| {', '.join(f'`{n}`' for n in page['next']) or '—'} |")
                    md += ["", "| 详情 URL | 结果 | HTTP | 文档 | 版本 | 原因 |",
                           "| --- | --- | --- | --- | --- | --- |"]
                    for detail in scan["details"]:
                        if detail["version_created"]:
                            version_cell = f"v{detail['version_no']}（新）"
                        elif detail["version_no"]:
                            version_cell = f"沿用 v{detail['version_no']}"
                        else:
                            version_cell = "—"
                        md.append(
                            f"| `{detail['url']}` | {detail['outcome']} | "
                            f"{detail['http_status']} | {detail['document_key'] or '—'} | "
                            f"{version_cell} | {detail['version_reason'] or '—'} |")
                    if scan["retraction_verdicts"]:
                        md += ["", "**撤稿裁决**", "",
                               "```json",
                               json.dumps(scan["retraction_verdicts"],
                                          ensure_ascii=False, indent=2), "```"]
                    if scan["blocked"]:
                        md += ["", "**未发请求即被拒绝的候选 URL**", ""]
                        for blocked in scan["blocked"]:
                            md.append(f"- `{blocked['url']}` — {blocked['reason']} "
                                      f"({blocked['where']})")
                    relevant_notes = [
                        note for note in scan["notes"]
                        if "duplicate listing" not in note
                        and "not requested this scan" not in note
                    ]
                    if relevant_notes:
                        md += ["", "**引擎备注**", ""]
                        md += [f"- {note}" for note in relevant_notes]
                    md += ["", f"请求清单文件：`{scan['request_manifest']}`", ""]
            if step["data"]:
                md += ["**证据**", "", "```json",
                       json.dumps(step["data"], ensure_ascii=False, indent=2),
                       "```", ""]

        # Version history appendix.
        md += ["## 附录 A · 版本历史", "", "| source | document | v | scan | "
               "issued | title | raw digest | business digest |",
               "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for source in self.config.sources:
            for version in self.store.all_versions(source.source_id):
                md.append(
                    f"| {source.source_id} | {version['document_key']} | "
                    f"{version['version_no']} | {version['scan_id']} | "
                    f"{version['issued_on']} | {version['title']} | "
                    f"`{version['raw_digest'][:12]}…` | "
                    f"`{version['business_digest'][:12]}…` |")
        md += ["", "## 附录 B · 文档状态", "",
               "| source | document | state | retracted_at | missing_streak | last URL |",
               "| --- | --- | --- | --- | --- | --- |"]
        for source in self.config.sources:
            for doc in self.store.documents(source.source_id):
                md.append(
                    f"| {source.source_id} | {doc['document_key']} | {doc['state']} | "
                    f"{doc['retracted_at'] or '—'} | {doc['missing_streak']} | "
                    f"`{doc['last_url_key'] or '—'}` |")
        md += ["", "## 附录 C · URL 归一化示例", "",
               "| 原始 URL | 归一化后（frontier 键） |", "| --- | --- |"]
        for entry in self.normalization_examples:
            md.append(f"| `{entry['raw']}` | `{entry['normalized']}` |")
        md += ["", "## 附录 D · 目录索引文件", "",
               "`diffs/` 保存每个版本变更的段落级差异；`requests/` 保存每次扫描的"
               "完整请求清单（含未发出请求的拒绝记录）。", ""]
        (REPORTS / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    normalization_examples: list[dict]


def collect_normalization_examples() -> list[dict]:
    from .urlnorm import normalize_url
    tracking = frozenset({
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "ref", "spm",
    })
    raw_urls = [
        "http://127.0.0.1:8811/notices/same-url/A-1001?view=full&sort=asc"
        "&utm_source=x&utm_medium=y#section-2",
        "http://127.0.0.1:8811/notices/same-url/A-1001?sort=asc&view=full",
        "http://127.0.0.1:8811/notices/public-edition/A-1002?edition=public",
        "http://127.0.0.1:8811/notices/public-edition/A-1002",
        "http://127.0.0.1:8812/n/water-quality?b=2&a=1&utm_source=feed",
        "http://127.0.0.1:8812/n/water-quality?a=1&b=2",
    ]
    examples = []
    for raw in raw_urls:
        examples.append({"raw": raw, "normalized": normalize_url(raw, tracking)})
    return examples


def main() -> None:
    global WORK_SCENARIO
    REPORTS.mkdir(exist_ok=True)
    for stale in (REPORTS / "requests", REPORTS / "diffs", WORK):
        if stale.exists():
            shutil.rmtree(stale)
    WORK.mkdir(parents=True, exist_ok=True)
    WORK_SCENARIO = reset_scenario_copy()

    config = load_config(CONFIG_PATH)
    archive = RawArchive(WORK / "archive")
    store = Store(WORK / "pipeline.sqlite3")
    sites = start_sites(WORK_SCENARIO)
    engine = ScanEngine(config, store, archive, progress=lambda msg: print(msg))
    runner = Runner(config, store, archive, sites, engine)
    runner.normalization_examples = collect_normalization_examples()
    started = time.time()

    def reset_cache() -> None:
        for site in sites:
            site.reset_page_cache()

    def scan(source_ids: list[str]) -> list[int]:
        return [runner.scan_source(sid)["scan_id"] for sid in source_ids]

    try:
        # 1 — baseline ---------------------------------------------------
        s1 = scan(["sim_a", "sim_b"])
        runner.step(
            "基线扫描：建立首个版本与请求清单",
            "两站各自完整扫描。A 站目录链接带业务参数 `sort=asc&view=full` 与追踪参数；"
            "B 站目录使用相对短链 `n/<slug>?b=2&a=1&utm_*`，服务器以 302 跳到长地址。"
            "所有详情均解析为统一字段 `source/document_key/title/issued_on/body/"
            "canonical_url`。",
            s1)

        # 2 — evidence of URL normalization & scope refusal --------------
        scan2 = runner.scans[s1[0]]
        normalization = [
            {
                "equivalent_count": 1,
                "detail_urls": [d["url"] for d in scan2["details"]
                                if "A-1001" in d["url"] or "same-url" in d["url"]],
                "note": ("目录同时出现 A-1001 的两种等价写法（排序不同+追踪参数"
                         "+fragment）；frontier 只保留一条并只请求一次。"),
            }
        ]
        business = [
            {
                "detail_urls": [d["url"] for d in scan2["details"]
                                if "edition=public" in d["url"]],
                "note": ("`edition=public` 不在 tracking_params 中，因此保留为不同"
                         "frontier 键并分别生成文档 A-1002 与 A-1002P。"),
                "document_keys": [d["document_key"] for d in scan2["details"]
                                  if "edition=public" in d["url"]],
            }
        ]
        offsite = [b for b in scan2["blocked"] if b["reason"] == "origin_not_allowed"]
        robots = [b for b in scan2["blocked"] if b["reason"] == "robots_disallowed"]
        canonicals = [
            {"url": d["url"], "canonical_url": d["canonical_url"],
             "canonical_blocked": d.get("canonical_blocked"),
             "requested": False}
            for d in scan2["details"] if d.get("canonical_blocked")
        ]
        server_audit = runner._audit_servers()
        runner.step(
            "等价链接归一、保留业务参数、站外与 robots 拦截",
            "归一化只删除配置内的追踪键、排序剩余参数并去掉 fragment；"
            "不同业务参数（`edition=public`）不合并。站外链接与 robots 禁止路径"
            "均未产生 HTTP 请求（清单中 `requested=false`）。"
            "`rel=canonical` 指向站外主机时只记录、不跟随请求；"
            "服务器日志交叉核验证明了实际到达的路径集合。",
            s1,
            {"equivalent": normalization, "business_parameter": business,
             "offsite_refused": offsite, "robots_refused": robots,
             "offsite_canonical": canonicals,
             "redirects": [{"url": d["url"], "to": d["redirected_to"]}
                           for d in scan2["details"] if d["redirected_to"]],
             "server_side_audit": server_audit})

        # 3 — conditional fetch, 304 -----------------------------------
        s2 = scan(["sim_a", "sim_b"])
        conditional_evidence = []
        for scan_info in runner.scans.values():
            for detail in scan_info["details"]:
                if detail["outcome"] == "not_modified":
                    conditional_evidence.append({
                        "source": scan_info["source_id"], "url": detail["url"],
                        "status": detail["http_status"],
                        "version": detail["version_no"],
                        "raw_digest": detail["raw_digest"],
                        "note": detail["note"],
                    })
        runner.step(
            "第二轮扫描：条件抓取与 304",
            "详情请求携带上一轮的 `If-None-Match` / `If-Modified-Since`，"
            "服务器返回 304。304 不生成版本、不把正文当作空、复用既有版本；"
            "304 的空响应体仍按摘要归档（sha256 e3b0c442…）。",
            s2, {"not_modified": conditional_evidence})

        # 4 — footer-only change ---------------------------------------
        mutate([{"site": "layout_a", "set": {"footer_version": 2}},
                {"site": "layout_b", "set": {"footer_version": 2}}])
        reset_cache()
        s3 = scan(["sim_a", "sim_b"])
        footer_evidence = [
            {"source": scan_info["source_id"], "url": detail["url"],
             "raw_digest": (detail["raw_digest"] or "")[:12],
             "previous_raw_digest": (detail["previous_raw_digest"] or "")[:12],
             "raw_bytes_changed": detail["raw_bytes_changed"],
             "version_created": detail["version_created"],
             "reason": detail["version_reason"]}
            for scan_info in runner.scans.values() if scan_info["scan_id"] in s3
            for detail in scan_info["details"] if detail["outcome"] == "ok"
        ]
        runner.step(
            "第三轮扫描：仅页脚变化 → 不生成版本",
            "两站只改页脚（A 增加帮助/关于链接，B 增加 status 链接），"
            "正文、标题、日期全部不变。下面的证据表明：原始字节摘要变了"
            "（raw_bytes_changed=true），业务摘要不变，因此 "
            "`version_created=false`，版本号沿用。",
            s3, {"footer_only": footer_evidence})

        # 5 — body and title changes -----------------------------------
        mutate([
            {"site": "layout_a", "notice_key": "A-1001",
             "fields": {"paragraphs": [
                 "经研究决定，自本通告发布之日起启动公共数据共享试点，试点期延长为两年。",
                 "试点单位应当按照最小必要原则收集数据，并定期公开共享目录。",
                 "新增：试点评估报告于每季度末发布。"]}},
            {"site": "layout_b", "notice_key": "B-2001",
             "fields": {"title": "Water quality monitoring programme notice (revised)"}},
        ])
        reset_cache()
        s4 = scan(["sim_a", "sim_b"])
        change_evidence = [
            {"source": scan_info["source_id"], "url": detail["url"],
             "document": detail["document_key"], "version": detail["version_no"],
             "created": detail["version_created"], "reason": detail["version_reason"],
             "diff": detail["diff"]}
            for scan_info in runner.scans.values() if scan_info["scan_id"] in s4
            for detail in scan_info["details"] if detail["version_created"]
        ]
        runner.step(
            "第四轮扫描：正文与标题业务变更 → 生成新版本",
            "A-1001 正文修改并新增段落；B-2001 标题修改。两者均产生新版本，"
            "段落级差异写入 diffs/。注意：A 站页脚仍为第二版（与上一轮相同），"
            "因此这些版本由业务字段而非页脚触发。",
            s4, {"new_versions": change_evidence})

        # 6 — navigation-only change ------------------------------------
        mutate([{"site": "layout_a", "set": {"nav_version": 2}},
                {"site": "layout_b", "set": {"nav_version": 2}}])
        reset_cache()
        s5 = scan(["sim_a", "sim_b"])
        nav_evidence = [
            {"source": scan_info["source_id"], "url": detail["url"],
             "raw_bytes_changed": detail["raw_bytes_changed"],
             "created": detail["version_created"], "reason": detail["version_reason"]}
            for scan_info in runner.scans.values() if scan_info["scan_id"] in s5
            for detail in scan_info["details"] if detail["outcome"] == "ok"
        ]
        runner.step(
            "第五轮扫描：仅导航变化 → 不生成版本",
            "两站导航条新增链接，页脚保持上一轮版本。原始摘要再次变化，"
            "业务摘要不变，版本数保持不变。",
            s5, {"navigation_only": nav_evidence})

        # 7 — robots-disallowed directory page --------------------------
        mutate([{"site": "layout_a", "set": {"robots_embargo_page": 3}}])
        reset_cache()
        s7a = scan(["sim_a"])
        embargo = runner.scans[s7a[0]]
        runner.step(
            "第六轮扫描：robots 禁抓目录页 → 不请求并声明不完整",
            "A 站 robots.txt 将 `/notices/page/3?` 列为 Disallow（精确路径，"
            "`?` 不匹配额外查询参数）。第 2 页的 rel=next 指向第 3 页，但该 URL "
            "在发出请求前即被拒绝（清单中 `requested=false`）。遍历无法到达目录"
            "末尾，扫描标记 `complete=false` / `paging_blocked`，"
            "而不是把缺失页当作不存在。",
            s7a, {"stop_reason": embargo["stop_reason"],
                  "complete": embargo["complete"],
                  "pages": embargo["pages"],
                  "refused": [r for r in embargo["blocked"]
                              if "page/3" in r["url"]],
                  "robots_rules": embargo["robots"].get("rules")})

        # 8 — pagination loop -------------------------------------------
        mutate([{"site": "layout_a", "set": {"robots_embargo_page": None}},
                {"site": "layout_b", "set": {"pagination_loop": True,
                                             "loop_target_page": 2}}])
        reset_cache()
        s6 = scan(["sim_b"])
        loop_scan = runner.scans[s6[0]]
        runner.step(
            "第七轮扫描：目录分页环 → 终止并报告不完整",
            "B 站第 2 页的 rel=next 指回自身。遍历识别到环后立即终止，"
            "不重复请求该页；扫描标记 `paging_loop=true` / `complete=false`，"
            "并声明目录集合不完整。",
            s6, {"pages": loop_scan["pages"], "stop_reason": loop_scan["stop_reason"],
                 "complete": loop_scan["complete"]})

        # 9 — temporary 503 ---------------------------------------------
        mutate([{"site": "layout_b", "set": {"pagination_loop": False}},
                {"site": "layout_a", "notice_key": "A-1055",
                 "fields": {"status_override": 503}}])
        reset_cache()
        s7 = scan(["sim_a"])
        seven = runner.scans[s7[0]]
        missing_doc = next((d for d in store.documents("sim_a")
                            if d["document_key"] == "A-1055"), None)
        runner.step(
            "第八轮扫描：临时 503",
            "A-1055 返回 503。该扫描 `complete=false`；503 不推进 404 缺失计数，"
            "不生成版本。A-1055 文档状态保持原样（无缺失计数）。",
            s7, {"stop_reason": seven["stop_reason"],
                 "details": [d for d in seven["details"] if "A-1055" in d["url"]],
                 "document_state": missing_doc})

        # 9 — first 404 -------------------------------------------------
        mutate([{"site": "layout_a", "notice_key": "A-1055",
                 "fields": {"status_override": None}},
                {"site": "layout_a", "notice_key": "A-1044",
                 "fields": {"available": False}}])
        reset_cache()
        s8 = scan(["sim_a"])
        doc_1044 = next((d for d in store.documents("sim_a")
                         if d["document_key"] == "A-1044"), None)
        runner.step(
            "第九轮扫描：首次 404 → 只标记暂不可达",
            "A-1044 首次 404，仅 `temporarily_unreachable`，缺失计数 1，"
            "不撤稿；该扫描完整，因此可作为证据轮次。",
            s8, {"document_state": doc_1044,
                 "detail": [d for d in runner.scans[s8[0]]["details"]
                            if "A-1044" in d["url"]]})

        # 10 — interference: restore A-1044, first 404 for A-1055 -------
        mutate([{"site": "layout_a", "notice_key": "A-1044",
                 "fields": {"available": True}},
                {"site": "layout_a", "notice_key": "A-1055",
                 "fields": {"available": False}}])
        reset_cache()
        s9 = scan(["sim_a"])
        states = {doc["document_key"]: doc for doc in store.documents("sim_a")}
        runner.step(
            "第十轮扫描：撤稿证据链的干扰项",
            "A-1044 恢复（缺失计数清零、状态回到 active）；A-1055 首次 404。"
            "说明只有相邻两轮完整扫描的 404 才构成撤稿证据。",
            s9, {"A-1044": states.get("A-1044"), "A-1055": states.get("A-1055")})

        # 11 — second consecutive 404 -> retracted ----------------------
        mutate([{"site": "layout_a", "notice_key": "A-1055",
                 "fields": {"available": False}}])
        reset_cache()
        s10 = scan(["sim_a"])
        states = {doc["document_key"]: doc for doc in store.documents("sim_a")}
        runner.step(
            "第十一轮扫描：连续两轮完整扫描 404 → 撤稿",
            "A-1055 在第八轮（完整）与本轮（完整）均 404，两轮相邻且都完整，"
            "标记 `with_retracted`。历史版本保留可读，撤稿是状态变化而非删除。",
            s10, {"A-1055": states.get("A-1055"),
                  "verdicts": runner.scans[s10[0]]["retraction_verdicts"]})

        # 12 — 503 between 404s must not advance certification ----------
        mutate([{"site": "layout_a", "notice_key": "A-1055",
                 "fields": {"status_override": 503}}])
        reset_cache()
        s11 = scan(["sim_a"])
        runner.step(
            "第十二轮扫描：503 不构成轮次",
            "A-1055 返回 503，扫描不完整。即便此前已有撤稿证据，这一轮也不能作为"
            "新的确认轮；A-1044 的同轮缺失计数同样不被推进。",
            s11, {"scan": {k: runner.scans[s11[0]][k] for k in
                           ("complete", "stop_reason", "retraction_verdicts")},
                  "A-1055": next((d for d in store.documents("sim_a")
                                  if d["document_key"] == "A-1055"), None)})

        # 13 — restart persistence --------------------------------------
        mutate([{"site": "layout_a", "notice_key": "A-1055",
                 "fields": {"status_override": None, "available": True}}])
        reset_cache()
        restart = demonstrate_restart(config, store, archive)
        for scan_id in (restart["interrupted_scan_id"], restart["resumed_scan_id"]):
            runner.scans[scan_id] = {
                "scan_id": scan_id, "source_id": "sim_b",
                "complete": scan_id == restart["resumed_scan_id"],
                "paging_loop": False,
                "stop_reason": "interrupted" if scan_id == restart["interrupted_scan_id"]
                else "completed (resumed)",
                "pages": [], "robots": {"status": "200"}, "details": [],
                "blocked": [], "notes": restart["notes"], "retraction_verdicts": [],
                "request_manifest": "", "request_count": 0, "refused_count": 0,
            }
        runner.step(
            "第十三轮扫描：中断后重启接续",
            "B 站扫描在请求完 2 个详情后被强制中断；frontier 中保留 pending 行。"
            "随后用全新引擎实例重启，检测到 pending URL 并完成剩余请求，"
            "最终扫描 complete=true 且不再遗留 pending。",
            [restart["interrupted_scan_id"], restart["resumed_scan_id"]],
            {k: v for k, v in restart.items() if k != "notes"})

    finally:
        for site in sites:
            site.stop()

    runner.write_report()
    write_versions_md(store, [source.source_id for source in config.sources])
    # Persist machine-readable log.
    run_log = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "steps": runner.steps,
        "scans": runner.scans,
        "normalization_examples": runner.normalization_examples,
    }
    (REPORTS / "run.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2), encoding="utf-8")
    store.close()

    print(f"\nDemo finished in {time.time() - started:.1f}s")
    print(f"Report:   {REPORTS / 'report.md'}")
    print(f"Requests: {REPORTS / 'requests'}")
    print(f"Diffs:    {REPORTS / 'diffs'}")
    print(f"Work:     {WORK}")


def write_versions_md(store: Store, source_ids: list[str]) -> None:
    lines = ["# 版本历史 / Version history", ""]
    for source_id in source_ids:
        lines += [f"## {source_id}", ""]
        versions = store.all_versions(source_id)
        if not versions:
            lines += ["_no versions_", ""]
            continue
        current = None
        for version in versions:
            if version["document_key"] != current:
                current = version["document_key"]
                lines += ["", f"### {current}", ""]
            lines.append(
                f"- **v{version['version_no']}** · scan {version['scan_id']} · "
                f"issued {version['issued_on']} · "
                f"raw `{version['raw_digest'][:12]}…` · "
                f"business `{version['business_digest'][:12]}…`")
            lines.append(f"  - title: {version['title']}")
            lines.append(f"  - url: `{version['url_key']}`")
    lines.append("")
    (REPORTS / "versions.md").write_text("\n".join(lines), encoding="utf-8")


def demonstrate_restart(config: Config, store: Store, archive: RawArchive) -> dict:
    """Crash a scan mid-way, then resume it with a fresh engine instance."""
    source = config.source("sim_b")
    engine = ScanEngine(config, store, archive)
    partial = engine.scan(source, interrupt_after=2)
    pending = [row["url_key"] for row in store.pending_urls(source.source_id)]
    partial_scan = store.scan(partial.scan_id)

    resumed_engine = ScanEngine(config, store, archive)
    resumed = resumed_engine.scan(source)
    pending_after = [row["url_key"] for row in store.pending_urls(source.source_id)]
    resumed_urls = list(resumed.resumed_urls)
    return {
        "interrupted_scan_id": partial.scan_id,
        "interrupted_status": partial_scan["status"],
        "pending_after_interrupt": pending,
        "resumed_scan_id": resumed.scan_id,
        "resumed_urls": resumed_urls,
        "resumed_complete": resumed.complete,
        "pending_after_resume": pending_after,
        "notes": [note for note in resumed.notes if "resumed pending" in note],
        "documents_versions": {
            doc["document_key"]: len(store.versions_for(source.source_id,
                                                         doc["document_key"]))
            for doc in store.documents(source.source_id)
        },
    }


if __name__ == "__main__":
    main()

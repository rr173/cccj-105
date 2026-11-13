# 公共研究通告归档管线（Public Notice Archive Pipeline）

一条仅依赖 Python 3.11 标准库的抓取—解析—归档管线，配套**两个本地站点模拟器**
（`sim-a` / `sim-b`），在真实 HTTP 上演示全部要求的行为。**不访问任何外部网站。**

```bash
# 完整演示：启动两站、跑完整场景、生成 reports/
python -m archive_pipeline.demo

# 测试（235 项：21 单元 + 34 真实 HTTP 集成/身份回归 + 7 旧库修复 + 38 资助表格
#       + 30 日程整理 + 42 资格预审 + 63 原始留档空间回收）
python -m unittest discover -s tests -v

# 原始留档空间回收（见 §16；预览只读、计划可重开、隔离可撤销、清理需显式）
python -m archive_pipeline.reclaimcli preview --cutoff 2026-11-01T00:00:00+00:00
python -m archive_pipeline.reclaimcli plan    --cutoff 2026-11-01T00:00:00+00:00
python -m archive_pipeline.reclaimcli show    --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimcli isolate --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimcli restore --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimcli purge   --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimcli journal --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimdemo

# 离线资格预审（独立于扫描/资助/日程，见 §15）
python -m archive_pipeline.eligdemo_main                                   # 样本演示 → reports/eligibility.md
python -m archive_pipeline.eligcli check --clause scenario/eligibility/clauses/E-9001-v1.json
python -m archive_pipeline.eligcli --workdir work/eligdemo run \
    --clause scenario/eligibility/clauses/E-9001-v1.json \
    --applications scenario/eligibility/applications/applications-basic.jsonl
python -m archive_pipeline.eligcli --workdir work/eligdemo show --application-id APP-1004 --text
python -m archive_pipeline.eligcli --workdir work/eligdemo runs

# 旧版混合库修复（先预览、后显式应用）
python -m archive_pipeline.cli repair --preview          # 只读，不改库
python -m archive_pipeline.cli repair --apply            # 单事务原子迁移

# 资助表格结构化作业（独立于网页扫描，见 §13）
python -m archive_pipeline.funddemo                                  # 样本演示
python -m archive_pipeline.fundcli process                           # 处理归档版本
python -m archive_pipeline.fundcli rejections --history              # 拒收原因与历史
python -m archive_pipeline.fundcli facts --archive-id arc_xxxx       # 查询事实

# 申报日程整理作业（独立于网页扫描，见 §14）
python -m archive_pipeline.scheduledemo                              # 样本演示
python -m archive_pipeline.schedcli organize                         # 整理归档公告
python -m archive_pipeline.schedcli current --explain                # 当前有效安排+溯源
python -m archive_pipeline.schedcli current --on-date 2026-04-08     # 指定日期查问
python -m archive_pipeline.schedcli changes --project schedsim::S-1001  # 版本变更记录
python -m archive_pipeline.schedcli pending --history                # 待确认事项与历史

# 手动操作模拟器（可选）
python -m archive_pipeline.simctl start                      # 前台运行两站
python -m archive_pipeline.simctl mutate --site layout_a --set footer_version=2
python -m archive_pipeline.simctl mutate --site layout_b --set-notice B-2005 status_override=503

# 管线 CLI（持久状态位于 work/）
python -m archive_pipeline.cli scan --all
python -m archive_pipeline.cli status
python -m archive_pipeline.cli show-scan 15
python -m archive_pipeline.cli identities --source sim_a
python -m archive_pipeline.cli versions --source sim_a
python -m archive_pipeline.cli versions --source sim_a --document A-1002   # 歧义时拒绝任选
python -m archive_pipeline.cli diff --source sim_a --archive-id arc_xxxx
```

| 站点 | 端口 | 版式 | robots.txt | 目录 | 详情 |
| --- | --- | --- | --- | --- | --- |
| sim-a | 8811 | `layout_a` | `Allow: /notices/`，禁 `/notices/draft-notes`，可禁某目录页 | `article.notice` + `dl.facts`，`rel=next` | 绝对链接、`rel=canonical` |
| sim-b | 8812 | `layout_b` | 仅允许 `archive-bot`，禁 `/feed*sort=desc`、`/internal/`，`*` 组全禁 | `section.feed` 卡片，相对短链，`rel=next` | 短链 302 到长地址，无 canonical |

---

## 1. 统一记录模型与归档身份（identity）

两套适配器（`archive_pipeline/htmlparse.py`）输出同一结构：

```json
{"source":"sim_a","notice_key":"A-1001","title":"…","issued_on":"2026-03-02",
 "body":"第一段\n\n第二段","canonical_url":"http://127.0.0.1:8811/notices/…"}
```

* **页面公告编号不是归档身份。** 页面上的业务键（A 站 `data-key`/`编号`；
  B 站 `data-ref`/`Ref`）归一化后存为 `notice_key`（旧字段名 `document_key`
  仍保留并取相同值，向后兼容），用于展示与检索，但**不作为唯一归档身份**。
  两个业务变体（如完整版 `?view=full` 与公众版 `?edition=public`）即使页面
  印着**相同编号**，也必须各自独立。
* **稳定可查的归档身份 `archive_id`。** 由 `source_id` 与归一化**最终 URL**
  （真实重定向跟随之后的 URL）经 SHA-256 确定性派生（`archive_pipeline/identity.py`，
  形如 `arc_<32hex>`），跨进程稳定、不依赖自增主键、可在入库前计算。
* **业务参数参与区分，追踪参数与 fragment 不造新文档。** 归一化只删除配置的
  追踪键并排序其余参数（见 §2）；保留下来的业务查询参数进入最终 URL，因此
  `?edition=public` 与无该参数是两个 `archive_id`，而 `?utm_source=x#frag` 不是。
* **真实重定向归并、声明式 canonical 不归并。** 真实 HTTP 3xx 记入
  `url_aliases`，别名 URL 解析到**目标** `archive_id`，不另建文档；页面
  `rel=canonical` 只记录、从不请求，也**不参与身份计算**，因此无法把带不同
  业务参数的内容强制并到一起。
* 同一套身份规则统一用于**入库、版本查询、条件请求（304）复用与撤稿判断**，
  不存在只在详情页插入处生效的旁路。
* `body` 是对**提取容器**（A：`.body`；B：`.content`）做块级规范化：
  每个 `p/li/h1–h4` 一段，段间 `\n\n`；段内空白折叠、零宽字符剔除、`<br>` 变空格。
  **标记抖动（加粗、属性、缩进）不改变正文**；段落增减、顺序、可见文字改变会改变正文。
* `issued_on` 支持 `YYYY-MM-DD`、`YYYY年M月D日`、`YYYY/M/D`、`YYYY.M.D`，统一为 ISO 日期；
  无法解析时记 `""` 并在 `issues` 中报告（扫描判为不完整，不静默造数据）。
* `canonical_url` 只记录、**从不请求**；站外 canonical 另记 `canonical_blocked`。

页眉、导航、页脚位于提取容器之外，因此永远不影响业务字段——这是"页脚改动不产生版本"的
结构性保证，而不是靠字符串比较碰运气。

### 1.1 旧编号对应多个变体时如何查询

公告编号可能对应多个 `archive_id`。此时：

* `versions --document A-1002` / `diff --document A-1002` **拒绝任选一个返回**，
  而是在 stderr 列出全部候选及其 `--archive-id`、状态与区分 URL（退出码 4）；
* 用 `identities --source sim_a` 列出全部稳定身份；
* 用 `--archive-id arc_…` 精确定位某一变体；`notice_key` 始终保留在每个版本上，
  仍可按编号检索到全部变体（`Store.identities_for_notice` /
  `versions_for_notice`）。

## 2. URL 归一化（`urlnorm.py`）

用于 frontier 身份与去重：

1. scheme/host 小写；http 与 https 视为不同；默认端口不输出，非默认端口保留；
2. 丢弃 fragment；
3. **仅**删除 `tracking_params` 中配置的查询键（大小写不敏感）；其余参数全部保留；
4. 剩余参数按 `(key, value)` 排序 → 参数顺序不产生重复；
5. 末尾 `/` 与空查询归一。

结果：`?sort=asc&view=full&utm_source=x#frag` 与 `?view=full&sort=asc` 相同；
`?edition=public` 与无此参数**不同**（业务参数不被合并），会形成两个 frontier 键、
两个 `archive_id`（即便是同编号的完整版与公众版，也各自独立，见 §1）。

## 3. 范围与 robots

* **主机白名单**：每个来源有 `allowed_origins`（scheme+host+port 精确匹配）。目录链接、
  `rel=next`、重定向每一跳、canonical、robots 重定向都先过白名单；越界则**不建立连接**，
  并在请求清单中留下 `requested:false` 记录。
* **robots 规则子集**（`robots.py` 顶部与每次扫描报告都会原样声明）：

  - `User-agent` 分组；匹配本产品 token 的组优先于 `*` 组，多个匹配组取并集；
  - `Allow` / `Disallow` 路径前缀规则，支持 `*` 通配与结尾 `$` 锚定；
    按匹配模式长度取最长，长度相同 `Allow` 优先；
  - 规则对 `path?query` 匹配；`#` 注释、空行忽略；
  - `Crawl-delay` 解析并报告但**不采用**（间隔由配置的 `min_interval_ms` 决定）；
  - `Sitemap` 忽略；
  - robots.txt 自身不跟随重定向；不可用（404/5xx/网络/重定向）时按来源配置
    `robots_unavailable_policy`：`disallow_all`（默认）或 `allow_all`。
* **最小请求间隔**：按 origin 记录请求**开始**时间，不足间隔则先等待，等待时长写入清单
  （`spacing_wait_ms`）。robots.txt 也计入间隔。

## 4. 条件抓取与 304（`fetcher.py` / `engine.py`）

* 每个详情 URL 的 `url_identities` 行保存 `ETag` 与 `Last-Modified`，下次请求发送
  `If-None-Match` / `If-Modified-Since`。B 站短链解析到长地址后，**两个行都写入验证器**，
  但两行指向同一个 `archive_id`。
* **304 语义按身份隔离**：归档 304 的原始字节（通常为空）；**不写版本**；沿用**该
  `archive_id` 自己**的既有版本作为当前版本；`content_digest` 保持原值——绝不把 304
  当空正文，也绝不借用同编号另一变体的正文。
  304 同时是"资源仍在"的正向信号，会把该身份的
  `temporarily_unreachable`/`with_retracted` 复活（不影响兄弟变体）。
* 304 而该身份本地无版本 = 协议矛盾：清空验证器使下次强制全量，扫描判为不完整并报告
  `304_without_version`。

## 5. 归档与版本（`archive.py` / `store.py`）

* 原始响应字节按 **SHA-256 摘要**写入 `work/archive/<source>/<yyyymmdd>/<digest>.bin`
  （>5MB gzip 存储），同名 `.json` 记录 URL、最终 URL、状态、响应头、扫描号。内容寻址，
  不可变，可复算。
* 规范化正文另算 `content_digest`；判断是否新版本的 `business_digest =
  SHA256(title, issued_on, body)`。
* **版本规则（按 `archive_id` 独立成链）**：`business_digest` 与该身份上一版本不同
  （或无上一版本）→ 在该变体内追加版本；相同 → 不生成版本，即使原始字节因页眉/页脚
  变化而不同（报告中同时给出 `raw_bytes_changed: true/false` 与两种摘要，便于核验）。
  两个同编号变体各有独立版本号序列，互不影响、互不覆盖。
* 版本只追加、不修改；撤稿后历史版本仍可读（撤稿是状态，不是删除）。

## 6. 轮次完成判定与撤稿（`engine.py`）

一个**扫描轮次**从入口页开始，结束时有明确 `complete` 判定。以下任一发生则 `complete=false`
并记录 `stop_reason`：

| 情形 | stop_reason |
| --- | --- |
| 分页环（next 指向已访问目录） | `paging_loop: …` |
| next 指向被 robots/范围拒绝的目录页 | `paging_blocked: …` |
| 目录页非 200 / 网络错误 / 解析失败 | `directory_http_*` / `directory_parse_error` |
| 目录页预算耗尽 | `page_budget_exhausted` |
| 详情 5xx / 网络错误 / 过大 / 解析失败 / 缺业务键 | `detail_http_503` 等 |
| 304 但无本地版本 | `detail_304_without_version` |
| 被强制中断（崩溃模拟） | `interrupted` |

**撤稿规则（严格按"连续两轮完整扫描均为 404"，且按身份独立裁决）**：

1. 404/410 记入 `scan_misses`（带 `archive_id`）并按 URL 累加 `missing_streak`；
   该身份进入 `temporarily_unreachable`。**单次 404 绝不撤稿。**
2. 扫描结束时若有完整判定，做后置裁决：取此前**最近的完整轮次** `R_last`；
   若本轮与 `R_last` 都对**同一 `archive_id`** 观察到 404 → 标记该身份
   `with_retracted`。同编号的另一变体有独立身份与独立证据，不会被连带撤稿。
3. 未完成轮次（503、网络错误、解析失败、robots 拦截、分页环、中断）**跳过**：
   既不计入，也不清除既有证据。完整轮次中该身份正常返回则打断链（计数归零）。
4. 503 从不推进缺失计数；`scan_misses` 只接受 404/410。
5. 裁决写入扫描结果的 `retraction_verdicts`（含 `archive_id`、
   `skipped_incomplete_scans`、证据描述、保留的版本数），并出现在报告和扫描项中。

## 7. 持久前沿与重启接续

* `url_identities(source_id, url_key)` 保存状态（`pending`/`processed`）、
  归属的 `archive_id` 与 `final_url_key`、是否真实重定向别名（`is_alias`）、
  验证器、缺失计数、最后版本号。
* 目录页发现详情 URL 即登记为 `pending`；处理完改为 `processed`；可重试失败
  （如站外重定向被拦）保持 `pending`，下一轮自然重试。
* 进程崩溃/强制中断时，未处理的 `pending` 行留在 SQLite；新进程只需重新打开数据库，
  扫描会带上这些行（`resumed_urls` 与备注列出它们），并完成剩余请求。
* 演示用 `interrupt_after=2` 真实验证：中断后 3 条 pending，重启后全部消化，无重复版本。

## 8. 每次扫描的请求清单

`reports/requests/scan-<id>.json`（CLI 可用 `show-scan <id>` 查看）：

```json
{"scan_id":1,"source_id":"sim_a",
 "requests":[{"kind":"robots","url":"…/robots.txt","status":200,"requested":true,
              "spacing_wait_ms":0.0,"conditional_headers":{}},
             {"kind":"directory","url":"…/notices","status":200, …},
             {"kind":"detail","url":"…?sort=asc&view=full","status":200,
              "conditional_headers":{"If-None-Match":"…"}},
             {"kind":"detail","url":"https://outside.example.org/…",
              "status":null,"requested":false,"outcome":"redirect_blocked",
              "redirect_from":"…/archive/offsite-hop/B-2004"}],
 "blocked_candidates":[{"url":"https://example.invalid/offsite",
                        "reason":"origin_not_allowed","where":"detail link on …"}],
 "seed_urls":["…"]}
```

每条记录都含归一化 URL、是否真正发出请求、HTTP 状态、条件头、强制等待；被拒绝的候选
（站外链接、robots 禁止路径、越界重定向）保留在 `blocked_candidates`，因此"为什么没有
这个请求"可审计。

## 8.5 串档回归夹具（真实本地 HTTP，`tests/test_identity_regression.py`）

夹具把模拟站 A 公众版页面的公告编号改成与完整版**相同的 `A-1002`**，同时保留两者的
正文与地址差异，全部走真实 socket 请求，并断言 SQLite 中**实际持久化的内容**：

| 夹具 | 断言 |
| --- | --- |
| 同编号两变体 | 两个独立 `archive_id`、各自独立 v1 与各自正文；编号仍记录为 `A-1002` |
| 只改完整版正文 | 完整版进 v2、公众版停留 v1 且当前正文不被覆盖 |
| 其一返回 304 | 304 只复用本身份正文，绝不返回另一变体正文；两 URL 验证器独立 |
| 仅其一连续 404 | 该身份 `with_retracted`，另一身份仍 `active` 且版本保留；裁决只列该身份 |
| 追踪参数别名 / 真实重定向 | 不新增身份、不新增版本；短链标记 `is_alias` 并共享目标身份 |
| 声明式 canonical | 只记录不用于身份，业务参数变体不被归并 |

旧格式混合库的修复夹具（`tests/test_repair_legacy.py`）手工搭建升级前 schema 的混合
历史，断言：修复前预览只读、应用后历史归属与正文/顺序、重复应用 no-op、缺失原始留档
时隔离，以及在 6 个迁移步骤分别注入故障后回滚、重开数据库仍可安全再执行。

## 9. 演示覆盖矩阵（`python -m archive_pipeline.demo`）

| 步骤 | 场景 | 可核验证据 |
| --- | --- | --- |
| 1 | 基线扫描两站 | 首次版本、robots/目录/详情请求清单 |
| 2 | 归一/业务参数/站外/robots/canonical | 等价链接只请求一次；`edition=public` 独立文档；`requested:false` 记录；`mirror.example.net` canonical 只记录不请求；服务器日志交叉核验 |
| 3 | 第二轮条件抓取 | 全部 304；复用 v1；304 空体按摘要归档、元数据 `status:304` |
| 4 | 仅页脚变化 | `raw_bytes_changed=true` 且 `version_created=false` |
| 5 | 正文/标题变化 | 新版本 + 段落级 `diffs/` |
| 6 | 仅导航变化 | 原始字节变、业务摘要不变、版本数不变 |
| 7 | robots 禁抓目录页 | 未发请求、`paging_blocked`、`complete=false` |
| 8 | 分页环 | 环点终止、`paging_loop`、不重复请求 |
| 9 | 临时 503 | `complete=false`、缺失计数不动、无版本 |
| 10 | 首次 404 | `temporarily_unreachable`、streak=1 |
| 11 | 干扰项 | 恢复文档使计数清零 |
| 12 | 连续两轮完整 404 | `with_retracted`，版本保留 |
| 13 | 503 插在中间 | 未完成轮次被跳过；`skipped_incomplete_scans` 列明；证据不清零 |
| 14 | 中断重启 | pending 行接续、`resumed_urls`、无重复版本 |

`reports/report.md` 是完整叙述与表格；`reports/run.json` 是可机读日志；
`reports/versions.md` 是逐文档版本历史；`reports/diffs/*.json` 是段落差异。

## 10. 目录结构

```
archive_pipeline/
  config.py    # JSON 配置校验（来源、白名单、追踪键、间隔、适配器）
  identity.py  # 稳定归档身份：由 source + 归一化最终 URL 派生 archive_id，重定向别名解析
  urlnorm.py   # 归一化
  robots.py    # robots 子集
  fetcher.py   # 白名单/robots/间隔/重定向/条件 GET
  htmlparse.py # 容错 HTML 树 + 两套适配器 + 正文规范化
  archive.py   # 内容寻址原始字节归档
  store.py     # SQLite：url_identities/document_identities/url_aliases/versions/
               #         scan_misses/scan_items/repair_runs/quarantine_notes
  worklock.py  # 工作目录共享/独占锁：正式采集持共享，回收隔离持独占
  reclaim.py   # 孤立原始响应回收：只读引用判断、计划、逐文件日志、隔离/撤销/清理
  reclaimcli.py# preview/plan/show/journal/plans/isolate/restore/purge 命令
  reclaimsample.py # 合成留档现场（跨日期副本、损坏组、gzip、宽限期…）
  reclaimdemo.py   # 回收端到端演示与自检
  repair.py    # 旧库混合历史的只读预览 + 单事务幂等修复
  engine.py    # 扫描、身份解析、版本、轮次完成、撤稿裁决、恢复
  demo.py      # 全场景演示与报告
  cli.py       # scan/status/show-scan/identities/versions/diff/repair
  sim_html.py  # 两版式 HTML 与 robots.txt 生成
  sim_server.py# 两个本地 HTTP 站点（ETag/Last-Modified/302/404/503/版次级404）
  simctl.py    # 场景状态修改
  fundconfig.py# 资助作业配置：表格选择条件 + 机构/项目/年度/金额标题别名
  fundgrid.py  # HTML 表格 → 逻辑网格（colspan/rowspan 展开，结构拒收）
  fundamount.py# 金额与单位解析（千分位/全角/小数，Decimal 精确换算）
  fundtable.py # 列头映射、明细提取、合计核对
  funding.py   # 资助作业：只读消费管线库与原始留档 → funding.sqlite3
  fundcli.py   # process/rejections/facts 命令
  fundsample.py# 可重现通告留档样本构建器
  funddemo.py  # 资助作业演示与报告
  scheduleconfig.py # 日程作业词表：公告类型/节点角色/轮次/引用/取消短语
  scheduledate.py   # 中文与数字日期解析（省略年补齐、区间、字符区间溯源）
  scheduleextract.py# 段落/句子级证据抽取：轮次、角色、引用目标、取消语义
  schedule.py # 日程作业：只读消费管线库与留档 → schedule.sqlite3（账本+快照）
  schedcli.py # organize/current/changes/pending 命令
  schedulesample.py # 日程作业可重现样本构建器（两来源、多轮、跨年、撤销…）
  scheduledemo.py   # 日程作业演示与报告
  eligvalue.py # 申请字段类型读取：缺失/null/类型不符 → 未知值；Decimal 精确比较
  eligclause.py# 条款校验与归档绑定：版本、字段声明、常量、引用、环、引文逐字核对
  eligengine.py# 三值求值与完整解释树（不短路；成员计数区间与逐人结果）
  eligibility.py# 只读绑定归档、逐行处理 JSONL、结果库与三种结论汇总
  eligcli.py   # check/clause/run/show/runs 命令
  eligsample.py# 合成归档样本构建器（同编号变体、两版本）
  eligdemo.py  # 人工编录条款、错误引文样例与合成申请构建器
  eligdemo_main.py # 资格预审端到端演示与报告
config/sources.json   config/funding_tables.json   config/schedule_vocab.json
scenario/   scenario/funding/   scenario/schedule/   scenario/eligibility/
tests/   reports/   work/
```

## 11. 旧版混合历史修复（`repair.py` / `cli repair`）

升级前的库以页面公告编号为唯一键，两个同编号业务变体的观察会被交错追加进
**同一条版本链**。修复入口在**现有 SQLite 库**上运行，只使用库里既有的耐久
证据与不可变原始留档：每个旧版本对应的 `url_key`、真实重定向（`redirects`）、
`scan_items` / `scan_misses` / `scans` 事实，以及内容寻址的原始字节归档。

**流程（先只读预览，再显式应用）：**

```bash
python -m archive_pipeline.cli repair --preview     # 输出完整计划，不写库
python -m archive_pipeline.cli repair --apply       # 单事务迁移
```

修复规则与保证：

* 按每个旧版本的**最终 URL**（真实重定向解析后）重新派生 `archive_id`，把交错
  链拆回各变体；变体内部按 `(scan_id, version_id)` 保留修改顺序并在变体内重新
  编号 1..n，旧编号保留在 `legacy_version_no`，原始 `url_key` 也保留，因此**旧
  版本引用可追溯**到修复后的归属。
* **不改写原始文件、不抹去扫描事实。** 内容寻址的 `work/archive` 完全不动；
  旧表在同一事务内改名为 `versions_legacy` / `url_frontier_legacy` /
  `document_meta_legacy` / `redirects_legacy` / `scan_misses_legacy` 永久留存，
  `scans` / `scan_items` 原样保留（`scan_items` 仅补一列 `archive_id`）。
* **证据不足或冲突不单挑、不猜正文。** 原始留档缺失（blob 不存在）、同一身份
  发布过互相冲突的编号、或仅有 frontier 而无任何版本的身份，会进入
  `quarantine_notes` 并标记 `document_identities.state='quarantined'` /
  `evidence_state='quarantined'`，保留全部已有正文但**不得冒充已确认的当前内容**。
* **原子性与幂等。** 整个迁移在一个 SQLite 事务内（注意 Python
  `executescript` 会隐式 COMMIT，修复因此逐条 `execute`）；任何步骤失败都
  ROLLBACK，不留下半迁移状态。模式版本写入 `PRAGMA user_version=2`，与表变更
  同事务提交。重复执行为 no-op，不会再次拆分或新增版本。
* 可用 `--fail-point <step>` 注入受控故障（`rename_versions`、
  `recreate_tables`、`insert_identities`、`insert_versions`、
  `copy_frontier_state` 等）；故障后重开数据库仍为旧形态，可安全再执行——
  该保证由 `tests/test_repair_legacy.py` 在每个故障点断言。
* 对旧库直接 `scan` 会被拒绝（退出码 3）并提示先修复，避免新代码写在未迁移的
  混合库上。

## 12. 已知边界

* 仅支持上述 robots 子集；`Crawl-delay` 不采用；不解析 `Sitemap`。
* 目录页不做条件抓取（只归档用于审计），详情页才使用验证器。
* `archive_id` 以归一化最终 URL 为身份：同一业务变体若永久更换了业务查询形态
  （而非追踪参数或真实重定向），会得到新的身份——这是"业务参数必须参与区分"的
  直接后果；旧身份与版本不会被删除，可经 `notice_key` 与 `url_aliases` 追溯。
* 时间戳含扫描生成时间，因此 `reports/` 每次运行会变化；数据库与归档按运行重建于 `work/`
  （演示启动时清理）。

## 13. 资助表格结构化作业（`funding.py` / `fundcli.py`）

通告正文里的科研资助额度常藏在 HTML 表格中，纯文本归档丢失了合并单元格的上下级
关系。本作业把已归档版本对应的**原始 HTML** 中的资助表格整理成 SQLite 事实记录：
机构、资助项目、年度、金额（精确到人民币元）。

**独立入口、只读消费。** 作业与网页扫描完全分离：不访问站点、不调用语言模型、
不迁移旧库。它以只读方式打开身份模式的 `work/pipeline.sqlite3`（旧模式库会被
明确拒绝并提示先 `repair`，绝不代为迁移），按 `raw_digest` 从 `work/archive/`
读取原始字节并复核 SHA-256——**留档缺失（`missing_raw`）或摘要不符
（`digest_mismatch`）是明确的版本级失败，绝不拿规范化正文顶替**。结果写入独立的
`work/funding.sqlite3`，管线库与原始留档保持原样。

```bash
python -m archive_pipeline.funddemo                     # 样本端到端演示 → reports/funding.md
python -m archive_pipeline.fundcli process              # 处理全部归档版本（可 --source/--archive-id）
python -m archive_pipeline.fundcli rejections --history # 拒收原因（含历次诊断）
python -m archive_pipeline.fundcli facts --archive-id arc_xxxx --table 0 --row 3
```

### 13.1 表格选择与健康检查配置（`config/funding_tables.json`）

一份纯 JSON 声明全部规则，作业不靠"取每行第几个文本"猜字段：

* `select`：表格选择条件（`id` / `class` / `caption_pattern` 正则，任一规则命中即选中）；
* `headers`：`institution`（机构）、`project`（资助项目）、`amount`（资助金额）、
  `total`（合计行标签）的标题别名，按规范化文本精确匹配（忽略一处单位括注如`（万元）`）；
* `ignore_headers`：可安全忽略的列（默认 备注/说明）。

任何列匹配不上别名 → 整表拒收（`unmapped_column`）。配置摘要（canonical JSON 的
SHA-256）记录在每条结果与事实上；**改配置后重跑 `process` 即重处理被拒收的表**，
旧诊断保留在 `outcome_history` 可追溯（见 §13.5）。

### 13.2 支持的 HTML 子集与逻辑网格

* `<table>` 可含 `<caption>`；`thead/tbody/tfoot` 仅作行组、不携带语义，行按文档序处理；
* `<tr>` 必须是表格或行组的直接子元素，`<th>/<td>` 必须是 `<tr>` 的直接子元素；
* `colspan/rowspan` 必须为显式正整数（HTML 的 `0`"跨到末尾"不在子集内）；
* 不允许嵌套表格；展开后网格必须是完整矩形：合并区域不重叠、rowspan 不越界、无空洞；
* 表头为前 1–2 行全 `<th>` 行（两层表头：年度在上层、金额在下层）。

合并单元格展开为可解释的逻辑网格（`fundgrid.py`）：每个网格位置都能回到覆盖它的
物理单元格。机构通过 rowspan 覆盖多行时，明细行自动继承，并记录继承自哪个物理
行头；年度列组（colspan）下必须恰有一列匹配金额别名，其余列必须匹配忽略别名。
表头、明细、合计三类行严格区分。

### 13.3 表格级拒收原因（不挪动数字凑行）

结构类：`nested_table`、`misplaced_row`、`misplaced_cell`、`invalid_span`、
`rowspan_out_of_bounds`、`overlap_merge`、`grid_hole`、`empty_table`、`no_header`、
`unsupported_header_depth`、`header_row_in_body`、`unmapped_column`、`column_mapping`、
`duplicate_year`、`amount_spans_years`（一个金额格横跨两个年度列，绝不复制到两个年度）、
`amount_spans_rows`、`multiple_total_rows`、`no_details`。

数据类：`unknown_currency`（如美元）、`conflicting_unit`（同层冲突单位）、
`missing_unit`、`unparseable_amount`（破折号/待定等，绝不补零）、`missing_field`、
`total_mismatch`。版本级：`missing_raw`、`digest_mismatch`。

### 13.4 金额与单位

* 单位可写在表格说明（`单位：万元` 或 `（万元）`）或金额列头（`资助金额（万元）`）；
  支持人民币 元/千元/万元；**列头局部单位优先于表格单位**；
* 支持千分位（`,`/`，`）、全角数字与小数，转换为精确人民币元 `Decimal`
  （全程不经过二进制浮点，如 `0.1` 万元 = `1000` 元整）；
* 合计行不入账；同表按年度列出的合计与已识别明细在统一单位下逐项核对，
  差额或无法解析的金额使整张表进入待处理区（保留网格、候选明细与具体原因），
  其余正常表格不受影响。

### 13.5 事实模型、溯源与幂等

`facts` 以 `(source_id, archive_id, version_no, table_index, row_index, year)`
定位：同编号的公众版与完整版各有独立事实、互不覆盖，各版本结果都保留。每条明细
携带：来源、归档身份、版本号、表格序号、原始物理单元格位置（金额格/机构格/项目格）、
继承自哪些合并表头与行头（`header_path_json`/`row_header_path_json`）、原文金额
（`amount_text`）与单位依据（`unit_source`），可回到网页逐字段核对。

* 每张表在一个事务内处理：处理途中退出不留部分可查询明细，重开可安全续处理；
* 相同输入再次处理不重复记账（已入账且配置与留档摘要未变的表跳过）；
* 被拒收的表在配置修正后重处理；`table_outcomes` 是当前状态，
  `outcome_history` 保留每一次诊断（含版本级错误），只有完整通过的结果进入事实查询。

### 13.6 可重现样本与演示（`scenario/funding/` + `funddemo.py`）

`python -m archive_pipeline.funddemo` 在 `work/funddemo/` 构建 12 个归档版本的
样本（来源 `fundsim`，身份模式管线库 + 内容寻址原始留档），覆盖：跨行机构、
跨列年度、局部金额单位（万元覆盖表级元）、全角数字、千分位、合计核对
（F-3001 完整版/公众版，同编号两个身份）；错位合并（F-3002）；跨年度金额格
（F-3003）；差额合计（F-3004）；留档缺失（F-3005）与摘要损坏（F-3006）；
缺别名的表（F-3007，改配置后入账）；破折号/待定（F-3008）；嵌套表格（F-3009）；
未知币种（F-3010）；缺单位（F-3011）。演示依次展示中途退出与续处理、正确展开
与换算、双版本事实共存、全部拒收原因、改配置后重处理与诊断历史，报告写入
`reports/funding.md`。

### 13.7 资助作业测试（`tests/test_funding.py`，38 项）

期望事实在测试中独立于原始表格逐条列出（机构/项目/年度/金额/来源单元格），
与数据库实际记录逐字段核对；覆盖网格展开与继承、合并区域重叠/越界/空洞、
金额与单位解析（含 Decimal 精确性）、列映射拒收、合计核对、拒收隔离、
重复处理幂等、中途退出回滚与续处理、配置修正重处理、旧库拒绝。

### 13.8 资助作业已知边界

* 仅支持 §13.2 的 HTML 子集；表格说明仅取 `<caption>`；合计行标签必须在第 0 列；
* 年度表头形如 `2024`/`2024年`/`2024年度`；币种仅人民币 元/千元/万元；
* 配置改变后再次 `process` 会以新配置重处理所有表（当前状态以最近一次运行的
  配置为准，历史全程可查）；作业为单进程批量工具，不做并发处理。

## 14. 申报日程整理作业（`schedule.py` / `schedcli.py`）

科研管理处按周安排项目申报服务时，需要回答“哪个项目哪天开放、哪天停止收件、
评审何时开始”，并能说明每个日期出自哪份公告的哪段文字。本作业把已归档公告
版本整理成可查问的有效日程，与网页扫描和资助表格作业完全独立：**不访问网站、
不调用语言模型、不迁移旧库、不改写扫描结果与原始留档**；以只读方式打开身份模式
的 `work/pipeline.sqlite3`（旧模式库明确拒绝，提示先 `repair`），并按
`raw_digest` 软复核 `work/archive/` 原始字节（缺失/不符只记录 `raw_status`，
绝不拿正文顶替）。结果写入独立的 `work/schedule.sqlite3`。

```bash
python -m archive_pipeline.scheduledemo                # 样本端到端演示 → reports/schedule.md
python -m archive_pipeline.schedcli organize           # 整理全部归档公告（可 --source）
python -m archive_pipeline.schedcli current            # 当前有效安排（可 --project/--round）
python -m archive_pipeline.schedcli current --on-date 2026-04-08 --explain
python -m archive_pipeline.schedcli changes --project schedsim::S-1001 [--run N]
python -m archive_pipeline.schedcli pending [--history] [--source ID]
```

### 14.1 词表驱动的识别（`config/schedule_vocab.json` / `scheduleconfig.py`）

一份纯 JSON 声明全部业务短语，解析器不硬编码任何节点名称：

* `notice_types`：申报/补充（含延期）/更正/撤销四类公告的标题与正文别名，
  专门类型优先于通用“申报通知”，标题命中优先于正文，避免补充通知引用原公告
  标题时被误判；
* `roles`：报名开放 `apply_open`、材料提交 `submit_deadline`、补正
  `supplement_deadline`、专家评审 `review_start`，每个角色标注 `open`/
  `deadline` 语义，用于日期区间取首/取尾；别名跨角色冲突时配置校验直接报错；
* `round_markers`：第一/二/三轮（期）标记与无标记时的默认轮次；
* `target_ref`：显式编号引用（含“来源：X，编号：Y”与“编号：Y（来源：X）”
  两种语序）、项目代码正则、以及由公告编号派生原编号的隐式后缀；
* `cancellation`：整轮撤销短语与单节点取消短语，二者语义严格区分。

词表摘要（canonical JSON 的 SHA-256）盖在每条证据、待确认事项与账本记录上；
**改词表后重新整理会产生一套独立可追溯的新判断，旧判断与原因永久保留在历史表。**

### 14.2 日期解析与溯源（`scheduledate.py` / `scheduleextract.py`）

* 支持 `2026年3月10日`、`二〇二六年三月十日`（含十/二十/三十一）、
  `2026-03-10`、`2026/3/10`、`2026.3.10` 与全角数字；非法日历日期
  （如 13 月 40 日）显式报错，不静默造数；
* **省略年份（`3月10日`/`3月10号`）一律以公告发布日期的年份补齐**，
  不按语义跨年猜测；证据中以 `had_year=1` 标明；
* `至/到/~/—/-` 连接的日期区间整体识别，按角色 `open`/`deadline` 语义取
  起点或终点，两个端点的原文都保留；
* 抽取以**段落（`\n\n` 切分）→ 句子（句末标点切分）→ 日期**三级定位，
  每条证据保存段落序号、段落起点、句子起止与日期在**正文中的半开字符区间**、
  原始日期写法、归一 ISO 日期、角色短语及其位置、轮次与轮次短语——
  `body[date_start:date_end]` 必须恰好等于原始写法，可逐字回到原文核对。

### 14.3 事件账本、当前快照与事务边界（`schedule.py`）

* `node_ledger` 是**只追加的事件账本**：`set`（含延期）、`cancel_node`
  （单节点取消）、`cancel_round`（整轮撤销）。账本唯一键包含完整证据坐标
  与词表摘要，相同输入重复整理不产生任何重复行；
* 每个公告版本在**独立事务**内写入抽取结果、证据、待确认事项与账本行；
  全部版本处理完后，再用**一个事务**整体替换可查询快照
  （`current_nodes` / `round_schedules` / `pending_current`）。处理途中崩溃
  只回滚当前版本，快照维持上一次发布状态——重开数据库看不到任何部分发布的
  安排，续跑安全且幂等；
* 新版本只改一个日期时，账本追加一行 `set`，快照里其他节点保持不变；
  **延期前的旧日期在 `changes` 历史中完整可查，但不再出现在 `current`**；
* 整轮撤销后该轮节点从当前查询隐藏（`round_schedules.status=
  round_cancelled`，账本仍在）；单节点取消只把该节点置 `cancelled`，
  轮次与其余节点保持 `open`——取消评审绝不等于撤销整轮，反之亦然。

### 14.4 跨公告引用与待人工确认

补充/更正/撤销公告先按**来源 + 公告编号**解析唯一目标：

* 命中恰好一个归档身份才并入该项目的安排；零命中（`target_not_found`）、
  多命中（`target_ambiguous`）、无任何可解析引用（`target_unspecified`）
  全部进入待确认，不猜测、不任选；
* 更正公告的角色必须是目标公告已安排过的角色，否则记 `role_mismatch`
  待确认；“原写 X 有误”引用的旧日期只作证据、不建节点，只有“更正为 Y”
  的新日期入账；
* 有日期但无角色短语 → `unmapped_date`；非申报公告只有模糊时间表述、
  无具体日期 → `vague_statement`。待确认材料**永不并入**已确认安排；
  `pending_history` 保留每次判断，词表更新使旧事项被新节点覆盖后自动从
  当前待确认列表关闭（历史仍可查）。

### 14.5 可重现样本与覆盖矩阵（`scenario/schedule/` + `schedulesample.py`）

`python -m archive_pipeline.scheduledemo` 在 `work/schedemo/` 构建两来源
（schedsim / schedsim2）17 个归档版本的样本，覆盖：同一项目两轮日期
（S-1001）、跨年报名（S-1002）、仅延长收件期限且同公告两版本
（S-1003 v1/v2）、整轮撤销（S-1004）、省略年份（S-1005）、单节点取消与
先更正后取消（S-1006/S-1008/S-1013）、同编号两身份（S-1007 完整版/公众版）、
跨来源显式引用（S-1009→schedsim2/X-2001）、整体模糊（S-1010）、中文数字
（S-1011）、词表外角色（S-1012，扩词表后入账）。

### 14.6 日程作业测试（`tests/test_schedule.py`，30 项）

预期日期与正文位置在测试中独立列出，与数据库实际记录逐项核对：日期解析
（中文/全角/省略年/区间/非法日期/字符区间）、两轮分别建安排、跨年归一、
延期历史替换、整轮撤销与单节点取消的范围差异、更正只改目标角色、跨来源
解析、待确认不被并入、幂等重整理、受控中断后的回滚与恢复、词表变更保留旧
判断、管线库与原始留档只读、旧模式库拒绝。

### 14.7 日程作业已知边界

* 轮次识别依赖词表中的“第 N 轮/期”标记，无标记公告归入默认轮次；
* 省略年份只按发布年份补齐，不做“12 月公告写 1 月大概率属次年”之类推断；
* 跨公告引用仅支持词表声明的编号/项目代码形式，自然语言隐射（如“前述项目”）
  保持待人工确认；作业为单进程批量工具，不做并发处理。

## 15. 离线资格预审作业（`eligibility.py` / `eligcli.py`）

申报服务人员收齐材料前先做资格预审：把人工编录的资格条款作用到一批申请资料，
逐项给出**满足 / 不满足 / 待补证**及判断依据。作业与网页扫描、资助表格、日程
整理完全独立：不访问网站、不调用语言模型、不迁移旧库；以**只读**方式打开身份
模式的 `work/pipeline.sqlite3`，只用于核对条款绑定的归档版本与引文；不依赖
资助表或日程结果。结果写入独立的 `work/eligibility.sqlite3`。

```bash
python -m archive_pipeline.eligdemo_main      # 构建样本 workdir 并跑完整演示 → reports/eligibility.md
python -m archive_pipeline.eligcli check --clause PATH      # 只检查条款，不读申请
python -m archive_pipeline.eligcli --workdir W run --clause PATH --applications FILE [--progress]
python -m archive_pipeline.eligcli --workdir W show --application-id ID [--run N] [--text]
python -m archive_pipeline.eligcli --workdir W runs
```

退出码：`0` 成功；`2` 用法/运行错误；`3` **条款无效，未产出任何判定**；
`4` **申请文件结构损坏**——运行标记未完成、保留已写前缀、无成功汇总。

### 15.1 条款文件与版本绑定（`eligclause.py`）

条款为纯 JSON（`format: eligibility-clause/1`，样例
`scenario/eligibility/clauses/E-9001-v1.json`）。每一份条款**精确绑定**
`source_id` + `archive_id` + `version_no`，并声明主体与成员两个作用域的字段
类型（`string` / `boolean` / `decimal` / `date`）。每个具名条件有唯一 ID、
`expr` 与一条**引文**：正文中的半开字符区间 `[start,end)` 加上原句 `quote`。
程序从归档库读出该版本 `body`，只有 `body[start:end] == quote` 时才接受条款：

* 不存在的版本 / 身份 → `unknown_version`（并列出该身份实际存在的版本）；
* 引文落在**同编号另一变体**（如公众版 vs 完整版）→ `citation_in_other_variant`；
* 区间偏移、引文在别处出现但切片不等于原句 → `citation_mismatch`；
* 引文在绑定正文中完全找不到 → `citation_not_found`；越界 → `citation_out_of_bounds`。

条款编录错误在**读取第一份申请之前**一次性报出，绝不降级成“待补证”：
未知键/拼写错误、未声明字段、作用域不符、常量类型错误（十进制常量必须是字符串，
布尔/日期同理）、对字符串做大小比较、`at_least` 非非负整数、
`count` 出现在成员作用域、未定义引用、**引用环**、跨主体/成员作用域引用、
根条件未定义或非主体作用域、重复条件 ID。**不运行任何用户提供的 Python 表达式。**

条款换版必须显式提供另一份文件；单次运行始终使用开始时验证的内容，条款摘要
（canonical JSON SHA-256）与文件 SHA-256 记入运行摘要与每份解释。

### 15.2 条件语言

```json
{"field": "F", "op": "eq|gt|ge|lt|le", "value": CONSTANT}
{"field": "F", "op": "in", "values": [CONSTANT, ...]}
{"all": [EXPR, ...]}   {"any": [EXPR, ...]}   {"not": EXPR}
{"ref": "CONDITION_ID"}
{"count": {"predicate": MEMBER_EXPR, "at_least": K}}
```

* `count` 只能位于主体作用域，谓词在**成员作用域**逐人求值；同一谓词里的学历
  与年限必然读自同一名成员，不能拿甲的学历拼乙的年限；
* 前向引用允许（见样例 `ROOT` 引用后定义的 `PI_CONDITION`/`COUNT_DOCTORS`）；
* 成员谓词只能读当前成员字段；引用环、越界引用在条款校验时报出位置并拒绝。

### 15.3 三值语义与成员计数（`eligengine.py`）

固定规则：`all` 有假即假、全真才真、否则未知（空 `all` 为真）；`any` 有真即真、
全假才假、否则未知（空 `any` 为假）；`not` 保留未知；缺失值之间比较不等于真。
整条 `all` 只要已有**明确反例**即为不满足，即使其他材料缺失——不会机械地改判
待补证（例：诚信明确有失信记录时，即使法人登记日期缺失，总体仍是不满足）。

成员计数先算已知满足人数 `t` 与结果未知人数 `u`：`complete=true` 时人数区间
`[t, t+u]`；`complete=false` 时未列成员只可能增加满足人数，区间 `[t, ∞)`。
下界达到 `k` → 满足；上界小于 `k` → 不满足；其余待补证；`k=0` 恒满足。

样例（“至少两名博士且各有三年经验”，`COUNT_DOCTORS`）：

| 申请 | 情形 | 人数区间 | 计数结论 | 总体 |
| --- | --- | --- | --- | --- |
| APP-1001 | 完整名单，2 真 1 假 1 未知 | `[2, 3]` | 满足 | 满足 |
| APP-1003 | `complete=false` 但已知 3 真 | `[3, ∞)` | 满足 | 满足 |
| APP-1004 | 完整名单，1 真 1 假 1 未知（卡门槛） | `[1, 2]` | 待补证 | 待补证 |
| APP-1005 | 两人各满足一半（学历/年限分属不同人） | `[0, 0]` | 不满足 | 不满足 |
| APP-1012 | 未列全且有未知成员 | `[1, ∞)` | 待补证 | 待补证 |

### 15.4 申请资料（JSONL）与未知值

每行一个 JSON 对象：唯一 `application_id`（字符串）、`subject`（主体对象）、
`items`（成员对象数组）、`complete`（布尔）。`complete=true` + `items=[]` 表示
**确定没有成员**（完整空名单），与 `complete=false`（尚有人未提供）严格不同。

字段缺失 → `missing_field`；值为 `null` → `null_value`；实值不符合声明类型
（含十进制写成 JSON 数字、日期不是合法 ISO 日期）→ `type_mismatch`。三类均为
**未知值**，逐字段记录原因，**不中断文件**。十进制资料与常量一律以字符串承载，
比较使用 `Decimal`，全程不经过浮点（`3.0 >= 3`、`5.00 >= 5` 含边界成立）。

文件**结构**错误则不同：坏 JSON（`bad_json`）、重复 `application_id`、
`items` 非数组/成员非对象、`complete` 非布尔、`application_id` 缺失或为空等，
逐行推进到该行即非零退出，**标明行号**；已写结果保留但运行标记 `incomplete`，
**不写成功汇总**；后续行不再判定。

### 15.5 结果、解释与可追溯

* 结果按输入顺序写入（`seq`/`line_no` 保留行序），汇总三种结论数量，并记录
  条款摘要、文件摘要与输入摘要（SHA-256、字节数、行数）；
* 每份申请保存**完整解释树**：覆盖所有条件分支（不短路），每个节点带条件 ID/
  位置、三值结果、实际字段值与资料 JSON Pointer、成员计数的逐人结果与人数区间，
  并关联条款加载时已核对的公告引文（`body[start:end] == quote`）；
* 总体结论取条款声明的 `root` 条件；
* 结果库可重开，`show` 仍可从解释逐项核对（同一份结果只存一份条件展开，
  `ref` 节点用 `/condition_nodes/...` 指针引用，读回时展开）。

### 15.6 样本、演示与测试

`scenario/eligibility/` 全部为**合成资料**：`eligsample/` 是归档原文（E-9001
完整版 v1/v2、公众版同编号另一变体、E-9002），`clauses/` 是人工编录的条款与
三类错误引文样例（错位、串到公众版、把 v1 文本指向 v2），`applications/` 是
12 份申请（含缺失字段、完整空名单、未列全但足够、未知卡门槛、两人各一半、
精确十进制边界、明确反例等）。`python -m archive_pipeline.eligdemo_main`
构建 `work/eligdemo/` 并写出 `reports/eligibility.md`，演示条款检查、三类拒绝、
批量顺序结果、单份完整解释、计数四情形、三类坏文件与 2000 行生成文件的逐行处理。

`tests/test_eligibility.py`（42 项）独立列出预期结论与关键解释后与库中记录核对。

### 15.7 已知边界

* 只做辅助预审：结论、引文与解释可追溯，**最终由工作人员按条款核实决定，
  不代替人工审批**；
* 条件需人工编录为 JSON，程序**不从自然语言自动推断**资格条件；
* 一次运行只使用一份条款；条款换版需显式指定另一份文件；
* 申请文件按行处理，内存只保留当前申请与条款；结果库会随申请数增长
  （每份约 10–30 KB 解释），大批量时请预留磁盘；
* 作业为单进程批量工具，不做并发处理；归档库只读，不重写任何归档内容。

## 16. 原始留档空间回收（`reclaim.py` / `reclaimcli.py`）

采集管线把原始响应按内容寻址写入 `work/archive/<source>/<yyyymmdd>/<digest>.bin`
（大响应为 `.bin.gz`），旁边是同摘要的 `.json` 元数据。随着轮次增加，磁盘上会留下
**已落盘、但没有任何持久采集记录引用**的孤立响应。本作业只回收这一类孤立响应：
版本、扫描事实、扫描请求清单、分页清单，以及下游作业（资助事实、日程账本、资格结果）
引用的摘要都**不删、不改**；历史版本保留政策不变。

```bash
python -m archive_pipeline.reclaimcli preview --cutoff 2026-11-01T00:00:00+00:00
python -m archive_pipeline.reclaimcli plan    --cutoff 2026-11-01T00:00:00+00:00
python -m archive_pipeline.reclaimcli show    --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimcli isolate --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimcli restore --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimcli purge   --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimcli journal --plan rcl_xxxxxxxxxxxxxxxx
python -m archive_pipeline.reclaimdemo            # 合成现场 + 53 项自检 → reports/reclaim.md
```

退出码：`0` 成功；`2` 用法/前置条件错误；`3` 计划已过期（候选新增引用或文件被替换，
一个文件都不移动）；`4` 冲突（部分文件未能移动/撤销保留双方/清理被新引用拦住）；
`5` 引用来源不可读（未做任何改动）。

### 16.1 边界与引用判断（`reclaim.py`）

* **边界**：一个工作目录及其实际留档根。默认 `<workdir>/archive`（即扫描器实际写入
  的目录）；`--archive` 可显式指定，相对路径按工作目录解析，绝不按当前进程目录解析。
* **引用来源**（全部只读）：`pipeline.sqlite3` 的 `versions.raw_digest`、
  `scan_items.raw_digest`、`scan_items.detail_json`、`scans.requests_json`、
  `scans.pages_json`；工作目录下存在的 `funding.sqlite3`（`facts`、
  `version_errors`、`table_outcomes`）、`schedule.sqlite3`
  （`extraction_versions`）、`eligibility.sqlite3`；以及 `reports/requests/`、
  `requests/`、`manifests/` 下的请求/分页 JSON 清单。
* **一个摘要只要仍有引用，它在所有来源/日期目录下的全部副本都受保护。**
  某一份旁边的元数据看起来更旧不会改变结论。
* **读不到就拒绝执行。** 管线库缺失、数据库为空/无表/无任何可核对摘要列、
  JSON 清单损坏、下游库损坏，都会让 `preview`/`plan` 以退出码 5 终止——
  "查不到引用"只有在每个来源都可读时才允许当作结论。不做任何降级猜测。

### 16.2 文件系统规则

只认可现有留档布局的普通文件：

```
<archive-root>/<source>/<yyyymmdd>/<sha256>.bin
<archive-root>/<source>/<yyyymmdd>/<sha256>.bin.gz
<archive-root>/<source>/<yyyymmdd>/<sha256>.json
```

* `.bin.gz` 以 **gzip 解压后的原始字节**核对摘要；压缩包的摘要不作为响应摘要。
  元数据声明 `storage: gzip` 与磁盘形态必须一致。
* 完整、摘要相符、归属一致的组才可能成为候选。以下一律只列诊断，不进入候选：
  缺元数据或缺响应文件的不完整组、损坏 gzip、摘要不符、元数据 `digest`/`source_id`/
  `bytes`/`storage` 冲突、符号链接（含目录）、陌生路径与陌生顶层目录。
* 候选还必须早于使用者给定的宽限截止时间：文件系统 mtime **和** 元数据
  `fetched_at` 都早于 `--cutoff`。近期刚写入、尚未入库的文件不能因"当前查不到引用"
  被回收。宽限期内但不完整的组计入"宽限期"统计而不是"异常"。

### 16.3 预览与计划

`preview` 只读：不移动文件、不创建 `reclaim.sqlite3`，并报告
`file_moves: 0`、`freed_disk_bytes: 0`。`plan` 把同一份分类快照持久化到
`work/reclaim.sqlite3`，可用 `show`/`journal` 再次打开。计划逐组、逐文件记录：

* 组：来源、日期目录、响应摘要、判定（受保护/宽限期内/可隔离/异常）、原因、
  磁盘字节、响应字节、引用依据（库/表/列/行或清单路径）；
* 文件：相对路径、角色（blob/blob_gz/meta）、磁盘字节、文件内容 SHA-256、mtime；
* 汇总：`protected`、`within_grace`、`isolatable`、`anomaly` 四类分别统计组数与
  磁盘字节；`estimated_reclaimable_disk_bytes` 是**可隔离组的磁盘占用估算**，
  `freed_disk_bytes` 在永久清理前恒为 0——预计节省量从不冒充已释放空间。

### 16.4 隔离区与逐文件日志

隔离区固定在留档根内、由程序管理：`<archive-root>/.reclaim/quarantine/<plan_id>/…`
`--archive` 之外的任意路径不会被接受为删除目标；每个待移动文件的目标路径都由
计划内的相对路径确定性派生，命令行无法指定。

移动文件与写日志无法由一次 SQLite 事务包住，因此隔离是**先记动作、再改名、再逐文件
落状态**（`actions` 表 + `files.state`）。在移动响应文件之后、移动元数据之前被强制
结束，重开时 `journal` 仍能说明每个文件在哪里：

* `isolate` 重入先用 `action='move_in'`、`status='started'` 的动作行按文件系统实际
  位置核对——已在隔离区则记为完成，仍在原位则记为未开始，两者都不是则保留双方并
  标冲突；随后只处理仍为 `planned` 的文件，继续原计划；
* 已移动文件不会再次计入空间收益（`released_bytes` 只在永久清理时写入一次）；
* `restore` 遇到原路径出现**不同内容**时保留双方并报 `conflict`，绝不覆盖；内容相同
  则保留现有文件、删除隔离副本；
* `purge` 重试时按文件状态判断：已删除的文件报 `missing` 并把释放量补记一次，
  不会重复计释放量。

### 16.5 执行前复核与过期计划

计划与执行之间可能又跑过采集。`isolate` 在移动**第一份文件之前**用计划自己的
`--cutoff` 重新读取引用并重新分类整个留档：任一候选摘要新增引用、文件大小/内容/mtime
变化、文件消失或变成符号链接、组不再完整、或整组判定改变，都会让计划变为 `stale`
并以退出码 3 拒绝，**一个文件都不移动**，冲突逐条列出（摘要、路径、原因、依据）。
不会按旧名单强行清理。恢复中断的隔离同样先复核。

### 16.6 与正式采集入口的协作（`worklock.py`）

扫描器在提交扫描项/版本之前就把原始字节写入留档，因此"数据库此刻查不到引用"并不
等于孤立。宽限期无法覆盖这个窗口，本功能也不靠加大宽限期规避：

* `ScanEngine.scan()` 在**整个扫描期间**持有工作目录的**共享**锁
  （`<workdir>/pipeline.lock` 上的 `flock`）；锁在正式入口内部获取，调用方无法跳过；
* `preview`/`plan`/`isolate`/`restore`/`purge` 持有同一把锁的**独占**模式；
* 同一工作目录的采集与隔离因此串行化，不同工作目录互不阻塞；不要求并行吞吐。

`tests/test_reclaim.py::RealIngestWindowTests` 用两个真实进程证明：采集进程在写入
原始字节、尚未提交事实时持有共享锁，回收进程在独占锁上等待；采集提交后回收才继续，
此时该摘要已被引用而受保护。

### 16.7 合成现场、演示与测试

`reclaimsample.py` 在临时/`work` 目录生成合成留档：版本引用对象、仅扫描项引用的
响应、同摘要跨日期副本、过期孤立对象、新近孤立对象、可隔离 gzip、缺元数据组、
损坏 gzip、摘要不符、元数据来源/大小冲突、符号链接与陌生路径。
`python -m archive_pipeline.reclaimdemo` 依次演示并自检：预览不改文件、计划可重开、
隔离后可撤销、永久清理的**真实磁盘增量**、保存计划后新增引用被拒、采集写盘未提交时
回收进程等待、隔离中途被杀后继续/撤销、撤销遇同名不同内容保留双方、清理中断后重试
不重复计量，隔离后新增引用拦住清理（53 项核对，报告写入 `reports/reclaim.md`）。

`tests/test_reclaim.py`（63 项）独立构造引用库与文件事实，断言引用判断、gzip 解压后
摘要、宽限、四类统计、计划可重开、逐文件日志、过期计划零移动、中断恢复、撤销冲突、
清理幂等、CLI 退出码、真实两进程采集窗口与旧 CLI/版本查询仍可用；所有断言读取真实
字节、目录与数据库记录。

### 16.8 已知边界

* 只回收"文件已落盘且无任何持久引用"的原始响应；不导出备份、不淘汰历史版本、
  不生成分析报表，也不修改 `versions`/`scan_items`/`scans`。
* 引用来源是固定清单（见 §16.1）；工作目录外部的数据库或清单不会自动纳入，需要时
  把它复制到工作目录的对应位置。
* 时间戳解析要求带时区偏移的 ISO-8601；`--cutoff` 必须显式给出。
* 同一工作目录的采集与回收串行；不提供并行回收，也不跨工作目录协调。

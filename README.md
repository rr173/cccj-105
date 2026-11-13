# 公共研究通告归档管线（Public Notice Archive Pipeline）

一条仅依赖 Python 3.11 标准库的抓取—解析—归档管线，配套**两个本地站点模拟器**
（`sim-a` / `sim-b`），在真实 HTTP 上演示全部要求的行为。**不访问任何外部网站。**

```bash
# 完整演示：启动两站、跑完整场景、生成 reports/
python -m archive_pipeline.demo

# 测试（100 项：21 单元 + 34 真实 HTTP 集成/身份回归 + 7 旧库修复 + 38 资助表格）
python -m unittest discover -s tests -v

# 旧版混合库修复（先预览、后显式应用）
python -m archive_pipeline.cli repair --preview          # 只读，不改库
python -m archive_pipeline.cli repair --apply            # 单事务原子迁移

# 资助表格结构化作业（独立于网页扫描，见 §13）
python -m archive_pipeline.funddemo                                  # 样本演示
python -m archive_pipeline.fundcli process                           # 处理归档版本
python -m archive_pipeline.fundcli rejections --history              # 拒收原因与历史
python -m archive_pipeline.fundcli facts --archive-id arc_xxxx       # 查询事实

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
config/sources.json   config/funding_tables.json   scenario/   scenario/funding/
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

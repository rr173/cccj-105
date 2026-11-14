# XLIFF 1.2 审译接入演示

样本工作目录：`/workspace/work/xliffdemo`（来源 `xliffsim`，4 个归档版本：X-4001 完整版与公众版、X-4002 补充通告 v1/v2）

正文区间按 Python 字符串下标左闭右开解释。例如 X-4001 第一段 `body[31:97]` 是 '一、申报时间为2026年6月1日至2026年7月15日，逾期不再受理。项目代号为〔KX-2026-01〕，请务必在申报表中保持一致。'（66 个字符、152 个 UTF-8 字节），本作业只接受字符串下标，不接受字节位置。

## 1. 干净交付：中文多字节片段 + 保护代码 + 占位符

```bash
python -m archive_pipeline.xliffcli --workdir work/xliffdemo intake \
    scenario/xliff/reviews/01-full-enus.xlf \
    --bindings scenario/xliff/mappings/01-full-enus.json
```

* run 1 → `completed`（退出码 0），单元 2：候选 2、草稿 0、拒收 0；新增候选 2，冲突组 0

| 单元 | 区间 | 源语言→目标语言 | 状态 | 候选 |
| --- | --- | --- | --- | --- |
| u-01 | [31,97) | zh-cn→en-us | candidate | 1 |
| u-head | [0,29) | zh-cn→en-us | candidate | 2 |

读回的语料行（`usable`）：

* 原文 `body[31:97]` = '一、申报时间为2026年6月1日至2026年7月15日，逾期不再受理。项目代号为〔KX-2026-01〕，请务必在申报表中保持一致。'
* 保护代码树（源）：`[{"kind":"g","id":"1","equiv_text":null,"children":[]},{"kind":"x","id":"2","equiv_text":"〔KX-2026-01〕","children":[]}]`
* 审定译文：`(1) Applications are accepted from June 1, 2026 to July 15, 2026; late submissions are not accepted. The project code is 〔KX-2026-01〕, and must be kept identical in the application form.`
* 目标代码树：`[{"kind":"g","id":"1","equiv_text":null,"children":[]},{"kind":"x","id":"2","equiv_text":"〔KX-2026-01〕","children":[]}]`
* 溯源：delivery 1，文件 `/workspace/scenario/xliff/reviews/01-full-enus.xlf`，SHA-256 `550ee61e10d55e18…`

## 2. 嵌套 group、兄弟代码换序、两种英语标签

* run 2 → `completed`（退出码 0），单元 4：候选 4、草稿 0、拒收 0；新增候选 4，冲突组 0
* `u-nest-1` 的 group 路径：`["outer", "inner"]`（外层 outer，内层 inner；id 判重范围仍是整个 file）
* `u-swap` 源代码顺序 `['p1', 'p2']`，目标顺序 `['p2', 'p1']` — 同一父节点下的兄弟代码换序，接受
* 同为 `body[31:97)`：en-US 组 1 个、en-GB 组 1 个，互不合并（语言标签存储统一小写；全库 en-US 2 组、en-GB 4 组）

## 3. 一份文件里可运与被拒并存（逐单元说明）

```bash
python -m archive_pipeline.xliffcli --workdir work/xliffdemo intake \
    scenario/xliff/reviews/03-mixed-rejections.xlf \
    --bindings scenario/xliff/mappings/03-mixed-rejections.json
```

* run 3 → `partial`（退出码 4），单元 14：候选 4、草稿 2、拒收 8；新增候选 4，冲突组 0；非零退出码 4 指明存在错误，其余单元仍接入

| 单元 | 原文件 | 类别 | 结果 | 代码 | 位置 |
| --- | --- | --- | --- | --- | --- |
| u-ok | `notices/X-4001-application.html` | - | candidate | — | file#5 line 5 source@6 target@7 |
| u-bad-id | `notices/X-4001-application.html` | - | rejected | target_missing_code | file#5 line 9 source@10 target@11 |
| u-bad-kind | `notices/X-4001-application.html` | - | rejected | target_code_type_changed | file#5 line 13 source@14 target@15 |
| u-bad-level | `notices/X-4001-application.html` | - | rejected | target_code_hierarchy_changed | file#5 line 17 source@18 target@19 |
| u-bad-equiv | `notices/X-4001-application.html` | - | rejected | target_equiv_changed | file#5 line 21 source@22 target@23 |
| u-draft | `notices/X-4001-application.html` | - | draft | draft_not_approved | file#5 line 25 source@26 target@27 |
| u-approved-draft | `notices/X-4001-application.html` | - | draft | draft_not_approved | file#5 line 29 source@30 target@31 |
| u-unsupported | `notices/X-4001-application.html` | - | rejected | unsupported_inline | file#5 line 33 source@34 target@35 |
| u-missing-target | `notices/X-4001-application.html` | - | rejected | target_count | file#5 line 37 source@None target@None |
| u-public-ok | `notices/X-4001-application.html#public` | - | candidate | — | file#6 line 44 source@45 target@46 |
| u-public-misbound | `notices/X-4001-application.html#public` | - | rejected | binding_mismatch | file#6 line 48 source@49 target@50 |
| u-version | `notices/X-4002-supplement.html#v1` | - | rejected | binding_mismatch | file#7 line 56 source@57 target@58 |
| u-window | `notices/X-4002-supplement.html#v1` | - | candidate | — | file#7 line 60 source@61 target@62 |
| u-prefix | `notices/X-4002-supplement.html#v1` | - | candidate | — | file#7 line 64 source@65 target@66 |

拒收原因（逐条取自数据库）：
* `u-bad-id` — target_missing_code: target lacks code ids ['2']; target introduces code ids ['9']
* `u-bad-kind` — target_code_type_changed: code '1' is a g in the source but a x in the target
* `u-bad-level` — target_code_hierarchy_changed: code parent/child nesting or types differ (sibling order may change, a code may not change its direct code parent)
* `u-bad-equiv` — target_equiv_changed: x 'w2' equiv-text differs: source '〔010-8888-6666〕', target ['〔010-0000-0000〕']
* `u-draft` — draft_not_approved: well-formed but not an approved final translation: approved='no' (needs 'yes'); kept as draft, never queryable
* `u-approved-draft` — draft_not_approved: well-formed but not an approved final translation: target state='translated' (needs 'final'); kept as draft, never queryable
* `u-unsupported` — unsupported_inline: unsupported inline element '{urn:oasis:names:tc:xliff:document:1.2}ph'; only plain text, paired g and empty x are accepted
* `u-missing-target` — target_count: trans-unit has 0 target elements; exactly one is required (the source is never used as a translation)
* `u-public-misbound` — binding_mismatch: source expansion does not equal body[99:159] of xliffsim/arc_88e49bc08495d34458e9370634834de8/v1: expected '三、每个申报单位最多申报两个项目；超过限额的申报材料不予受理，且不另行通知。\n\n五、本通告未尽事宜，由科研管理处负责解释', got '二、申报单位须具备独立法人资格，并提供近三年的科研项目清单。清单中的项目代号须与申报书封面完全相同，不得自行简写或改写。' — the span matches a different business variant arc_df6fcccf27a8993dc8ffc23c1e60c5d2 v1 — do not borrow the other variant's body
* `u-version` — binding_mismatch: source expansion does not equal body[39:98] of xliffsim/arc_cc1aabf730dec3f8c59bf8f0c67dc8e6/v1: expected '申报截止时间延长至2026年8月15日，项目代号〔KX-2026-01〕不变。\n\n二、原通告第三条关于申报限额的规定继', got '一、申报截止时间再次延长至2026年9月15日，项目代号〔KX-2026-01〕不变。已按前次通告准备的材料继续有效。' — the span matches version 2 of the same identity — a new version is never substituted automatically

所有被拒单元仍保留源/译文本与两棵代码树供人工核对，例如 `u-bad-level`：

* 源树 `[{"kind":"g","id":"g1","equiv_text":null,"children":[{"kind":"x","id":"w1","equiv_text":"〔受理窗口〕","children":[]},{"kind":"x","id":"w2","equiv_text":"〔010-8888-6666〕","children":[]}]}]`
* 译树 `[{"kind":"x","id":"w1","equiv_text":"〔受理窗口〕","children":[]},{"kind":"g","id":"g1","equiv_text":null,"children":[{"kind":"x","id":"w2","equiv_text":"〔010-8888-6666〕","children":[]}]}]`
* 译文 `(4) Submission location and contact: see 〔受理窗口〕 section; the enquiry number is 〔010-8888-6666〕; office hours follow the published schedule.`

## 4. 末尾重复单元 id：同 file 内全部失效

* run 4 → `partial`（退出码 4），单元 5：候选 3、草稿 0、拒收 2；新增候选 2，冲突组 0

* file `notices/X-4001-application.html`：u-dup 共 2 次出现 → rejected(duplicate_unit_id), rejected(duplicate_unit_id)
* file `notices/X-4001-application.html#dup`：u-dup 共 1 次出现 → candidate(−)
* 第一个 file 里先读到的那份也一并失效；另一个 file 中同名 id 合法，仍可作为候选

## 5. 坏 XML 不改库；DTD/外部实体拒绝

* 语法错误：`malformed_xml` — XML syntax error: mismatched tag: line 9, column 10
* 接入前后 `xliff.sqlite3` 摘要：`ff86e4f53959832e…` → `ff86e4f53959832e…`（**未改动**）
* DTD/外部实体：`doctype_refused` — DTDs are not accepted (internal or external); no entity may be declared or loaded
* 接入后摘要仍为 `ff86e4f53959832e…`；不联网、不加载任何外部资源

## 6. 相同文件重传 / 不同包装相同译文

* 原样重传：`duplicate`（与 delivery 1 相同），候选数 12→12、交付数 4→4
* 换命名空间前缀、重新分包的同一译文：新增候选 0，新增交付来源 2
  * 候选 8 的来源交付：[3]（['03-mixed-rejections.xlf']）
  * 候选 3 的来源交付：[2]（['02-nested-en-gb.xlf']）
  * 候选 1 的来源交付：[1, 5]（['01-full-enus.xlf', '05-prefixed-alias.xlf']）

## 7. 先唯一、后冲突

* 冲突前：available，唯一可用候选 1
* 冲突译文接入：run 7 → `completed`（退出码 0），单元 1：候选 1、草稿 0、拒收 0；新增候选 1，冲突组 1
* 冲突后：`conflict`，候选数 2，可用候选 None（不再冒充唯一答案）
* 原因：2 distinct approved translations for the same notice version, span and language pair; awaiting human review — none is offered as the unique usable answer
* 候选 1：(1) Applications are accepted from June 1, 2026 to July 15, 2026; late submissions are not accepted. The project code is 〔KX-2026-01〕, and must be kept identical in the application form.
  * 来源：delivery 1 (01-full-enus.xlf #u-01)；delivery 5 (05-prefixed-alias.xlf #u-01)
* 候选 13：(1) The application period runs from June 1, 2026 through July 15, 2026. Submissions after the deadline are not accepted. Project code 〔KX-2026-01〕 must appear consistently in the application form.
  * 来源：delivery 6 (06-conflict-enus.xlf #u-01-conflict)
* 可直接使用且尚未冲突的组：11 个；每组恰有一个候选
* 待核对（冲突）组：1 个；草稿不参与冲突判断

## 8. 提交前强制退出：旧状态原样保留，重跑成功

* 受控中断：controlled interruption before commit
* 组状态记录 12→12，候选 13→13，交付 6→6（候选与可用性同事务提交，不会出现“候选已加、旧译文仍唯一可用”）
* 该次运行在库中标记为 `interrupted`，未产生交付
* 重跑同一份文件（正常提交）：run 9 → `partial`（退出码 4），单元 1：候选 0、草稿 0、拒收 1；新增候选 0，冲突组 0
* 重跑结果确实落库：run 9 `partial`，delivery 7 单元 `u-wrong-version` rejected（binding_mismatch）——这份文件本身绑定了错误的版本，规范问题照实记录，与上次中断无关
* 中断前后该冲突组状态保持 `conflict`（候选 2 份，无唯一可用候选）

## 9. 读取命令对照

```bash
python -m archive_pipeline.xliffcli --workdir work/xliffdemo usable
python -m archive_pipeline.xliffcli --workdir work/xliffdemo usable --language en-GB
python -m archive_pipeline.xliffcli --workdir work/xliffdemo groups --state conflict
python -m archive_pipeline.xliffcli --workdir work/xliffdemo candidates --archive-id arc_df6fcccf27a8993dc8ffc23c1e60c5d2
python -m archive_pipeline.xliffcli --workdir work/xliffdemo issues --unit-status rejected
python -m archive_pipeline.xliffcli --workdir work/xliffdemo units --unit-id u-bad-level
python -m archive_pipeline.xliffcli --workdir work/xliffdemo deliveries
python -m archive_pipeline.xliffcli --workdir work/xliffdemo runs
python -m archive_pipeline.xliffcli --workdir work/xliffdemo usable --json
```

当前可用语料（本次演示结束时的库内状态）：

* `xliffsim` `arc_88e49bc0…` v1 [31,97) zh-cn→en-us：(1) Applications are accepted from June 1, 2026 to July 15, 2026; late submissions are not accepted. The project code is KX-2026-01, and must be kept identical in the application form.
* `xliffsim` `arc_cc1aabf7…` v1 [37,58) zh-cn→en-us：1. The application deadline is extended to August 15, 2026
* `xliffsim` `arc_cc1aabf7…` v1 [37,78) zh-cn→en-us：1. The application deadline is extended to August 15, 2026; project code KX-2026-01 is unchanged.
* `xliffsim` `arc_df6fcccf…` v1 [0,29) zh-cn→en-gb：To all relevant units: the 2026 research cooperation project application arrangements are set out below.
* `xliffsim` `arc_df6fcccf…` v1 [0,29) zh-cn→en-us：To all relevant units: the 2026 research cooperation project application arrangements are announced below.
* `xliffsim` `arc_df6fcccf…` v1 [31,97) zh-cn→en-gb：(1) Applications are accepted from June 1, 2026 to July 15, 2026; late submissions are not accepted. The project code is 〔KX-2026-01〕, and must be kept identical in the application form.
* `xliffsim` `arc_df6fcccf…` v1 [99,159) zh-cn→en-gb：(2) Applicants must be independently registered legal entities and must provide a list of research projects from the past three years. Codes in the list must match the cover of the application exactly; abbreviations or rewrites are not permitted.
* `xliffsim` `arc_df6fcccf…` v1 [99,159) zh-cn→en-us：(2) Applicants must be independently registered legal entities and must provide a list of research projects from the past three years. Codes in the list must match the cover of the application exactly; abbreviations or rewrites are not permitted.
* `xliffsim` `arc_df6fcccf…` v1 [161,199) zh-cn→en-gb：Applications exceeding the limit will not be accepted, without further notice.(3) Each applicant may submit at most two projects; 
* `xliffsim` `arc_df6fcccf…` v1 [161,199) zh-cn→en-us：Applications exceeding the limit will not be accepted, without further notice.(3) Each applicant may submit at most two projects; 
* `xliffsim` `arc_df6fcccf…` v1 [201,260) zh-cn→en-us：(4) Submission location and contact: see section 〔受理窗口〕; the enquiry number is 〔010-8888-6666〕; office hours follow the published schedule.

支持边界：

* 只接收 XLIFF 1.2（根命名空间 `urn:oasis:names:tc:xliff:document:1.2`）；内联只支持普通文字、成对 `g`、带 `equiv-text` 的空元素 `x`；
* `ph`/`bpt`/`ept`/`it`/`mrk`/`sub` 等明确报 `unsupported_inline`，不剥掉后继续入账；
* 拒绝 DTD 与外部实体；不联网、不调用翻译服务、不把译文写回公告正文；
* 审定候选 = `approved="yes"` 且目标 `state="final"`；其余为草稿，永不进入可用查询；
* 不做人工裁决：冲突组只标记为待核对并保留两份候选与来源；
* 网页采集与版本查询（`cli scan/versions/...`）不受影响，本作业只读管线库与归档正文。

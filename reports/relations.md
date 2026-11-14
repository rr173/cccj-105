# 离线关系规则作业演示（机构关联演练）

样本工作目录：`/workspace/work/reldemo`（来源 `relsim`，5 个归档版本；规则 `scenario/relations/rules/org.json`）

事实与结果都写入 `relation.sqlite3`；归档库 `pipeline.sqlite3` 只读，`notice` 关系一次运行内来自同一个读取视图。

## 1. 先检查规则并查看计划（不读取任何事实）

```bash
python -m archive_pipeline.relcli check \
    --rules scenario/relations/rules/org.json --plan
```

```
依赖边方向：head depends_on body（箭头由规则头指向正/负体原子）

分层与递归分量：
  层 0: [ancestor]  递归分量 #0（半朴素迭代）
  层 1: [coop_reach]  递归分量 #1（半朴素迭代）
  层 2: [reach_even, reach_odd]  递归分量 #2（半朴素迭代）
  层 3: [notice_org]  非递归（依赖已完成）
  层 4: [notice_allowed]  非递归（依赖已完成）
  层 5: [ancestor_excluded]  非递归（依赖已完成）
  层 6: [notice_visible]  非递归（依赖已完成）
  层 7: [org_self]  非递归（依赖已完成）

强连通分量（求值顺序）：
  #0: ['ancestor']  [单点]
  #1: ['coop_reach']  [单点]
  #2: ['reach_odd', 'reach_even']  [递归]
  #3: ['notice_org']  [单点]
  #4: ['notice_allowed']  [单点]
  #5: ['ancestor_excluded']  [单点]
  #6: ['notice_visible']  [单点]
  #7: ['org_self']  [单点]

规则依赖边：
  ancestor -> parent_org (rule 0)
  ancestor -> ancestor (rule 1)
  ancestor -> ancestor (rule 1)
  coop_reach -> coop (rule 2)
  coop_reach -> coop_reach (rule 3)
  coop_reach -> coop_reach (rule 3)
  coop_reach -> coop (rule 4)
  reach_even -> org (rule 5)
  reach_odd -> reach_even (rule 6)
  reach_odd -> coop (rule 6)
  reach_even -> reach_odd (rule 7)
  reach_even -> coop (rule 7)
  org_self -> parent_org (rule 8)
  notice_org -> notice (rule 9)
  notice_org -> mention (rule 9)
  notice_allowed -> notice_org (rule 10)
  notice_allowed -not-> excluded (rule 10)
  notice_allowed -not-> blocked_notice (rule 10)
  ancestor_excluded -> ancestor (rule 11)
  ancestor_excluded -> excluded (rule 11)
  notice_visible -> notice_allowed (rule 12)
  notice_visible -not-> ancestor_excluded (rule 12)
```

依赖边全部写成“规则头 → 体原子”；否定边同样由头指向被否定的关系，并标注 `-not->`。同一强连通分量内出现否定边会在阶段 2 被拒绝。

## 2. 执行转换并读回演练关系

```bash
python -m archive_pipeline.relcli run --rules scenario/relations/rules/org.json
python -m archive_pipeline.relcli --workdir work/reldemo show \
    --relation ancestor --page 0 --page-size 50
```

运行 1 完成：总元组 106，其中推导新增 65，预算 5000；notice 行 5，运行前后归档文件摘要一致：True。

| 关系 | 类型 | 元组 |
| --- | --- | --- |
| `blocked_notice` | base | 0 |
| `coop` | base | 5 |
| `excluded` | base | 1 |
| `mention` | base | 7 |
| `notice` | notice | 5 |
| `org` | base | 12 |
| `parent_org` | base | 11 |
| `ancestor` | derived | 18 |
| `coop_reach` | derived | 25 |
| `reach_even` | derived | 1 |
| `reach_odd` | derived | 1 |
| `notice_org` | derived | 7 |
| `notice_allowed` | derived | 6 |
| `ancestor_excluded` | derived | 1 |
| `notice_visible` | derived | 5 |
| `org_self` | derived | 1 |

notice 关系只从归档库读 `source_id, archive_id, version_no, title, body`，包含全部历史版本；公告编号只作为正文/标题内容保留，不参与身份：

* `relsim` / `arc_25a6d2f01627d245df9ba57bf2e5831e` / v1 — 关于联合科研平台开放申请的通告
* `relsim` / `arc_25a6d2f01627d245df9ba57bf2e5831e` / v2 — 关于联合科研平台开放申请的通告（修订）
* `relsim` / `arc_309df132b62995e8a86c08f47f2c3228` / v1 — 关于设备维护窗口的通告
* `relsim` / `arc_6af784ed31085359fffe393e61bd4579` / v1 — 关于联合科研平台开放申请的公众通告
* `relsim` / `arc_ed728d5bb344f8aa476bef08729add98` / v1 — 关于开展联合数据评估的通知

同一编号 N-5001 有两个归档身份（完整版 `...5831e` 公众版 `...4579`），完整版的 v1 与 v2 也各自是独立版本行；mentions 精确绑定到版本。

### 2.1 与手写预期元组逐集合比对

| 关系 | 与手写预期一致 | 多出 | 缺少 |
| --- | --- | --- | --- |
| `ancestor` | 是 | [] | [] |
| `coop_reach` | 是 | [] | [] |
| `notice_org` | 是 | [] | [] |
| `notice_allowed` | 是 | [] | [] |
| `notice_visible` | 是 | [] | [] |
| `reach_even` | 是 | [] | [] |
| `reach_odd` | 是 | [] | [] |
| `ancestor_excluded` | 是 | [] | [] |
| `org_self` | 是 | [] | [] |

覆盖点：正向自递归（祖先、合作）、互递归（`reach_even`/`reach_odd`）、同一递归关系在正文出现两次（祖先第二条规则，以及合作传递闭包）、菱形路径去重（A1→B1→C 与 A1→P→A1→… 都只保留集合元组）、显式重复变量自连接（`org_self`：同一变量占 `parent_org` 两列）、空关系与常量（`blocked_notice` 为空、`reach_even` 以常量 A1 起步）、低层递归收敛后才做否定（`ancestor` 与 `excluded` 的层号关系）、不同公告版本不串用。

### 2.1b 同一演练现场与独立穷举求值器交叉核对

* `ancestor`：生产 18 元组，穷举 18 元组，一致
* `coop_reach`：生产 25 元组，穷举 25 元组，一致
* `reach_even`：生产 1 元组，穷举 1 元组，一致
* `reach_odd`：生产 1 元组，穷举 1 元组，一致
* `notice_org`：生产 7 元组，穷举 7 元组，一致
* `notice_allowed`：生产 6 元组，穷举 6 元组，一致
* `notice_visible`：生产 5 元组，穷举 5 元组，一致
* `ancestor_excluded`：生产 1 元组，穷举 1 元组，一致
* `org_self`：生产 1 元组，穷举 1 元组，一致

### 2.2 打乱事实与规则顺序后结果集合不变

打乱后各行派生关系与原顺序逐集合比较：

* `ancestor`：一致
* `coop_reach`：一致
* `notice_org`：一致
* `notice_allowed`：一致
* `notice_visible`：一致
* `org_self`：一致
* 打乱后的运行状态：`completed`；notice 快照一致：True

### 2.3 独立小型穷举求值器交叉核对

`reloracle.py` 为测试用参考实现：内存列表、每个分量反复全量重跑直到不再新增（brute-force 不动点），不共享生产代码。

| 随机资料 | 关系 | 生产元组 | 穷举元组 | 一致 |
| --- | --- | --- | --- | --- |
| seed 1 | `reach` | 5 | 5 | 是 |
| seed 1 | `reach_safe` | 2 | 2 | 是 |
| seed 1 | `same` | 1 | 1 | 是 |
| seed 2 | `reach` | 2 | 2 | 是 |
| seed 2 | `reach_safe` | 2 | 2 | 是 |
| seed 2 | `same` | 0 | 0 | 是 |
| seed 3 | `reach` | 12 | 12 | 是 |
| seed 3 | `reach_safe` | 8 | 8 | 是 |
| seed 3 | `same` | 1 | 1 | 是 |
| seed 4 | `reach` | 4 | 4 | 是 |
| seed 4 | `reach_safe` | 1 | 1 | 是 |
| seed 4 | `same` | 1 | 1 | 是 |
| seed 5 | `reach` | 8 | 8 | 是 |
| seed 5 | `reach_safe` | 5 | 5 | 是 |
| seed 5 | `same` | 0 | 0 | 是 |

全部一致：是

### 2.4 规则与坏事实的定位拒绝

| 样本 | 拒绝码 | 位置 | 是否读取事实 |
| --- | --- | --- | --- |
| 否定闭环（当前输入为空也拒绝） | `negative_cycle` | `rules[1]` | 否（check 阶段） |
| 无正关系绑定的变量 | `unsafe_variable` | `rules[0].body[1]` | 否（check 阶段） |
| 混合类型：字符串与整数比较 | `comparison_type_mismatch` | `rules[0].body[1]` | 否（check 阶段） |
| 基础关系作为规则头 | `base_relation_as_head` | `rules[0]` | 否（check 阶段） |
| 未声明关系 | `unknown_relation` | `rules[0].body[0]` | 否（check 阶段） |
| 元数不符 | `arity_mismatch` | `rules[0].body[0]` | 否（check 阶段） |
| 未定义的推导关系 | `undefined_derived_relation` | `$.relations['b']` | 否（check 阶段） |
| 裸字符串（不能靠长相猜常量） | `bare_string_term` | `rules[0].body[0].args[1]` | 否（check 阶段） |

坏事实按文件与行号拒绝；前半段好事实、后半段坏事实时**整次回滚**，不会把部分输入当作结果：

| 样本 | 结果 |
| --- | --- |
| 缺列 | `failed` 4 `missing_column` 行 2 |
| 多列 | `failed` 4 `extra_column` 行 2 |
| null | `failed` 4 `null_value` 行 2 |
| 类型不符（布尔冒充整数） | `failed` 4 `type_mismatch` 行 2 |

规则里的布尔常量不能被当作整数：

* `bool_not_int` at `rules[0].head.args[0]` — rules[0].head.args[0]: booleans are not integers and cannot be used as a typed constant

### 2.5 预算超限与真实中途退出

先用当前成功运行为基准：

* 旧结果运行号 1，`ancestor` 元组 18，`notice` 行 5
* 关系行指纹 SHA-256（排序后 JSON）：`9f2539cd3fa655e1156d53eac05389685f3705dfc2b618e48629f3644c621b6b`

超预算：预算 10，事实本身已超，运行明确失败且不发布：

* 状态 `failed`，退出码 4，错误 `budget`：deriving rule 0 for ancestor: derived tuple count reached 11, over the budget of 10
* 运行列表：[(1, 'completed')]

真实中途退出（`--hard-exit` 在打开的事务里 `os._exit(9)`）：

* 子进程退出码 9（无输出、无提交）
* 重开数据库后的运行列表：[(1, 'completed')]
* 最新成功运行仍是 1
* 旧运行关系指纹仍为 `9f2539cd3fa655e1156d53eac05389685f3705dfc2b618e48629f3644c621b6b`
* 读回 `notice_visible` 仍有 5 行

随后正常再执行并分页读取每个关系：

* 新运行 2 `completed`
* `org`：共 12 行，首 3 行 [{"org_id": "A", "name": "甲研究所"}, {"org_id": "A1", "name": "甲一所"}, {"org_id": "A2", "name": "甲二所"}]
* `parent_org`：共 11 行，首 3 行 [{"child": "A", "parent": "R"}, {"child": "A1", "parent": "A"}, {"child": "A2", "parent": "A"}]
* `coop`：共 5 行，首 3 行 [{"org_a": "A1", "org_b": "B1"}, {"org_a": "B1", "org_b": "A1"}, {"org_a": "B1", "org_b": "C"}]
* `excluded`：共 1 行，首 3 行 [{"org_id": "X"}]
* `mention`：共 7 行，首 3 行 [{"archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "org_id": "A1"}, {"archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "org_id": "C"}, {"archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 2, "org_id": "B1"}]
* `notice`：共 5 行，首 3 行 [{"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "title": "关于联合科研平台开放申请的通告", "body": "各研究机构：联合科研平台自2026年3月起开放申请。\n\n一、首批开放两个实验方向，申请以机构为单位提交。\n\n二、合作机构名单由科研管理处另行确认。"}, {"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 2, "title": "关于联合科研平台开放申请的通告（修订）", "body": "各研究机构：联合科研平台自2026年3月起开放申请，现作如下修订。\n\n一、首批开放三个实验方向，申请以机构为单位提交。\n\n二、合作机构名单由科研管理处另行确认；新增方向自公布之日起接受申请。"}, {"source_id": "relsim", "archive_id": "arc_309df132b62995e8a86c08f47f2c3228", "version_no": 1, "title": "关于设备维护窗口的通告", "body": "各用户：设备将于本月第二个周末维护，期间暂停服务。"}]
* `ancestor`：共 18 行，首 3 行 [{"org_id": "A", "ancestor": "R"}, {"org_id": "A1", "ancestor": "A"}, {"org_id": "A1", "ancestor": "R"}]
* `coop_reach`：共 25 行，首 3 行 [{"org_a": "A1", "org_b": "A1"}, {"org_a": "A1", "org_b": "A2"}, {"org_a": "A1", "org_b": "B1"}]
* `reach_even`：共 1 行，首 3 行 [{"org_id": "A1"}]
* `reach_odd`：共 1 行，首 3 行 [{"org_id": "B1"}]
* `org_self`：共 1 行，首 3 行 [{"org_id": "S"}]
* `notice_org`：共 7 行，首 3 行 [{"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "org_id": "A1"}, {"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "org_id": "C"}, {"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 2, "org_id": "B1"}]
* `notice_allowed`：共 6 行，首 3 行 [{"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "org_id": "A1"}, {"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "org_id": "C"}, {"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 2, "org_id": "B1"}]
* `ancestor_excluded`：共 1 行，首 3 行 [{"org_id": "D"}]
* `notice_visible`：共 5 行，首 3 行 [{"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "org_id": "A1"}, {"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 1, "org_id": "C"}, {"source_id": "relsim", "archive_id": "arc_25a6d2f01627d245df9ba57bf2e5831e", "version_no": 2, "org_id": "B1"}]
* `blocked_notice`：共 0 行，首 3 行 []

## 3. 较长有向链 + 少量回边：磁盘中间表、每轮增量与内存

构造 120 个节点的有向链，另加 12 条跨节点回边（随机取端点），规则为传递闭包 `reach`：

```bash
python -m archive_pipeline.relcli --workdir <tmp> run \
    --rules <tmp>/rules/chain.json --progress
```

| 轮次 | 本轮新增元组 | 增量连接 | 全量连接 |
| --- | --- | --- | --- |
| 0 | 130 | 0 | 1 |
| 1 | 140 | 2 | 2 |
| 2 | 310 | 2 | 2 |
| 3 | 773 | 2 | 2 |
| 4 | 1918 | 2 | 2 |
| 5 | 3793 | 2 | 2 |
| 6 | 3234 | 2 | 2 |
| 7 | 1120 | 2 | 2 |
| 8 | 0 | 2 | 2 |

* 传递闭包元组总数 11418（120 个节点；链本身的纯可达对为 7140，回边把大量节点连成环，自反与额外可达对因此明显增多）
* 结果库中 `facts` 表占用（页面计数 × 页大小）：1544192 字节；`rule_relations`、`round_stats` 等元数据 1609728 字节
* 本次 Python 进程的 tracemalloc 峰值：1194973 字节（中间集合不积累为全库容器）
* 中间关系全程保存在结果库 `facts` 表（磁盘）；每轮的`rel_<name>_delta` 是 SQLite 临时表（`temp_store=FILE`），全量侧是按 rowid 水位冻结的临时视图，本轮新元组对其不可见，求值结束即删除
* 实际每轮增量连接见 `round_stats.join_plan_json`："[{\"rule_index\": 1, \"position\": 0, \"relation\": \"reach\", \"alias\": \"t0\", \"mode\": \"delta\", \"negated\": false}, {\"rule_index\": 1, \"position\": 1, \"relation\": \"reach\", \"alias\": \"t1\", \"mode\": \"full\", \"negated\": false}, {\"rule_index\": 1, \"position\": 0, \"relation\": \"reach\", \"alias\": \"t0\", \"mode\": \"full\", \"negated\": false}, {\"rule_index\": 1, \"position\": 1, \"relation\": \"reach\", \"alias\": \"t1\", \"mode\": \"delta\", \"negated\": false}]"

## 4. 支持范围与统计含义

* 类型只有 `string` 与有符号 64 位 `int`；布尔不是整数，字符串按原值比较（不折叠大小写、不改空白）。
* `null`、缺列、多列与类型不符逐行拒绝并定位（文件:行:列）。
* 事实按集合语义：重复行只算一条（`INSERT OR IGNORE` + 主键）。
* 正递归与相互递归按最小不动点求值；否定采用分层否定，同分量负边直接拒绝。
* `delta_rows` 是**该关系该轮新插入的唯一元组数**；`variants` 是该轮执行的增量变体数；`joins_delta` / `joins_full` 统计体原子分别读增量表/全量快照的次数。
* `total_tuples` 是本次存储的元组总数（基础事实 + 全部推导关系）；`derived_tuples` 只是推导新增部分；预算对 `total_tuples` 生效。
* 这些计数只用于审计求值路径，正确性以与手写预期/穷举求值器的集合比对为准，耗时与“成功条数”不代替核对。

已知边界：不提供事实删除后的在线维护、聚合、函数生成新值与无限数值递归；规则与事实均为离线批量输入；作业为单进程。


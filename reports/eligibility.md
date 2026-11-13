# 离线资格预审作业演示

样本工作目录：`/workspace/work/eligdemo`（4 个归档版本，来源 `eligsample`，全部为合成资料、身份模式数据）

## 1. 条款检查（先于任何申请读取）

* 条款文件：`scenario/eligibility/clauses/E-9001-v1.json`
* 绑定：`eligsample` / `arc_8f02f27d93eb1db311423d1c5c169b1f` / v1（E-9001，发布 2026-02-10）
* 条款摘要（canonical JSON SHA-256）：`9785314575c0cc545e31e1b773372177b2158ae04c34eca6fd553bfc31d2eb65`
* 正文摘要（body SHA-256）：`236a5d6ab28fc1e0ab0190c18518465f2a88a11f0d24f6ac02b9f69ec743786a`

| 条件 ID | 作用域 | 引文区间 | 引文 | 核对 |
| --- | --- | --- | --- | --- |
| `ROOT` | subject | [0,33) | 各申报单位：现就2026年度重点资助项目申报资格预审条件通告如下。 | body[start:end] == quote ✔ |
| `ENTITY_KIND` | subject | [44,67) | 申报主体应为本市依法登记的高校、科研院所或企业 | body[start:end] == quote ✔ |
| `ENTITY_YEARS` | subject | [68,77) | 登记年限不少于3年 | body[start:end] == quote ✔ |
| `LEGAL_DATE` | subject | [78,102) | 法人登记日期须在2021年1月1日之后（含当日） | body[start:end] == quote ✔ |
| `PI_DEGREE` | member | [115,140) | 项目负责人须具有博士学位，且从事研究工作不少于5年 | body[start:end] == quote ✔ |
| `PI_YEARS` | member | [115,140) | 项目负责人须具有博士学位，且从事研究工作不少于5年 | body[start:end] == quote ✔ |
| `PI_CONDITION` | subject | [115,140) | 项目负责人须具有博士学位，且从事研究工作不少于5年 | body[start:end] == quote ✔ |
| `COUNT_DOCTORS` | subject | [152,183) | 团队中至少2名成员应具有博士学位，且每人从事研究工作不少于3年 | body[start:end] == quote ✔ |
| `INTEGRITY_OK` | subject | [195,219) | 申报主体及团队成员近5年内不得有科研诚信失信记录 | body[start:end] == quote ✔ |

## 2. 条款无效时拒绝读取申请、不产出判定

| 文件 | 错误类别 | 位置与原因 |
| --- | --- | --- |
| E-9001-v1-shifted.json | citation_mismatch | conditions[7].citation: cited slice does not equal the quote; quote occurs at [[152, 183]] but the clause says [153,184) |
| E-9001-to-public.json | citation_in_other_variant, citation_mismatch, citation_out_of_bounds | conditions[0].citation: quote is absent from bound variant arc_48ae4261e83d7b1d3128bb5f7603d951 v1 but appears in another variant of notice 'E-9001': arc_8f02f27d93eb1db311423d1c5c169b1f v1, arc_8f02f27d93eb1db311423d1c5c169b1f v2 — the clause is bound to the wrong business variant |
| E-9001-v2-as-v1.json | citation_not_found | conditions[7].citation: quote not found anywhere in bound body arc_8f02f27d93eb1db311423d1c5c169b1f v2: '团队中至少2名成员应具有博士学位，且每人从事研究工作不少于3年' |

上述三类（不存在版本 / 同编号串档 / 引文不符）均在条款校验阶段被拒绝，程序以退出码 3 结束，未读取任何申请。另有条款编录错误（字段拼写、作用域、常量类型、未定义引用、引用环、`at_least` 非整数等）同样在此阶段报出，见 §8 与测试。

## 3. 批量预审（按输入顺序逐行）

* 运行 `1` 状态：`completed`；写入 12 条
* 汇总：满足 **4**，不满足 **5**，待补证 **3**（共 12 份）
* 条款摘要 `9785314575c0cc545e31e1b773372177b2158ae04c34eca6fd553bfc31d2eb65`；输入摘要 `a1c66932b8d4353d3b23925110b1bba87aec46b3930e0a55112e342bff10cd23`（4283 字节，12 行）

| 序号 | 输入行 | application_id | 结论 | 根条件 |
| --- | --- | --- | --- | --- |
| 0 | 1 | APP-1001 | 满足 | `ROOT` |
| 1 | 2 | APP-1002 | 不满足 | `ROOT` |
| 2 | 3 | APP-1003 | 满足 | `ROOT` |
| 3 | 4 | APP-1004 | 待补证 | `ROOT` |
| 4 | 5 | APP-1005 | 不满足 | `ROOT` |
| 5 | 6 | APP-1006 | 满足 | `ROOT` |
| 6 | 7 | APP-1007 | 不满足 | `ROOT` |
| 7 | 8 | APP-1008 | 待补证 | `ROOT` |
| 8 | 9 | APP-1009 | 不满足 | `ROOT` |
| 9 | 10 | APP-1010 | 不满足 | `ROOT` |
| 10 | 11 | APP-1011 | 满足 | `ROOT` |
| 11 | 12 | APP-1012 | 待补证 | `ROOT` |

## 4. 单份申请的完整解释（未短路，覆盖所有分支）

申请 `APP-1001`：**满足**（run 1，输入第 1 行）

```
all [true] conditions[0].expr
  ref [true] conditions[0].expr.all[0]
    · → 条件 ENTITY_KIND （展开见 /condition_nodes/subject/ENTITY_KIND）
    in [true] conditions[1].expr
      · entity_type @ /subject/entity_type 实值="research_institute"  entity_type = 'research_institute'（string） 属于 ['university', 'research_institute', 'enterprise'] → true
  ref [true] conditions[0].expr.all[1]
    · → 条件 ENTITY_YEARS （展开见 /condition_nodes/subject/ENTITY_YEARS）
    compare [true] conditions[2].expr
      · registered_years @ /subject/registered_years 实值="3"  registered_years = '3'（decimal） ge 3 → true
  ref [true] conditions[0].expr.all[2]
    · → 条件 LEGAL_DATE （展开见 /condition_nodes/subject/LEGAL_DATE）
    compare [true] conditions[3].expr
      · legal_registered_on @ /subject/legal_registered_on 实值="2021-01-01"  legal_registered_on = '2021-01-01'（date） ge 2021-01-01 → true
  ref [true] conditions[0].expr.all[3]
    · → 条件 PI_CONDITION （展开见 /condition_nodes/subject/PI_CONDITION）
    count [true] conditions[6].expr
      · 阈值=1 人数区间=[1, 1] 已知真/假/未知=1/3/0
      · 成员[0] (/items/0) → true
        all [true] conditions[6].expr.count.predicate
          compare [true] conditions[6].expr.count.predicate.all[0]
            · role @ /items/0/role 实值="pi"  role = 'pi'（string） eq pi → true
          ref [true] conditions[6].expr.count.predicate.all[1]
            · → 条件 PI_DEGREE （展开见 /condition_nodes/members[PI_DEGREE,0]）
          ref [true] conditions[6].expr.count.predicate.all[2]
            · → 条件 PI_YEARS （展开见 /condition_nodes/members[PI_YEARS,0]）
          · all: 全部分支为真 → 真
      · 成员[1] (/items/1) → false
        all [false] conditions[6].expr.count.predicate
          compare [false] conditions[6].expr.count.predicate.all[0]
            · role @ /items/1/role 实值="co_investigator"  role = 'co_investigator'（string） eq pi → false
          ref [true] conditions[6].expr.count.predicate.all[1]
            · → 条件 PI_DEGREE （展开见 /condition_nodes/members[PI_DEGREE,1]）
          ref [false] conditions[6].expr.count.predicate.all[2]
            · → 条件 PI_YEARS （展开见 /condition_nodes/members[PI_YEARS,1]）
          · all: 存在假分支 → 假（假优先于未知）
      · 成员[2] (/items/2) → false
        all [false] conditions[6].expr.count.predicate
          compare [false] conditions[6].expr.count.predicate.all[0]
            · role @ /items/2/role 实值="co_investigator"  role = 'co_investigator'（string） eq pi → false
          ref [false] conditions[6].expr.count.predicate.all[1]
            · → 条件 PI_DEGREE （展开见 /condition_nodes/members[PI_DEGREE,2]）
          ref [true] conditions[6].expr.count.predicate.all[2]
            · → 条件 PI_YEARS （展开见 /condition_nodes/members[PI_YEARS,2]）
          · all: 存在假分支 → 假（假优先于未知）
      · 成员[3] (/items/3) → false
        all [false] conditions[6].expr.count.predicate
          compare [false] conditions[6].expr.count.predicate.all[0]
            · role @ /items/3/role 实值="co_investigator"  role = 'co_investigator'（string） eq pi → false
          ref [true] conditions[6].expr.count.predicate.all[1]
            · → 条件 PI_DEGREE （展开见 /condition_nodes/members[PI_DEGREE,3]）
          ref [unknown] conditions[6].expr.count.predicate.all[2]
            · → 条件 PI_YEARS （展开见 /condition_nodes/members[PI_YEARS,3]）
          · all: 存在假分支 → 假（假优先于未知）
  ref [true] conditions[0].expr.all[4]
    · → 条件 COUNT_DOCTORS （展开见 /condition_nodes/subject/COUNT_DOCTORS）
    count [true] conditions[7].expr
      · 阈值=2 人数区间=[2, 3] 已知真/假/未知=2/1/1
      · 成员[0] (/items/0) → true
        all [true] conditions[7].expr.count.predicate
          compare [true] conditions[7].expr.count.predicate.all[0]
            · degree_level @ /items/0/degree_level 实值="doctorate"  degree_level = 'doctorate'（string） eq doctorate → true
          compare [true] conditions[7].expr.count.predicate.all[1]
            · years_experience @ /items/0/years_experience 实值="5"  years_experience = '5'（decimal） ge 3 → true
          · all: 全部分支为真 → 真
      · 成员[1] (/items/1) → true
        all [true] conditions[7].expr.count.predicate
          compare [true] conditions[7].expr.count.predicate.all[0]
            · degree_level @ /items/1/degree_level 实值="doctorate"  degree_level = 'doctorate'（string） eq doctorate → true
          compare [true] conditions[7].expr.count.predicate.all[1]
            · years_experience @ /items/1/years_experience 实值="3"  years_experience = '3'（decimal） ge 3 → true
          · all: 全部分支为真 → 真
      · 成员[2] (/items/2) → false
        all [false] conditions[7].expr.count.predicate
          compare [false] conditions[7].expr.count.predicate.all[0]
            · degree_level @ /items/2/degree_level 实值="master"  degree_level = 'master'（string） eq doctorate → false
          compare [true] conditions[7].expr.count.predicate.all[1]
            · years_experience @ /items/2/years_experience 实值="8"  years_experience = '8'（decimal） ge 3 → true
          · all: 存在假分支 → 假（假优先于未知）
      · 成员[3] (/items/3) → unknown
        all [unknown] conditions[7].expr.count.predicate
          compare [true] conditions[7].expr.count.predicate.all[0]
            · degree_level @ /items/3/degree_level 实值="doctorate"  degree_level = 'doctorate'（string） eq doctorate → true
          compare [unknown] conditions[7].expr.count.predicate.all[1]
            · years_experience @ /items/3/years_experience 实值=（缺失）  years_experience: 字段为 null（null_value） → 未知（待补证）
          · all: 无假分支但存在未知 → 待补证
  ref [true] conditions[0].expr.all[5]
    · → 条件 INTEGRITY_OK （展开见 /condition_nodes/subject/INTEGRITY_OK）
    compare [true] conditions[8].expr
      · has_integrity_issue @ /subject/has_integrity_issue 实值=false  has_integrity_issue = False（boolean） eq False → true
  · all: 全部分支为真 → 真
```

未知值 2 处（各自记录原因）：

| 条件 | JSON Pointer | 原因 |
| --- | --- | --- |
| `COUNT_DOCTORS` | `/items/3/years_experience` | null_value（字段为 null（null_value）） |
| `PI_YEARS` | `/items/3/years_experience` | null_value（字段为 null（null_value）） |

这两处未知值并未把总体结论改成待补证：成员计数区间为 `[2, 3]`，下界已达门槛，名单中未列或未知的成员只可能让满足人数更多。换言之，只有会改变结论的材料才需要补证。

明确反例优先：`APP-1007` 已有诚信反例（`INTEGRITY_OK` 为假），同时法人登记日期缺失（`LEGAL_DATE` 仍为未知 ['LEGAL_DATE']），总体仍是**不满足**——不会因其他材料缺失而改判待补证。

## 5. “至少两名博士且各有三年经验”：名单与字段怎样改变结论

| 申请 | 情形 | 人数区间 | 计数结论 | 总条件中的其他分支 | 总体结论 |
| --- | --- | --- | --- | --- | --- |
| APP-1001 | 完整名单；已知满足 2 人、未知 1 人（区间 [2,3]） | [2, 3] （真/假/未知 = 2/1/1） | true | ENTITY_KIND=true, ENTITY_YEARS=true, LEGAL_DATE=true, PI_CONDITION=true, COUNT_DOCTORS=true, INTEGRITY_OK=true | 满足 |
| APP-1003 | 名单未列全（complete=false），但已知满足 2 人（下界已达门槛） | [3, ∞) （真/假/未知 = 3/0/0） | true | ENTITY_KIND=true, ENTITY_YEARS=true, LEGAL_DATE=true, PI_CONDITION=true, COUNT_DOCTORS=true, INTEGRITY_OK=true | 满足 |
| APP-1004 | 完整名单；已知 1 人、未知 1 人（区间 [1,2]，卡住门槛） | [1, 2] （真/假/未知 = 1/1/1） | unknown | ENTITY_KIND=true, ENTITY_YEARS=true, LEGAL_DATE=true, PI_CONDITION=true, COUNT_DOCTORS=unknown, INTEGRITY_OK=true | 待补证 |
| APP-1005 | 两人各满足一半：一人有学历无年限，一人有年限无学历 | [0, 0] （真/假/未知 = 0/2/0） | false | ENTITY_KIND=true, ENTITY_YEARS=true, LEGAL_DATE=true, PI_CONDITION=false, COUNT_DOCTORS=false, INTEGRITY_OK=true | 不满足 |
| APP-1012 | 名单未列全且有未知成员（上界不封顶 [1,∞)） | [1, ∞) （真/假/未知 = 1/1/1） | unknown | ENTITY_KIND=true, ENTITY_YEARS=true, LEGAL_DATE=true, PI_CONDITION=true, COUNT_DOCTORS=unknown, INTEGRITY_OK=true | 待补证 |

### APP-1001 团队成员计数

* 结论：`true`；人数区间 [2, 3]；已知满足 2 人（下界 2 ≥ 2）→ 满足

| 成员 | JSON Pointer | 结果 | 学历 | 年限 | 判断依据 |
| --- | --- | --- | --- | --- | --- |
| 0 | `/items/0` | true | "doctorate" | "5" | compare=true; compare=true |
| 1 | `/items/1` | true | "doctorate" | "3" | compare=true; compare=true |
| 2 | `/items/2` | false | "master" | "8" | compare=false; compare=true |
| 3 | `/items/3` | unknown | "doctorate" | null | compare=true; compare=unknown |

### APP-1004 团队成员计数

* 结论：`unknown`；人数区间 [1, 2]；人数区间 [1, 2]，阈值 2 未被确定：下界不足且上界（或未知上界）可能达到 → 待补证

| 成员 | JSON Pointer | 结果 | 学历 | 年限 | 判断依据 |
| --- | --- | --- | --- | --- | --- |
| 0 | `/items/0` | true | "doctorate" | "5" | compare=true; compare=true |
| 1 | `/items/1` | false | "master" | "3" | compare=false; compare=true |
| 2 | `/items/2` | unknown | "doctorate" | null | compare=true; compare=unknown |

### APP-1005 团队成员计数

* 结论：`false`；人数区间 [0, 0]；人数上界 0 < 2 → 不满足

| 成员 | JSON Pointer | 结果 | 学历 | 年限 | 判断依据 |
| --- | --- | --- | --- | --- | --- |
| 0 | `/items/0` | false | "doctorate" | "1" | compare=true; compare=false |
| 1 | `/items/1` | false | "master" | "8" | compare=false; compare=true |

“两人各满足一半”即学历与年限分别落在不同人身上，谓词在同一成员上取 `all`，因此没有任何一名成员完整满足；这是成员谓词的作用域保证，不能拿甲的学历拼乙的年限。

## 6. 后半段结构损坏：逐行号报告、运行标记未完成

* `damaged-bad_json.jsonl`（第 4 行 JSON 语法损坏）：退出状态 `incomplete`，错误 `bad_json` 第 4 行 —— line 4: invalid JSON (Expecting value at column 1)
  - 已写结果：3 条（前缀 `['APP-1001', 'APP-1002', 'APP-1003']`），保留但明确未完成；成功汇总：`None`
* `damaged-duplicate_application_id.jsonl`（第 5 行重复 application_id）：退出状态 `incomplete`，错误 `duplicate_application_id` 第 4 行 —— line 4: application_id 'APP-1002' already appeared earlier in this file
  - 已写结果：3 条（前缀 `['APP-1001', 'APP-1002', 'APP-1003']`），保留但明确未完成；成功汇总：`None`
* `damaged-invalid_item.jsonl`（第 6 行成员结构损坏）：退出状态 `incomplete`，错误 `invalid_item` 第 4 行 —— line 4: items[0] must be a JSON object
  - 已写结果：3 条（前缀 `['APP-1001', 'APP-1002', 'APP-1003']`），保留但明确未完成；成功汇总：`None`

字段缺失、`null` 或类型不符是个别申请的未知值（逐字段记录原因，不中断文件）；上面的坏 JSON、重复 ID、成员结构损坏属于文件结构错误，两者严格区分。

## 7. 生成大文件的逐行处理

* 文件 `applications-large.jsonl`：731700 字节，2000 行（2000 份申请）
* 处理耗时 2.02 s；逐行读取，内存中同时只有当前一份申请与条款；运行状态 `completed`
* 汇总：满足 730，不满足 788，待补证 482
* 顺序保持：首条 `GEN-000001`，末条 `GEN-002000`
* 进程峰值常驻内存约 34 MB（含解释 JSON 的序列化缓冲，不随文件行数线性增长）

## 8. 数据约定与人工核实

* 条款文件逐条引用的原句均已与归档正文逐字核对（`body[start:end] == quote`）；条款改版必须显式提供另一份文件，单次运行始终使用开始时验证的内容。
* 归档库以只读方式打开，只用于核对版本与引文；不依赖资助表或日程结果。
* 十进制资料与常量均以字符串承载，比较使用 Decimal，不经过浮点。
* 本作业为辅助预审：结论、引文与解释均可追溯，最终由工作人员核实决定，不代替人工审批。


# 资助表格结构化作业演示

样本工作目录：`/workspace/work/funddemo`（12 个归档版本，来源 fundsim，全部为身份模式数据）

## 1. 表格处理途中退出
* 受控中断：interrupted inside table transaction ('fundsim', 'arc_7c76d1f6a2171fe968e58ae86f2ca0be', 1, 0)
* 中断后 F-3001 完整版可查询明细数：**0**（事务回滚，无部分明细）；运行状态：`interrupted`
* 重开后安全续处理：booked=1 skipped_booked=1 rejected=8 version_errors=2
* 续处理后 F-3001 完整版明细数：**6**

## 2. 正确展开与金额换算（F-3001 完整版）

逻辑网格：6 行 × 5 列，表头 2 层；rowspan/colspan 已展开为可解释网格。

| 逻辑行 | 机构 | 项目 | 年度 | 原文金额 | 单位 | 金额（元） | 金额格 | 机构格 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r2 | 中科院数学与系统科学研究院 | 基础理论专项 | 2024 | 1,234.56 | yuan(table) | 1234.56 | r2c2 | r2c0 |
| r2 | 中科院数学与系统科学研究院 | 基础理论专项 | 2025 | 1.5 | wan(column) | 15000 | r2c4 | r2c0 |
| r3 | 中科院数学与系统科学研究院 | 应用转化项目 | 2024 | ２０００ | yuan(table) | 2000 | r3c2 | r2c0 |
| r3 | 中科院数学与系统科学研究院 | 应用转化项目 | 2025 | 0.25 | wan(column) | 2500 | r3c4 | r2c0 |
| r4 | 江南大学 | 联合实验室 | 2024 | 500 | yuan(table) | 500 | r4c2 | r4c0 |
| r4 | 江南大学 | 联合实验室 | 2025 | 2 | wan(column) | 20000 | r4c4 | r4c0 |

换算要点：表级单位 `元`（表格说明），2025 年度列头局部单位 `万元` 优先；`２０００` 为全角数字，`1,234.56` 含千分位，`1.5`（万元）→ `15000` 元，全程 Decimal 精确换算。
合计核对：{'2024': '3734.56', '2025': '37500'}（与明细逐项相符，合计行不入账）

## 3. 同编号公众版与完整版互不覆盖
* 完整版 `arc_7c76d1f6a2171fe968e58ae86f2ca0be`：6 条事实
* 公众版 `arc_5fa427ff5d1c5f4bedf684fc76ef92df`：4 条事实（同一 notice_key=F-3001，不同 archive_id）

| 逻辑行 | 机构 | 项目 | 年度 | 原文金额 | 单位 | 金额（元） | 金额格 | 机构格 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | 中科院数学与系统科学研究院 | 基础理论专项 | 2024 | 0.1235 | wan(table) | 1235 | r1c2 | r1c0 |
| r1 | 中科院数学与系统科学研究院 | 基础理论专项 | 2025 | 1.5 | wan(table) | 15000 | r1c3 | r1c0 |
| r2 | 江南大学 | 联合实验室 | 2024 | 0.05 | wan(table) | 500 | r2c2 | r2c0 |
| r2 | 江南大学 | 联合实验室 | 2025 | 2 | wan(table) | 20000 | r2c3 | r2c0 |

## 4. 拒收与版本级错误（保留网格、候选明细与原因）

| 通告 | 表格 | 类别 | 原因码 | 说明 |
| --- | --- | --- | --- | --- |
| F-3009 | t0 | structural | `nested_table` | table contains a nested <table> inside a cell |
| F-3004 | t0 | data | `total_mismatch` | [{"year": 2024, "details_sum_yuan": "400", "total_yuan": "500", "difference_yuan": "100"}] |
| F-3010 | t0 | data | `unknown_currency` | table caption declares unrecognized currency/unit: ['美元'] |
| F-3011 | t0 | data | `missing_unit` | year column 2 (2024) has no column-level unit and the table caption declares none |
| F-3003 | t0 | structural | `amount_spans_years` | amount cell r2c2 spans 2 columns; one merged amount cannot be copied into several year col… |
| F-3008 | t0 | data | `unparseable_amount` | [{"where": "detail", "row": 2, "year": 2025, "cell": [2, 3], "text": "—"}, {"where": "deta… |
| F-3007 | t0 | structural | `unmapped_column` | column 0 header '承担单位' matches no configured alias (institution/project/year/ignore) |
| F-3002 | t0 | structural | `overlap_merge` | cell r4c0 (rowspan 1, colspan 2) overlaps cell r3c1 (rowspan 2, colspan 1) at r4c1 |
| F-3005 | — | source | `missing_raw` | no archived blob for raw_digest 9a3f0f73650fce8e97d6ce32bb86bfad77b468babdf097acf6c82058a2397490 |
| F-3006 | — | source | `digest_mismatch` | archived blob hashes to 5164e54062c231001b79721319ebfd1da698f191ac45a473c12ab66826b371f3, expected 4bebdff2624b4880f1518500068a77134b401e162099d8b5a7f1ffcba9ca6ee7 |

## 5. 改配置后重处理（F-3007）
* 默认配置下：`rejected` / `unmapped_column`（承担单位/拨付项目/拨付经费 未声明）
* 修正配置后重处理：`booked`，明细 1 条（1.25 千元 → 1250 元）
* 该表诊断历史 2 条（旧拒收原因可追溯）：
  - run 2 `rejected` `unmapped_column` config=da4dd6848fd8…
  - run 3 `booked` `-` config=e0904527c5f8…
* 重处理汇总：booked=3 skipped_booked=0 facts_total=11（无重复记账）


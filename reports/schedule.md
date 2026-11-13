# 项目申报日程整理作业演示

样本工作目录：`/workspace/work/schedemo`（17 个归档版本，来源 schedsim / schedsim2，全部为身份模式数据）

## 1. 版本处理途中退出与恢复（事务边界）
* 受控中断：interrupted inside version transaction ('schedsim', 'arc_97af7dd6ba0e3d4007a3bee40f02bd9f', 2)
* 中断后当前节点数：**0**（快照未发布，无部分可查安排）；运行状态：`interrupted`
* 重开后续整理完成：ledger 行 24 条，待确认 4 项，当前节点 20 个
* 重复整理幂等：第二次新增 ledger 行 **0**（相同输入与词表不产生重复节点）

## 2. 当前有效安排（含跨年、省略年、中文数字）

| 项目 | 轮次 | 节点 | 状态 | 归一日期 | 来源公告 | 版本 | 原文写法 | 段落 | 字符区间 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| X-2001 | 1 | apply_open | active | 2026-09-01 | X-2001 | v1 | 2026年9月1日 | p1 | [46,55)  |
| X-2001 | 1 | submit_deadline | active | 2026-09-25 | S-1009 | v1 | 2026年9月25日 | p1 | [55,65)  |
| S-1001 | 1 | apply_open | active | 2026-03-10 | S-1001 | v1 | 2026年3月10日 | p1 | [60,70)  |
| S-1001 | 1 | submit_deadline | active | 2026-04-08 | S-1003 | v2 | 2026年4月8日 | p1 | [81,90)  |
| S-1001 | 1 | review_start | active | 2026-04-05 | S-1001 | v1 | 2026年4月5日 | p1 | [100,109)  |
| S-1002 | 1 | apply_open | active | 2026-12-25 | S-1002 | v1 | 2026年12月25日 | p1 | [48,59)  |
| S-1002 | 1 | submit_deadline | active | 2027-01-15 | S-1002 | v1 | 2027年1月15日 | p1 | [69,79)  |
| S-1002 | 1 | review_start | active | 2027-02-10 | S-1002 | v1 | 2027年2月10日 | p1 | [89,99)  |
| S-1005 | 1 | apply_open | active | 2026-07-01 | S-1005 | v1 | 7月1日 | p1 | [49,53) （省略年补齐） |
| S-1005 | 1 | submit_deadline | active | 2026-07-20 | S-1005 | v1 | 7月20日 | p1 | [63,68) （省略年补齐） |
| S-1005 | 1 | review_start | active | 2026-08-03 | S-1005 | v1 | 8月3日 | p1 | [78,82) （省略年补齐） |
| S-1006 | 1 | apply_open | active | 2026-07-15 | S-1006 | v1 | 2026年7月15日 | p1 | [46,56)  |
| S-1006 | 1 | submit_deadline | active | 2026-07-30 | S-1006 | v1 | 2026年7月30日 | p1 | [66,76)  |
| S-1006 | 1 | review_start | cancelled | — | S-1013 | v1 | — | p1 | —  |
| S-1007 | 1 | apply_open | active | 2026-08-05 | S-1007 | v1 | 2026年8月5日 | p1 | [52,61)  |
| S-1007 | 1 | submit_deadline | active | 2026-08-18 | S-1007 | v1 | 2026年8月18日 | p1 | [71,81)  |
| S-1011 | 1 | apply_open | active | 2026-10-12 | S-1011 | v1 | 二〇二六年十月十二日 | p1 | [47,57)  |
| S-1011 | 1 | submit_deadline | active | 2026-10-28 | S-1011 | v1 | 二〇二六年十月二十八日 | p1 | [67,78)  |
| S-1012 | 1 | apply_open | active | 2026-11-10 | S-1012 | v1 | 2026年11月10日 | p1 | [47,58)  |
| S-1012 | 1 | submit_deadline | active | 2026-11-25 | S-1012 | v1 | 2026年11月25日 | p1 | [68,79)  |

## 3. 仅延长收件期限：历史替换可追溯

* `set` → 2026-03-25，来自 S-1001 v1，原文“2026年3月25日”（p1 [80,90)）
* `set` → 2026-04-08，来自 S-1003 v2，原文“2026年4月8日”（p1 [81,90)）
* 当前查询只显示 **2026-04-08**；旧日期 2026-03-25 仅存于历史，不再出现在当前安排。

## 4. 撤销范围：整轮撤销 ≠ 单节点取消

* S-1001 第 1 轮状态：`open`（依据 S-1001）
* S-1001 第 2 轮状态：`round_cancelled`（依据 S-1004）
* 第二轮节点在当前查询中不可见，但 ledger 保留其 set 与 cancel_round 记录；第一轮三个节点全部仍为 active。
* S-1006 评审节点状态：`cancelled`（单节点取消），轮次状态仍为 `open`，报名与收件节点不受影响。

## 5. 待人工确认材料（不并入已确认安排）

| 公告 | 原因码 | 说明 | 句子 |
| --- | --- | --- | --- |
| S-1012 | `unmapped_date` | date without any configured node-rol | 项目宣讲会日期为2026年11月6日，欢迎各单位参加。 |
| S-1014 | `target_ambiguous` | reference '编号：S-1007' resolved to 2  | 编号：S-1007 |
| S-1010 | `vague_statement` | non-application notice without a con | 关于部分项目申报时间安排的更正通知 |
| S-1010 | `target_unspecified` | no resolvable notice number or proje | 关于部分项目申报时间安排的更正通知 |

## 6. 外部编号与跨来源引用

* schedsim 的 S-1009 写明“来源：schedsim2，编号：X-2001”，唯一定位后把截止日改为 **2026-09-25**。
* S-1014 仅写“编号：S-1007”，而该编号在 schedsim 内对应完整版与公众版两个归档身份（reference '编号：S-1007' resolved to 2 notice(s); need a unique target），系统拒绝任选，整条补充保持待人工确认，两个身份的安排均未被改动。

## 7. 更改词表后重新整理（保留旧判断）

* 新词表下 S-1012 的宣讲会节点入账：2026-11-06。
* 旧词表产生的 unmapped_date 判断仍在历史中（1 条，config=56ce2493fc9e…），当前待确认列表不再包含该项。

## 8. 数据边界

* 管线库 `pipeline.sqlite3` 以只读方式打开；内容寻址原始留档 `archive/` 仅按摘要读取复核，全程不写入、不迁移、不重新访问网站。
* 日程结果写入独立的 `schedule.sqlite3`；旧库（user_version<2）会被明确拒绝并提示先 repair。

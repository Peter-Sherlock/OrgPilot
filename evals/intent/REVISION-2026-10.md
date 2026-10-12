# 评测集修订清单（2026-10）

依据：`LABELING.md`（R1–R10，产品确定的唯一口径）。修订范围：全部 145 条旧样本逐一复查，
另新增 13 条样本。旧标注 → 新标注均指 `expected.behavior` / `expected.intents` 的变化。

## 一、按规则统计

| 规则 | 修订条数 | 说明 |
|---|---|---|
| R1 健康度 | 4 | 预计完成时间与任务截止日比较后重定级 |
| R2 否定句 | 7 | 负责人否定风险 → on_track claim；字面否定、问题仍在 → at_risk |
| R3 带疑问的等待 | 1 | 带疑问的等待统一为 query_status + query_answer |
| R5 回复类别 | 11 | 假设句/拒同步 → noted；索要文档/流程问题 → unsupported |
| R6 含糊语气 | 1 | 给出时间 → 按 R1 生成 claim |
| R7 任务歧义 | 1 | 未提任务名且负责人有多个任务 → 追问 |
| R8 全员同步 | 8 | PM 自然语言请求 → 只发确认卡片，不直接发起 |
| R4/R9/R10 | 0 | 复查后无样本需要修改（R10 仅补充文件头约定） |
| **合计** | **33** | 另有 1 条样本类别调整（见 R3） |

## 二、逐条修订

### R1 健康度（4 条）

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| dev_report_status_001 | delayed | at_risk（behavior.claims 与 intents 同步改） | 预计 10-14 17:00 早于 pay_sdk 截止日 10-16 18:00：有阻塞但预计截止日前解决 |
| test_answer_pending_003 | delayed | at_risk（behavior + intents） | 预计 10-15 17:30 早于 pay_sdk 截止日 10-16 18:00 |
| dev_time_chinese_004 | on_track | delayed（behavior + intents） | 月底（10-31 18:00）晚于 ledger_check 截止日 10-22 |
| test_time_chinese_001 | on_track | delayed（behavior + intents） | 月底（10-31 18:00）晚于 pay_e2e_test 截止日 10-20 |

### R2 否定句（7 条）

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| dev_negation_001 | 无 claim，smalltalk | +pay_sdk on_track claim，claim_recorded；intents 增加 health_status=on_track | 负责人否定风险 → 生成 on_track claim |
| dev_negation_002 | 无 claim，smalltalk | +pay_e2e_test on_track claim，claim_recorded；intents 同上 | 同上 |
| dev_negation_003 | 无 claim，smalltalk | +ledger_check on_track claim，claim_recorded；intents 同上 | 同上 |
| dev_negation_004 | 无 claim，smalltalk | +checkout_page on_track claim，claim_recorded；intents 同上 | 同上 |
| dev_negation_005 | 无 claim，smalltalk | +merchant_admin on_track claim，claim_recorded；intents 同上 | 同上 |
| test_negation_001 | 无 claim，smalltalk | +merchant_admin on_track claim，claim_recorded；intents 同上 | 同上 |
| test_negation_003 | 无 claim，smalltalk | +pay_sdk **at_risk** claim，claim_recorded；intents 增加 health_status=at_risk | 字面否定（"没有扩大"）但问题仍在 |

### R3 带疑问的等待（1 条）

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| dev_p1_p2_003 | category=p1_p2，unsupported，intent=dependency_wait | category=query_status，query_answer，intent=query_status | 带疑问的等待 → query_status；与 dev_query_status_005、test_query_status_003 口径一致 |

复查确认：dev_query_status_005、test_query_status_003 已符合 R3，未改动；
dev_p1_p2_004（无疑问、要求催办）保留 dependency_wait + unsupported。

### R5 回复类别（11 条）

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| dev_hypothetical_001 | smalltalk | noted | 假设句与项目相关但不改变状态 |
| dev_hypothetical_002 | smalltalk | noted | 同上 |
| dev_hypothetical_003 | smalltalk | noted | 同上 |
| test_hypothetical_001 | smalltalk | noted | 同上 |
| test_hypothetical_002 | smalltalk | noted | 同上 |
| dev_sync_near_negative_002 | smalltalk | noted | "不用同步进度了"：拒同步、无状态变更 |
| dev_sync_near_negative_003 | smalltalk | noted | 同上 |
| dev_sync_near_negative_004 | smalltalk | noted | 同上 |
| test_sync_near_negative_002 | smalltalk | noted | 同上 |
| dev_multi_turn_topic_shift_001 | query_answer | unsupported；intent query_status → out_of_scope | "周报还用写吗"属流程类问题，处理不了 |
| test_multi_turn_topic_shift_001 | query_answer | unsupported；intent query_status → out_of_scope | "谁有接口文档发我一份"属索要文档 |

### R6 含糊语气（1 条）

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| dev_hypothetical_004 | 无 claim，smalltalk，intent speech_act=hypothetical | +pay_sdk at_risk claim（expected_completion=2026-10-14T18:00+08:00，"明天"无时刻按 18:00），claim_recorded；intent 改 statement/hedged 并补 time | 给出了时间 → 按 R1 生成 claim |

### R7 任务歧义（1 条）

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| test_negation_002 | smalltalk | clarify + asks_clarification=true | "我这边没有遇到什么阻塞"未提任务名，王伟负责 pay_sdk 与 merchant_admin 两个任务，不猜测任务 |

### R8 全员同步（8 条）

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| dev_request_sync_001～005 | starts_sync=true + 确认卡片 | **starts_sync=false**，保留 sends_confirmation_card=true、sync_confirm | PM 自然语言请求 → 只发确认卡片，确认后才发起 |
| test_request_sync_001～003 | starts_sync=true + 确认卡片 | **starts_sync=false**，其余同上 | 同上 |

## 三、新增样本（13 条，约 6:4 分配）

| 样本 | 类别 | 要点 | 规则 |
|---|---|---|---|
| dev_sync_command_001 | sync_command | PM 发 `/同步` → 直接发起（starts_sync=true，无卡片） | R8 |
| dev_sync_command_002 | sync_command | PM 发 `/同步 全员进度` → 直接发起 | R8 |
| dev_sync_command_003 | sync_command | PM 发 `/帮助` → help | R8 |
| test_sync_command_001 | sync_command | PM 发 `/同步` → 直接发起 | R8 |
| test_sync_command_002 | sync_command | 非 PM 发 `/同步` → no_permission | R8 |
| dev_confirmation_flow_001 | confirmation_flow | 待确认卡片，PM"好的，发吧"→ 发起 | R8 |
| dev_confirmation_flow_002 | confirmation_flow | 待确认卡片，PM"可以，就按这个发"→ 发起 | R8 |
| dev_confirmation_flow_003 | confirmation_flow | 待确认卡片，PM"算了，先不发"→ 不发起，noted | R8 |
| dev_confirmation_flow_004 | confirmation_flow | 待确认卡片，非 PM 回"好的"→ 不发起，smalltalk | R8 |
| test_confirmation_flow_001 | confirmation_flow | 待确认卡片，PM"发吧"→ 发起 | R8 |
| test_confirmation_flow_002 | confirmation_flow | 待确认卡片，PM"这周先不用发了"→ 不发起，noted | R8 |
| test_confirmation_flow_003 | confirmation_flow | 待确认卡片，PM"ok，确认发送"→ 发起 | R8 |
| dev_p1_p2_007 | p1_p2 | P2 计划变更（挪时间）→ unsupported；补足 dev_p1_p2_003 移出后 p1_p2 的覆盖度 | R5 |

新增后规模：dev 100 条、test 58 条，合计 158 条（dev 占比 63%）。

## 四、复查后无需修改的规则

- **R4**：非负责人汇报他人任务的样本（如 test_negation_003，陈磊汇报 pay_sdk）均照常生成 claim，与规则一致；
- **R9**：multi_turn_topic_shift 的热搜/午饭样本已按新话题类别回复并保留 asks_clarification=true；
- **R10**：仅补充文件头约定"只有日期没有时刻 = 当天 18:00"，存量样本时间标注已符合。

---

## 五、R11 补充修订（2026-10-12，产品新增规则后）

### 规则 R11：expected_completion 只填完成时间

只填"任务完成或阻塞解除"的时间；开始时间、演示时间、定位原因的时间、下次同步的时间不填；
任务已完成的样本也不填。全文见 `LABELING.md` R11。

### 消息改写（使时间成为完成时间，保留解析难点）

| 样本 | 旧原文 | 新原文 | 规则 |
|---|---|---|---|
| dev_time_chinese_001 | 支付SDK的报错还在查，预计下午三点能定位 | 支付SDK的报错还在修，预计下午三点能修好 | R11（定位 → 修好）；"三点"难点保留 |
| dev_time_chinese_002 | 商户后台的审批流搞定了，5点半给你们演示 | 商户后台的审批流收尾很顺利，预计5点半能全部搞定 | R11（演示 → 完成）；"5点半"难点保留 |
| dev_time_chinese_003 | 压测可能要拖到下周三才能开始 | 压测第一轮可能要拖到下周三才能跑完 | R11（开始 → 跑完）；"下周三"难点保留 |
| test_answer_pending_001 | 环境修好了，明天上午十点开始全链路跑 | 环境还在抢修，预计明天上午十点能恢复 | R11（开始 → 恢复）；"明天上午十点"难点保留；健康度 on_track → at_risk（R1：抢修中、截止日前恢复） |
| test_time_chinese_001 | 压测数据要在月底前出报告 | 压测要拖到月底才能做完 | 明确延期表述（保留"月底"）；健康度维持 delayed |
| test_time_chinese_002 | 压测第一轮晚上八点开跑 | 压测第一轮正在跑，预计晚上八点能全部跑完 | R11（开跑 → 跑完）；"晚上八点"难点保留 |

### 健康度修正

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| dev_time_chinese_004 | on_track | delayed（behavior + intents） | 月底（10-31 18:00）晚于 ledger_check 截止日 10-22（R1）。**更正**：第一轮修订清单的 R1 表格误记此样本已改为 delayed，实际未落地，本轮补上 |

### 回复类别拆分：sync_confirm → sync_confirm / sync_started

`sync_confirm` 现在只表示"只发了确认卡片、尚未发起"；已发起同步的样本改为 `sync_started`：

| 样本 | 旧标注 | 新标注 | 依据 |
|---|---|---|---|
| dev_sync_command_001 | sync_confirm | sync_started | R8：命令直达发起 |
| dev_sync_command_002 | sync_confirm | sync_started | 同上 |
| dev_confirmation_flow_001 | sync_confirm | sync_started | R8：卡片确认后发起 |
| dev_confirmation_flow_002 | sync_confirm | sync_started | 同上 |
| test_sync_command_001 | sync_confirm | sync_started | R8：命令直达发起 |
| test_confirmation_flow_001 | sync_confirm | sync_started | R8：卡片确认后发起 |
| test_confirmation_flow_003 | sync_confirm | sync_started | 同上 |

request_sync 8 条（自然语言、只发卡片）维持 `sync_confirm` 不变。

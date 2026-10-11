# 意图行为评测报告（split=test, mode=mock）

评测以系统行为为准（ADR-0008 §8）；baseline 无意图层，意图指标记为 N/A。

## 总体指标

| 指标 | 值 |
|---|---|
| 样本数 | 53 |
| claim precision | 0.00% |
| claim recall | 0.00% |
| task_id 准确率 | 20.00% |
| 健康状态准确率 | 0.00% |
| 时间准确率 | 0.00% |
| 错误 claim 率 | 11.11% (4/36) |
| 同步误触发率 | 0.00% |
| 回复类别准确率 | 26.42% |
| 多轮补全成功率 | 0.00% (4 条多轮样本) |
| 意图层指标 | N/A（baseline 无意图层） |

## 按类别统计

| 类别 | 样本 | 错误 claim | 同步误触发 | 回复正确 | claim P | claim R |
|---|---|---|---|---|---|---|
| acknowledge | 3 | 0 | 0 | 3 | 0% | 0% |
| answer_pending | 3 | 0 | 0 | 0 | 0% | 0% |
| chitchat | 3 | 0 | 0 | 3 | 0% | 0% |
| disambiguation | 2 | 0 | 0 | 0 | 0% | 0% |
| help | 3 | 0 | 0 | 0 | 0% | 0% |
| hypothetical | 2 | 0 | 0 | 2 | 0% | 0% |
| make_commitment | 3 | 0 | 0 | 2 | 0% | 0% |
| multi_task | 3 | 0 | 0 | 0 | 0% | 0% |
| multi_turn_topic_shift | 1 | 0 | 0 | 0 | 0% | 0% |
| negation | 3 | 1 | 0 | 2 | 0% | 0% |
| out_of_scope | 3 | 0 | 0 | 0 | 0% | 0% |
| p1_p2 | 4 | 0 | 0 | 0 | 0% | 0% |
| query_status | 3 | 1 | 0 | 0 | 0% | 0% |
| report_status | 3 | 0 | 0 | 0 | 0% | 0% |
| request_sync | 3 | 0 | 0 | 0 | 0% | 0% |
| substring_trap | 2 | 1 | 0 | 1 | 0% | 0% |
| sync_near_negative | 2 | 0 | 0 | 1 | 0% | 0% |
| sync_non_pm | 1 | 0 | 0 | 0 | 0% | 0% |
| time_chinese | 4 | 0 | 0 | 0 | 0% | 0% |
| vague | 2 | 1 | 0 | 0 | 0% | 0% |

## 失败样本（42 条）

### test_report_status_001（report_status）

- 遗漏 claim：pay_sdk 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_sdk", "health_status": "on_track", "expected_completion": "2026-10-14T20:00:00+08:00"}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_report_status_002（report_status）

- 遗漏 claim：pay_e2e_test 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_e2e_test", "health_status": "at_risk", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_report_status_003（report_status）

- 遗漏 claim：checkout_page 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "checkout_page", "health_status": "on_track", "expected_completion": "2026-10-14T18:00:00+08:00"}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_make_commitment_001（make_commitment）

- 错误 commitment：('pay_e2e_test', 'workflow_status', 'review')
- 遗漏 commitment：('pay_e2e_test', 'workflow_status', 'done')
- 期望：`{"claims": [], "commitments": [{"task_id": "pay_e2e_test", "predicate": "workflow_status", "expected_value": "done", "due_at": "2026-10-14T18:00:00+08:00"}], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [{"task_id": "pay_e2e_test", "predicate": "workflow_status", "expected_value": "review", "due_at": "2026-10-14T18:00:00Z"}], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`

### test_make_commitment_002（make_commitment）

- 遗漏 commitment：('checkout_page', 'workflow_status', 'done')
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [], "commitments": [{"task_id": "checkout_page", "predicate": "workflow_status", "expected_value": "done", "due_at": "2026-10-15T10:00:00+08:00"}], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_make_commitment_003（make_commitment）

- 错误 commitment：('pay_sdk', 'workflow_status', 'review')
- 遗漏 commitment：('pay_sdk', 'workflow_status', 'done')
- 期望：`{"claims": [], "commitments": [{"task_id": "pay_sdk", "predicate": "workflow_status", "expected_value": "done", "due_at": "2026-10-15T12:00:00+08:00"}], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [{"task_id": "pay_sdk", "predicate": "workflow_status", "expected_value": "review", "due_at": "2026-10-15T18:00:00Z"}], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`

### test_answer_pending_001（answer_pending）

- 遗漏 claim：pay_e2e_test 未生成状态
- 追问不符：期望 不追问，实际 追问了
- 回复类别不符：期望 claim_recorded，实际 clarify
- 期望：`{"claims": [{"task_id": "pay_e2e_test", "health_status": "on_track", "expected_completion": "2026-10-15T10:00:00+08:00"}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": true, "reply_category": "clarify"}`

### test_answer_pending_002（answer_pending）

- 遗漏 claim：ledger_check 未生成状态
- 追问不符：期望 不追问，实际 追问了
- 回复类别不符：期望 claim_recorded，实际 clarify
- 期望：`{"claims": [{"task_id": "ledger_check", "health_status": "on_track", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": true, "reply_category": "clarify"}`

### test_answer_pending_003（answer_pending）

- 遗漏 claim：pay_sdk 未生成状态
- 追问不符：期望 不追问，实际 追问了
- 回复类别不符：期望 claim_recorded，实际 clarify
- 期望：`{"claims": [{"task_id": "pay_sdk", "health_status": "delayed", "expected_completion": "2026-10-15T17:30:00+08:00"}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": true, "reply_category": "clarify"}`

### test_query_status_001（query_status）

- 回复类别不符：期望 query_answer，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "query_answer"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_query_status_002（query_status）

- 回复类别不符：期望 query_answer，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "query_answer"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_query_status_003（query_status）

- 错误 claim：不应在 pay_sdk 上生成状态
- 回复类别不符：期望 query_answer，实际 claim_recorded
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "query_answer"}`
- 实际：`{"claims": [{"task_id": "pay_sdk", "health_status": "at_risk", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`

### test_request_sync_001（request_sync）

- 同步触发不符：期望 发起，实际 未发起
- 确认卡片不符：期望 发送，实际 未发送
- 回复类别不符：期望 sync_confirm，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": true, "sends_confirmation_card": true, "asks_clarification": false, "reply_category": "sync_confirm"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_request_sync_002（request_sync）

- 同步触发不符：期望 发起，实际 未发起
- 确认卡片不符：期望 发送，实际 未发送
- 回复类别不符：期望 sync_confirm，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": true, "sends_confirmation_card": true, "asks_clarification": false, "reply_category": "sync_confirm"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_request_sync_003（request_sync）

- 同步触发不符：期望 发起，实际 未发起
- 确认卡片不符：期望 发送，实际 未发送
- 回复类别不符：期望 sync_confirm，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": true, "sends_confirmation_card": true, "asks_clarification": false, "reply_category": "sync_confirm"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_help_001（help）

- 回复类别不符：期望 help，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "help"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_help_002（help）

- 回复类别不符：期望 help，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "help"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_help_003（help）

- 回复类别不符：期望 help，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "help"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_out_of_scope_001（out_of_scope）

- 回复类别不符：期望 unsupported，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "unsupported"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_out_of_scope_002（out_of_scope）

- 回复类别不符：期望 unsupported，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "unsupported"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_out_of_scope_003（out_of_scope）

- 回复类别不符：期望 unsupported，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "unsupported"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_negation_003（negation）

- 错误 claim：不应在 pay_sdk 上生成状态
- 回复类别不符：期望 smalltalk，实际 claim_recorded
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`
- 实际：`{"claims": [{"task_id": "pay_sdk", "health_status": "at_risk", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`

### test_vague_001（vague）

- 错误 claim：不应在 pay_e2e_test 上生成状态
- 追问不符：期望 追问，实际 未追问
- 回复类别不符：期望 clarify，实际 claim_recorded
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": true, "reply_category": "clarify"}`
- 实际：`{"claims": [{"task_id": "pay_e2e_test", "health_status": "at_risk", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`

### test_vague_002（vague）

- 追问不符：期望 追问，实际 未追问
- 回复类别不符：期望 clarify，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": true, "reply_category": "clarify"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_multi_task_001（multi_task）

- 遗漏 claim：pay_sdk 未生成状态
- 遗漏 claim：merchant_admin 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_sdk", "health_status": "on_track", "expected_completion": null}, {"task_id": "merchant_admin", "health_status": "on_track", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_multi_task_002（multi_task）

- 遗漏 claim：pay_e2e_test 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_e2e_test", "health_status": "on_track", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_multi_task_003（multi_task）

- 遗漏 claim：checkout_page 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "checkout_page", "health_status": "on_track", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_disambiguation_001（disambiguation）

- 遗漏 claim：pay_e2e_test 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_e2e_test", "health_status": "on_track", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_disambiguation_002（disambiguation）

- 遗漏 claim：checkout_page 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "checkout_page", "health_status": "on_track", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_time_chinese_001（time_chinese）

- 遗漏 claim：pay_e2e_test 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_e2e_test", "health_status": "on_track", "expected_completion": "2026-10-31T18:00:00+08:00"}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_time_chinese_002（time_chinese）

- 遗漏 claim：pay_e2e_test 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_e2e_test", "health_status": "on_track", "expected_completion": "2026-10-14T20:00:00+08:00"}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_time_chinese_003（time_chinese）

- 遗漏 claim：pay_sdk 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_sdk", "health_status": "at_risk", "expected_completion": "2026-10-16T12:00:00+08:00"}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_time_chinese_004（time_chinese）

- 遗漏 claim：ledger_check 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "ledger_check", "health_status": "on_track", "expected_completion": "2026-10-18T18:00:00+08:00"}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_substring_trap_001（substring_trap）

- 错误 claim：不应在 pay_e2e_test 上生成状态
- 回复类别不符：期望 query_answer，实际 claim_recorded
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "query_answer"}`
- 实际：`{"claims": [{"task_id": "pay_e2e_test", "health_status": "at_risk", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`

### test_substring_trap_002（substring_trap）

- 健康度不符：pay_sdk 期望 on_track，实际 at_risk
- 期望：`{"claims": [{"task_id": "pay_sdk", "health_status": "on_track", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [{"task_id": "pay_sdk", "health_status": "at_risk", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`

### test_p1_p2_001（p1_p2）

- 回复类别不符：期望 unsupported，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "unsupported"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_p1_p2_002（p1_p2）

- 回复类别不符：期望 unsupported，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "unsupported"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_p1_p2_003（p1_p2）

- 回复类别不符：期望 unsupported，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "unsupported"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_p1_p2_004（p1_p2）

- 回复类别不符：期望 unsupported，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "unsupported"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_sync_near_negative_001（sync_near_negative）

- 遗漏 claim：pay_sdk 未生成状态
- 回复类别不符：期望 claim_recorded，实际 smalltalk
- 期望：`{"claims": [{"task_id": "pay_sdk", "health_status": "on_track", "expected_completion": null}], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "claim_recorded"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_sync_non_pm_001（sync_non_pm）

- 回复类别不符：期望 no_permission，实际 smalltalk
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "no_permission"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": false, "reply_category": "smalltalk"}`

### test_multi_turn_topic_shift_001（multi_turn_topic_shift）

- 回复类别不符：期望 query_answer，实际 clarify
- 期望：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": true, "reply_category": "query_answer"}`
- 实际：`{"claims": [], "commitments": [], "starts_sync": false, "sends_confirmation_card": false, "asks_clarification": true, "reply_category": "clarify"}`

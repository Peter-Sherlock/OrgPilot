# Ground Truth 场景规范

## 场景作为可执行规格

`evals/scenarios/*.yaml` 同时包含输入事件序列/多轮交互与预期结果。测试统一调用 `ScenarioRunner`，并由 `evaluate_scenario` 进行结构化严格比较。

---

## 场景清单

### 1. P0 静态确定性内核场景

| 文件 | 场景 ID | 核心验证点 |
| --- | --- | --- |
| `01_delay_propagation.yaml` | `delay_propagation` | 延期沿两级依赖传播，并私聊追问恢复时间 |
| `02_ambiguous_report_requires_question.yaml` | `ambiguous_report_requires_question` | 低置信风险先私聊追问，不公开广播 |
| `03_workflow_health_separation.yaml` | `workflow_health_separation` | `doing` 与 `at_risk` 并存，官方状态与健康判断分离 |
| `04_recovery_supersedes_risk.yaml` | `recovery_supersedes_risk` | 新恢复声明 supersede 旧风险声明，旧承诺显式失效 |

---

### 2. M1 多轮交互闭环场景

| 文件 | 场景 ID | 核心验证点 |
| --- | --- | --- |
| `m1_01_delay_inquiry_and_recovery.yaml` | `m1_01_delay_inquiry_and_recovery` | 延期追问并在成员回复恢复时间后，缺失信息补全，Case 标记为 `resolved` |
| `m1_02_delay_reschedule_approved.yaml` | `m1_02_delay_reschedule_approved` | 延期导致冲突后提议改期，PM 批准后自动执行任务更新并关闭 Case |
| `m1_03_pm_rejects_reschedule.yaml` | `m1_03_pm_rejects_reschedule` | PM 拒绝改期提议，Agent 严禁未授权修改任务，记录拒绝原因并将 Case 标记为 `escalated` |
| `m1_04_recovery_cancels_pending_inquiry.yaml` | `m1_04_recovery_cancels_pending_inquiry` | 等待成员回复期间任务自行恢复，Reconciler 自动取消待处理 Case，不发重复提醒 |
| `m1_05_unresponsive_member_escalates.yaml` | `m1_05_unresponsive_member_escalates` | 成员超时未回复且达到重试上限，自动触发升级至人工，杜绝无限刷屏打扰 |

---

## 场景编写规则

1. 使用独立且唯一的 `scenario_id` 和 `project_id`；
2. 所有时间字段必须带有时区；
3. 多轮交互使用 `rounds` 结构，每轮包含时戳、输入事件、审批操作与预期断言；
4. `event_id` 在全场景内唯一且幂等；
5. 必须通过 `orgpilot replay --all` 与 pytest 100% 通过。

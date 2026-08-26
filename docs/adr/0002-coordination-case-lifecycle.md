# ADR-0002：Coordination Case 生命周期与多轮协调闭环

- 状态：Accepted
- 日期：2026-08-26

## 背景

在 P0 阶段，OrgPilot 实现了单向确定性推导：
`OrgEvent → OrgState → DependencyGraph → CoordinationCase → CoordinationAction → PolicyDecision`。

然而，P0 无法执行动作、无法接收团队成员或 PM 的反馈、无法在获得新信息或状态变化时调整已有计划。如果仅重复生成静态候选动作，Agent 容易陷入无限重复提醒、执行过时动作或在缺乏授权时修改关键任务。

## 决策

1. **引入有状态的 `CaseLedger` 与完整的 Case 生命周期**：
   - 状态集合包括：`open`、`waiting_for_response`、`waiting_for_approval`、`approved`、`executing`、`resolved`、`cancelled`、`escalated`；
   - 记录触发证据（Claim IDs）、缺失信息、等待对象与截止时间、历史执行指令与协调轮次；
   - 在每轮执行中对齐当前组织状态：当任务自行恢复或已被解决时，自动将待响应的 Case 置为 `cancelled` 或 `resolved`，杜绝过时提醒。

2. **划分三阶段 Action 生命周期**：
   - `CoordinationAction`：Planner 提出的候选动作及参数；
   - `ActionCommand`：经 `PolicyEngine`（及 `ApprovalManager`）核准后生成的确定性执行指令；
   - `ActionResult`：由 `CollaborationAdapter` 执行并返回的结构化执行结果。

3. **建立独立的 Human Approval 状态机**：
   - 私聊追问（`ASK_RECOVERY_ESTIMATE`、`ASK_CLARIFICATION`）属于低风险，允许自动执行；
   - 任务修改（`UPDATE_TASK`）与公开通知（`NOTIFY_GROUP`）属于高风险，必须获得人工审批；
   - 审批凭证具有唯一性与时效性：拒绝后严禁执行、过期审批不得复用、审批凭证单次消费。

4. **实现 Mock Collaboration Adapter 与事件反馈机制**：
   - 统一提供私聊消息、审批请求、任务更新与群通知接口；
   - 将模拟成员的回复、审批决策及任务变更转化为标准的不可变 `OrgEvent`，重新进入事件日志与投影器。

5. **设计有边界的 Agent Loop**：
   - 循环具有明确终止与等待条件（`ALL_RESOLVED`、`WAITING_RESPONSE`、`WAITING_APPROVAL`、`MAX_ROUNDS`、`DUPLICATE_BLOCKED`、`ESCALATED`、`NO_ACTION`）；
   - 输出确定性的 `AgentExecutionTrace`，保证同一场景重复运行得到完全一致的轨迹。

## 后果

正面影响：
- Agent 能够根据新反馈动态修正旧计划；
- 避免对同一成员就相同问题进行重复打扰；
- 高影响动作具备严格的授权门禁与可审计性；
- 所有交互轨迹可程序断言，便于回归测试与基准评测。

代价与限制：
- 本阶段依然运行在内存与 Mock 环境中，不接入真实平台 API 与真实异步网络；
- 成员回复与审批行为通过结构化脚本模拟，未引入自然语言 LLM 生成。

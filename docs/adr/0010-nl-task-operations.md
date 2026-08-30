# ADR-0010: NL Task Operations Behind Approval Gates

- 状态：Accepted
- 日期：2026-08-30
- 关联：ADR-0007（意图路由）、ADR-0009（真实集成可靠性）、docs/upgrade-plan-open-organization.md（柱 1 收官）

## 背景

M3-R 之后，意图层能准确识别 `task_create` / `task_reassign` / `deadline_change`，
但命中后只能回复「该链路将在下一里程碑启用」。这是封闭世界假设的最后一块：
系统仍只认识预设任务，PM 无法用自然语言开辟、转移或改期工作。本决策补上执行层，
完成第 1 期收官。

## 决策

1. **提案-审批-结算三段式，绝不直接执行**（治理红线）。LLM 在既有抽取调用中产出逐字槽位提案（`TaskProposal`：operation/title/owner_name/task_ref/deadline_expr，prompt 规则 7）；三类任务操作不再短路跳过 LLM，因为槽位需要模型抽取。TaskManager 对提案做接地校验（负责人必须存在于成员目录、任务必须存在、同名冲突检测、截止期按团队时区解析），然后生成审批卡等待 PM 确认；批准才落内核事件。**执行不是适配器命令而是事件**：创建落 `task.created`，改派落 `task.updated.owner_id`，改期落 `task.updated.deadline`，零旁表、可回放。
2. **角色门禁**：仅 pm/lead 可发起创建、改派与改期；成员请求一律拒绝并引导走 PM（拒绝即记录，不静默）。
3. **审批结算与协调案例分离**（延续 ADR-0008 的分离原则）：NL 任务提案不是风险驱动的拓扑案例，不进 CaseLedger。提案挂在 ApprovalManager（持久化旁表，与案例/审批同构），由 `GatewayService.settle_task_approvals` 在审批决策后结算——批准发事件并经 outbox 通知相关负责人；拒绝不落任务事件。改期复用 `PROPOSE_RESCHEDULE` 适配器契约，但以 `proposal_kind=deadline_change` 与 CaseLedger 的风险改期严格区分。
4. **改期前做依赖影响分析**：沿任务依赖反向遍历所有传递下游；新截止不早于下游截止时标记冲突并提升审批卡风险级别。批准后通知上游负责人和全部受影响下游负责人。
5. **确定性任务 ID**：新任务 ID 由 `task-{sha256(project|title)[:10]}` 生成——同名提案天然冲突检测，不同提案互不碰撞；DAG 无需任何改动即可渲染新任务。
6. **审批身份与重复提案安全**：审批命令只投递给服务端登记的 `approver_id`，不能投给拟任负责人；action/approval ID 同时包含提案参数与消息时间摘要，使同一消息重试幂等，而同一任务后续新提案不会覆盖旧记录。
7. **Mock 与真实同契约**：MockLLMClient 用与路由器一致的触发词和邻接纪律解析任务句式（「已交付」的裸「交」不得劫持改派——需成员紧邻关键词之后），离线评测与真实链路行为对齐。

## 后果

- 正：第 1 期四类高价值领导意图（指令/建任务/改派/改期）全部可执行；LLM 提案参数必经人工确认，幻觉参数无法落账；三类任务操作自动进入 DAG、时间线与通知链路。
- 负/已知边界：NL 创建暂不支持依赖与子任务（`dependencies=()`，后续里程碑开放）；成员请求只被拒绝不转达；依赖分析目前依据截止时刻做确定性冲突提示，不自动重排下游任务；三类任务操作消息各需一次槽位抽取调用（低频意图，成本可接受）。

# OrgPilot 架构说明

## 演进历程

- **P0 阶段**：建立确定性单向协调内核（事件 → 状态 → 依赖分析 → Case 构建 → Policy 裁决）。
- **M1 阶段**：建立 Mock 闭环协调 Agent（CaseLedger 状态机 + 三阶段 Action 抽象 + 审批门禁 + Mock Adapter 反馈 + 有界 Agent Loop）。

---

## M1 闭环协调架构

```text
发现风险事件 (OrgEvent)
        ↓
确定性状态投影 (OrgProjector)
        ↓
依赖影响分析 (DependencyAnalyzer)
        ↓
Case 账本对齐与生命周期 (CaseLedger)
        ↓
候选动作规划 (CoordinationAction)
        ↓
独立 Policy 检查 (PolicyEngine)
        ↓
审批状态机门禁 (ApprovalManager)
        ↓
确定性指令生成 (ActionCommand)
        ↓
适配器执行 (MockCollaborationAdapter)
        ↓
执行结果 (ActionResult) & 模拟反馈事件
        ↓
反馈事件重新注入 Agent Loop
        ↓
状态修正、Case 解决 / 取消 / 升级
```

---

## 核心模块与职责分工

| 模块 | 职责 | 核心组件 / 类 |
| --- | --- | --- |
| `events` | 版本化事件契约、不可变信封、反序列化、内存幂等事件日志 | `OrgEvent`, `InMemoryEventLog`, `parse_event` |
| `domain` | 领域模型、严格校验、枚举词汇、稳定领域异常 | `OrgState`, `CoordinationCase`, `ActionCommand`, `ActionResult`, `ApprovalRequest` |
| `state` | 严格按时序投影当前组织状态与证据链路 | `OrgProjector` |
| `dependencies` | 依赖有向图环路检测与多跳风险影响传播 | `DependencyAnalyzer` |
| `coordination` | Case 生命周期维护、状态对齐与消除、审批状态机、候选动作规划 | `CaseLedger`, `ApprovalManager`, `CoordinationService` |
| `policy` | 独立评估动作风险等级与审批门禁 | `PolicyEngine` |
| `adapter` | 协作平台统一接口、Mock 模拟与反馈事件注入 | `CollaborationAdapter`, `MockCollaborationAdapter` |
| `agent` | 有界单步/多轮 Agent Loop 编排与轨迹记录 | `CoordinationAgent`, `AgentExecutionTrace` |
| `scenarios` | 声明式 Ground Truth 加载、多轮交互回放与自动化断言 | `ScenarioLoader`, `ScenarioRunner`, `Evaluator` |

---

## M1 核心机制设计

### 1. CaseLedger 状态机与对齐消除 (Reconciliation)
Coordination Case 不再是单次临时生成的快照，而由 `CaseLedger` 进行持久化生命周期跟踪：
- `open` → `waiting_for_response` / `waiting_for_approval` → `approved` → `resolved` / `cancelled` / `escalated`
- 每轮循环自动对齐当前任务状态：
  - 任务自行恢复时，旧等待中的 Case 自动转为 `cancelled` 并记录原因，杜绝过时提醒；
  - 收到预计恢复时间时，Case 缺失信息消除并转为 `resolved`；
  - 超时未回复且达到重试阈值时，Case 自动转为 `escalated`，防止无限打扰。

### 2. 三阶段 Action 生命周期
严格区分不同阶段的数据对象：
- `CoordinationAction`：Planner 提出的候选动作及参数；
- `ActionCommand`：经 PolicyEngine 校验（及 ApprovalManager 审批）后的确定性执行指令；
- `ActionResult`：Adapter 执行后返回的结构化执行结果。

### 3. Human Approval 状态机
- 低风险动作（私聊询问）：自动放行执行；
- 高风险动作（任务修改、公开群通知）：必须获得人工审批；
- 防呆规则：未审批绝不执行、拒绝后禁止执行、过期审批禁止使用、审批凭证单次消费。

### 4. 确定性与有界 Agent Loop
- Agent Loop 具备明确的终止与等待状态判断（`all_resolved`, `waiting_response`, `waiting_approval`, `escalated`, `no_action`）；
- 单个场景重复执行产生 100% 字节一致的 `AgentExecutionTrace`。

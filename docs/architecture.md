# OrgPilot 架构说明

## 演进历程

- **P0 阶段**：建立确定性单向协调内核（事件 → 状态 → 依赖分析 → Case 构建 → Policy 裁决）。
- **M1 阶段**：建立 Mock 闭环协调 Agent（CaseLedger 状态机 + 三阶段 Action 抽象 + 审批门禁 + Mock Adapter 反馈 + 有界 Agent Loop）。
- **M2 阶段**：建立 LLM 结构化声明抽取引擎（Pydantic v2 Schema 契约 + 防幻觉引文反查 + 相对时间解析 + 20 样本 Gold Dataset 评测基准）。
- **P1 阶段**：建立生产级 SQL 异步持久化存储与 FastAPI 事件网关（SQLAlchemy 2.0 Async + SQLite/PostgreSQL 双驱动 + 幂等事件日志 + 状态瞬时恢复 + REST/Webhook 网关）。

---

## 全链路端到端协同架构

```text
HTTP / Webhook 请求 (Events, Messages, Approvals)
        ↓
FastAPI 事件网关 (`src/orgpilot/gateway/`)
        ├── /api/v1/projects/{id}/events (结构化事件摄入与流式查询)
        ├── /api/v1/projects/{id}/messages (自然语言聊天文本摄入 + 自动抽取)
        ├── /api/v1/projects/{id}/cases (Case 账本生命周期查询与详情)
        ├── /api/v1/projects/{id}/approvals (待审列表与批准/拒绝决策 Webhook)
        └── /api/v1/projects/{id}/run-turn (显式触发 Agent 协调轮次)
        ↓
LLM 声明抽取引擎 (`src/orgpilot/extraction/`)
        ├── ClaimExtractor (Pydantic v2 强约束输出)
        ├── GroundingVerifier (原文引文反查防证据幻觉)
        └── TemporalResolver (相对时间绝对化归一解析)
        ↓
SQL 异步持久化存储 (`src/orgpilot/storage/`)
        ├── SqlEventStore (基于 (project_id, event_id) 唯一索引与 SHA-256 哈希校验的 Append-only 幂等事件日志)
        └── SqlStateStore (OrgState / CaseLedger / ApprovalManager 快照持久化与跨进程瞬时恢复)
        ↓
确定性状态投影 (`OrgProjector`)
        ↓
依赖影响分析 (`DependencyAnalyzer`)
        ↓
Case 账本对齐与消除 (`CaseLedger`)
        ↓
候选动作规划 (`CoordinationAction`)
        ↓
独立 Policy 检查 (`PolicyEngine`)
        ↓
审批状态机门禁 (`ApprovalManager`)
        ↓
确定性指令生成 (`ActionCommand`)
        ↓
协作平台适配器 (`CollaborationAdapter` / `MockCollaborationAdapter`)
        ↓
执行结果 (`ActionResult`) & 模拟/真实反馈事件重新注入 Agent Loop
```

---

## 核心模块与职责分工

| 模块 | 职责 | 核心组件 / 类 |
| --- | --- | --- |
| `gateway` | FastAPI 异步事件网关、Webhook 路由、生命周期与 REST 服务 | `create_app`, `GatewayService` |
| `storage` | SQLAlchemy 2.0 异步数据库持久化、幂等事件表、状态与 Case 快照 | `Database`, `SqlEventStore`, `SqlStateStore`, `Base` |
| `extraction` | 自然语言消息结构化抽取、防幻觉反查、相对时间解析、评测基准 | `ClaimExtractor`, `GroundingVerifier`, `TemporalResolver`, `MockLLMClient` |
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

## 核心工程与算法机制

### 1. 生产级 SQL 异步双驱动持久化
- 采用 SQLAlchemy 2.0 Async 架构，生产支持 PostgreSQL（`asyncpg`），本地与 CI 零配置支持异步 SQLite（`aiosqlite`）；
- `SqlEventStore` 设立 `(project_id, event_id)` 唯一索引，结合 SHA-256 哈希校验：完全一致事件判定为重复并幂等返回，冲突 ID 但不同内容直接拒绝入库；
- `SqlStateStore` 支持状态、Case 账本与审批记录的持久化，实现服务重启后的瞬间精确重建。

### 2. 严格的 LLM 权限边界与防幻觉设计
- **LLM 只生成声明（Claim），绝不直接修改官方状态（State）**；
- **引文反查（Grounding Verification）**：强制要求 LLM 提供 `source_quote`，并在入库前严格反查是否属于原消息的真实子串，从机制上杜绝虚假证据；
- **时序锚定（Temporal Normalization）**：基于消息基准发生时戳精确解析“明天下午”、“后天晚上”、“周五前”等相对时间，保证时间戳带时区且绝对一致。

### 3. CaseLedger 状态机与对齐消除 (Reconciliation)
Coordination Case 由 `CaseLedger` 进行持久化生命周期跟踪：
- `open` → `waiting_for_response` / `waiting_for_approval` → `approved` → `resolved` / `cancelled` / `escalated`
- 每轮循环自动对齐当前任务状态：任务自行恢复时自动取消旧 Case；收到恢复时间时 Case 标记为 `resolved`；超时未回复达到阈值时自动升级。

### 4. 三阶段 Action 生命周期与 Human Approval 门禁
- 区分 `CoordinationAction`（候选）→ `ActionCommand`（指令）→ `ActionResult`（结果）；
- 高风险操作（任务改期、群通知）必须通过 `ApprovalManager` 人工审批，支持防篡改、防过期与凭证单次消费。

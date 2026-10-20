# OrgPilot 架构说明

## 演进历程

- **P0 阶段**：建立确定性单向协调内核（事件 → 状态 → 依赖分析 → Case 构建 → Policy 裁决）。
- **M1 阶段**：建立 Mock 闭环协调 Agent（CaseLedger 状态机 + 三阶段 Action 抽象 + 审批门禁 + Mock Adapter 反馈 + 有界 Agent Loop）。
- **M2 阶段**：建立结构化声明抽取边界（离线规则客户端 + 可选 AIHubMix Anthropic 兼容客户端 + GroundingVerifier + 相对时间解析）。
- **P1 阶段**：建立 SQL 异步持久化与 FastAPI 事件网关原型（默认文件型 SQLite，可配置 PostgreSQL；尚未完成多实例生产验证）。
- **F1 阶段**：建立飞书 OpenAPI、HTTP Webhook / WebSocket 长连接与 2.0 交互式卡片适配层；完整业务流程尚未在真实租户跑通，演示步骤见 `feishu-demo-runbook.md`。
- **意图层**（ADR-0008）：飞书私聊经 `MessageRouter` 三层处理（确定性事实 → LLM 单次结构化调用 → 确定性护栏），只产出 claim，不直接改状态；冻结提示词的评测结果见 `evals/intent/BASELINE.md`。
- **P-1 团队接入**（ADR-0009）：`team.yaml` 声明成员（邮箱或手机号经飞书通讯录换成 open_id）与任务，CLI 按三方比对导入差异；自动初始化收进 demo 模式；只有 `pm` 有特权。已在飞书测试企业中用单账号实测解析。

---

## 全链路端到端协同架构

```text
飞书桌面端 / 移动端 (Feishu Desktop / Mobile Client)
        │ (发送群聊/私聊消息 或 点击卡片【🟢 批准】/【🔴 拒绝】按钮)
        ▼
FastAPI 事件网关 (`src/orgpilot/gateway/`)
        ├── /api/v1/feishu/events (飞书 URL Challenge 握手、消息接收与卡片按钮回调)
        ├── /api/v1/projects/{id}/events (结构化事件流摄入与查询)
        ├── /api/v1/projects/{id}/messages (自然语言聊天文本摄入 + 自动抽取)
        ├── /api/v1/projects/{id}/cases (Case 账本生命周期查询与详情)
        ├── /api/v1/projects/{id}/approvals (待审列表与批准/拒绝决策 Webhook)
        └── /api/v1/projects/{id}/run-turn (显式触发 Agent 协调轮次)
        ↓
分层意图路由 (`MessageRouter` / `src/orgpilot/intent/`，飞书私聊与沙箱聊天的入口)
        ├── 第 0 层：消息幂等、群聊拒收、成员 / 角色、探针与待确认状态、显式命令
        ├── 第 1 层：意图客户端（启发式或 LLM，单次结构化调用）
        └── 第 2 层：护栏（截止日判定、置信度闸门、未知健康度不写 claim）→ 处理器写事件
        ↓
LLM 声明抽取引擎 (`src/orgpilot/extraction/`，`/messages` API 的旧路径，时间解析两条路径共用)
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
飞书适配器 (`FeishuCollaborationAdapter` / `src/orgpilot/feishu/`)
        ├── SEND_PRIVATE_INQUIRY -> 发送延期追问卡片
        ├── REQUEST_APPROVAL -> 发送 PM 原地交互式审批卡片 (持久化审批 ID)
        ├── UPDATE_TASK_DEADLINE -> 更新 OrgPilot 内的任务截止时间；`ORGPILOT_FEISHU_TASK_WRITEBACK=true` 时同时调用飞书 Task v2 OpenAPI（要求任务 ID 是飞书任务 guid），失败则升级协调事项
        └── POST_GROUP_NOTIFICATION -> 发送群广播通知卡片
        ↓
执行结果 (`ActionResult`) & 状态更新卡片原地渲染
```

---

## 核心模块与职责分工

| 模块 | 职责 | 核心组件 / 类 |
| --- | --- | --- |
| `feishu` | 飞书 OpenAPI 客户端、Token 自动刷新、2.0 交互式卡片渲染与 HTTP Webhook 解析 | `AsyncFeishuClient`, `MockFeishuClient`, `FeishuCollaborationAdapter`, `FeishuWebhookHandler` |
| `team` | `team.yaml` 解析校验、邮箱 / 手机号解析、导入差异计划（三方比对）与全有或全无写入 | `TeamSpec`, `plan_import`, `import_team` |
| `gateway` | FastAPI 异步事件网关、Webhook 路由、生命周期与 REST 服务；网关和独立长连接监听器共用同一套装配 | `create_app`, `build_gateway_service`, `GatewayService` |
| `intent` | 分层意图路由、意图客户端（启发式 / LLM / oracle）、护栏、对话状态、意图评测 | `MessageRouter`, `LLMIntentClient`, `DialogueStateStore` |
| `storage` | SQLAlchemy 2.0 异步数据库持久化、幂等事件表、状态与 Case 快照 | `Database`, `SqlEventStore`, `SqlStateStore`, `Base` |
| `extraction` | 自然语言消息结构化抽取、防幻觉反查、相对时间解析、离线评测与可选在线客户端 | `ClaimExtractor`, `GroundingVerifier`, `TemporalResolver`, `MockLLMClient`, `AnthropicCompatibleLLMClient` |
| `events` | 版本化事件契约、不可变信封、反序列化、内存幂等事件日志 | `OrgEvent`, `InMemoryEventLog`, `parse_event` |
| `domain` | 领域模型、严格校验、枚举词汇、稳定领域异常、可注入时钟 | `OrgState`, `CoordinationCase`, `ActionCommand`, `ActionResult`, `ApprovalRequest`, `Clock` |
| `state` | 严格按时序投影当前组织状态与证据链路 | `OrgProjector` |
| `dependencies` | 依赖有向图环路检测与多跳风险影响传播 | `DependencyAnalyzer` |
| `coordination` | Case 生命周期维护、状态对齐与消除、审批状态机、候选动作规划 | `CaseLedger`, `ApprovalManager`, `CoordinationService` |
| `policy` | 独立评估动作风险等级与审批门禁 | `PolicyEngine` |
| `adapter` | 协作平台统一接口、Mock 模拟与反馈事件注入 | `CollaborationAdapter`, `MockCollaborationAdapter` |
| `agent` | 有界单步/多轮 Agent Loop 编排与轨迹记录 | `CoordinationAgent`, `AgentExecutionTrace` |
| `scenarios` | 声明式 Ground Truth 加载、多轮交互回放与自动化断言 | `ScenarioLoader`, `ScenarioRunner`, `Evaluator` |

---

## 核心工程与算法机制

### 1. 飞书 2.0 交互式卡片与原地审批
- PM 审批通过富文本交互卡片呈现，按钮携带与数据库一致的审批 ID；
- 网关验证飞书 Verification Token，并校验操作者 `open_id` 等于指定审批人，随后推进任务改期和事件持久化。

### 2. 显式运行模式
- **事件通道**：默认飞书 WebSocket 长连接（网关进程内启动，或用 `orgpilot start-feishu-ws` 单独运行，二选一）；`ORGPILOT_FEISHU_USE_WS=false` 时改用 HTTP Webhook，需要 Verification Token；
- **协作适配器**：默认 `MockCollaborationAdapter`，只有设置 `ORGPILOT_COLLABORATION_ADAPTER=feishu` 并提供完整凭据才启用真实飞书调用；`start-feishu-ws` 在 Mock 适配器下拒绝启动；
- **模型客户端**：默认离线；`ORGPILOT_LLM_PROVIDER=aihubmix|deepseek` 启用在线抽取，`ORGPILOT_INTENT_CLIENT=llm` 让意图层使用 LLM（默认启发式）。

### 3. 时间来源
- 依赖"现在"的判断（审批过期、同步会话超时、追问 / 确认卡片的有效期、生成事件的时间戳）都从注入的 `Clock` 读取：生产用 `SystemClock`，测试和回放用 `FrozenClock`，同一场景在任何日期运行结果一致（`tests/test_clock_determinism.py`）；
- 有意保留系统时间的：飞书 token 过期判断、存储层的元数据时间戳、评测报告与录制文件的生成时间。

### 4. 改期提议的载荷约定
- `PROPOSE_RESCHEDULE` / `UPDATE_TASK` 的 payload 以 `new_deadline` 为唯一的截止时间键，另带 `task_title` 和 `impacted_tasks`（下游任务标题）供卡片展示；
- 适配器缺少 `new_deadline` 时返回失败，不会用当前时间代替。

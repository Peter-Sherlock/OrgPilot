# OrgPilot 架构说明

## 演进历程

- **P0 阶段**：建立确定性单向协调内核（事件 → 状态 → 依赖分析 → Case 构建 → Policy 裁决）。
- **M1 阶段**：建立 Mock 闭环协调 Agent（CaseLedger 状态机 + 三阶段 Action 抽象 + 审批门禁 + Mock Adapter 反馈 + 有界 Agent Loop）。
- **M2 阶段**：建立 LLM 结构化声明抽取引擎（Pydantic v2 Schema 契约 + 防幻觉引文反查 + 相对时间解析 + 20 样本 Gold Dataset 评测基准）。
- **P1 阶段**：建立生产级 SQL 异步持久化存储与 FastAPI 事件网关（SQLAlchemy 2.0 Async + SQLite/PostgreSQL 双驱动 + 幂等事件日志 + 状态瞬时恢复 + REST/Webhook 网关）。
- **F1 阶段**：建立飞书开放平台真实通信适配层与 2.0 交互式卡片（FeishuClient + FeishuCollaborationAdapter + 带按钮原地审批卡片 + Webhook/WebSocket 调度）。

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
飞书适配器 (`FeishuCollaborationAdapter` / `src/orgpilot/feishu/`)
        ├── SEND_PRIVATE_INQUIRY -> 发送延期追问卡片
        ├── REQUEST_APPROVAL -> 发送 PM 原地交互式审批卡片 (带回调 Token 按钮)
        ├── UPDATE_TASK_DEADLINE -> 调用飞书 Task v2 OpenAPI 更新截止时间
        └── POST_GROUP_NOTIFICATION -> 发送群广播通知卡片
        ↓
执行结果 (`ActionResult`) & 状态更新卡片原地渲染
```

---

## 核心模块与职责分工

| 模块 | 职责 | 核心组件 / 类 |
| --- | --- | --- |
| `feishu` | 飞书 OpenAPI 客户端、Token 自动刷新、2.0 交互式卡片渲染、Webhook 解析与长连接 | `AsyncFeishuClient`, `MockFeishuClient`, `FeishuCollaborationAdapter`, `FeishuWebhookHandler` |
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

### 1. 飞书 2.0 交互式卡片与原地审批
- PM 审批通过富文本交互卡片呈现，卡片内置带回调载荷与 Token 的【🟢 批准】与【🔴 拒绝】按钮；
- PM 在飞书桌面端点击按钮后，网关接收 `card.action.trigger` 回调，校验通过后卡片原地即时更新为完成态（展示审批人与时间），同时触发 Agent 推进任务改期与群通知。

### 2. 双通道与双模无缝切换
- **双通道**：生产环境支持标准 HTTP Webhook，本地开发与测试支持免公网 IP 的 WebSocket 模式；
- **双模客户端**：支持 `AsyncFeishuClient`（真实调用）与 `MockFeishuClient`（离线录制），CI/CD 与本地开发无需配置真实 App Secret 即可进行 100% 确定性自动化测试。

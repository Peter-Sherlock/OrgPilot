# 开发记录

## 2026-08-29：真机联调修复二——bootstrap 幂等与前端静默失败治理（推进计划第二步·下）

### 真机暴露的问题

用户重复点击"一键初始化"后项目再次砖化：`bootstrap-sandbox` 每次以时间戳生成事件 ID，二次初始化追加重复的 `member.registered`（`ou_pm already exists`）——又一处"先落库、后投影"路径；同时前端在 HTTP 500 时静默失败（聊天发送 `!res.ok` 直接 return，bootstrap 失败仍渲染"初始化成功"横幅），用户侧表现为"完全没有反应"。

### 已修复

- **bootstrap 幂等化**：改为确定性事件 ID（项目内唯一）；初始化前先读投影，已存在的成员/任务事件跳过（top-up 语义）；先内存投影、成功后落库。连续 N 次点击实测安全（首次 4 成员/3 任务，后续 0/0）。
- **飞书演示引导同类隐患**：成员已注册时跳过注册事件。
- **前端静默失败治理**：沙盒聊天与 bootstrap 的非 2xx 响现在渲染"系统错误（HTTP xxx），本次操作未生效"气泡并刷新状态，不再无声无息。

### 回归测试（1 个）

- 连续两次 bootstrap：第二次为 no-op，项目回放保持健康。

### 自测（真实 LLM）

三次 bootstrap 幂等通过；多人同步全流程 6 步全 200（探针扇出 3 成员、模糊回复自动追问、多轮槽位补全、状态健康）。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction (mock): PASS (20 samples, 100% F1)
pytest: 171 passed
coverage: 90.28% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

---

## 2026-08-29：真机联调修复——摄入原子性、成员自注册与传输故障隔离（推进计划第二步·下）

### 真机暴露的问题（分屏沙盒实测发现）

1. **事件库砖化**：未注册成员（沙盒 `ou_alice`）的消息经真实 LLM 抽取出健康声明事件**先落库、后投影**，投影器按域规则拒绝未知行动者并抛异常 → HTTP 500，但"毒事件"已持久化 → 此后该项目所有回放全部 500，看板全 undefined、任何消息无响应，连 bootstrap 也无法执行。
2. **适配器异常炸穿回合**：配置真实飞书适配器后，发往沙盒合成 ID 的私信被飞书 API 拒绝（400），异常从 agent loop 无隔离的 `adapter.execute` 一路炸穿整个 HTTP 请求。

### 已修复

- **摄入原子性**（`ingest_message`）：事件先在内存投影、成功后才写 SQL 事件库——域拒绝时持久日志保持完全可回放，项目不再可能被单条事件砖化。
- **成员自注册**：未知发送者的消息自动合成 `member.registered`（role=member，稳定 event id 幂等）再处理声明——真实飞书部署中任何新同事私聊机器人都应被 Agent 自认识，而不是引发崩溃。
- **传输故障隔离**（agent loop 四处适配器调用：询问、审批卡片、两处任务更新）：统一走 `_safe_adapter_call`——通道故障（飞书 4xx/5xx、网络）降级为日志告警，Case 生命周期照常流转，由既有超时升级机制兜底。
- 数据修复：清理 `feishu-project` 砖化数据（15 事件、1 Case、1 快照、1 同步会话）。

### 回归测试（2 个）

- 未注册成员消息全链路：自动注册 → 声明处理 → 项目回放保持健康；
- 适配器传输故障（全部抛 ConnectionError）：回合正常完成、Case 进入等待响应状态。

### 自测（真实 LLM，分屏沙盒同源 API）

全流程 6/6 步 200：沙盒初始化 → PM 发起同步（探针扇出 3 成员）→ Alice 模糊回复被自动追问时间点 → 补充时间后采集完成 → Bob 正常汇报采集 → 最终投影状态健康（3 任务 / 0 活跃 Case / 0 待审批）。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction (mock): PASS (20 samples, 100% F1)
pytest: 170 passed
coverage: 90.32% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

---

## 2026-08-29：AIHubMix 真实模型冒烟、时区缺陷修复与在线基准（推进计划第二步·中）

### 已实现

- **`eval-extraction --provider aihubmix|mock`**：在线模型基准一键运行（读取 AIHUBMIX_* 配置并计费调用）；离线 mock 基准保持确定性回归定位不变。
- **团队参考时区机制**：新增 `ORGPILOT_TIMEZONE`（默认 `Asia/Shanghai`，启动时做 IANA 校验），贯通 GatewayService → ProgressSyncCoordinator → MessageContext → 抽取提示词；相对时间（“明天下午5点”等）按团队时区解析并强制输出同偏移 ISO 时间；引入 `tzdata` 依赖保证 Windows 下 `zoneinfo` 可用。
- **提示词工程**（基于在线基准逐样本诊断，全部为通用规则而非针对样本过拟合）：
  - 任务实体对齐强制化：`task_id` 只能从已知任务列表逐字选取，口语表述对齐语义最近任务——修复模型编造 `payment_sdk`/`frontend_ui` 类 ID 被接地校验器整条丢弃导致的召回损失；
  - `delayed` vs `at_risk` 判定语言 sharpen（“要延到…/卡住必须等到…/N天后才能恢复”→ delayed）；
  - `on_track` 恢复类声明 `expected_completion` 置 null；仅给日期/天数未指明时刻按 18:00（与离线 `TemporalResolver` 约定一致）。

### 在线基准实测（gpt-5.6-luna via AIHubMix，20 样本）

| 指标 | 提示词强化前 | 强化后 |
| --- | --- | --- |
| Health Status F1 | 76.19% | **96.00%** |
| Recall | 66.67% | 100.00% |
| Precision | 88.89% | 92.31% |
| 任务 ID 准确率 | 75.00% | 100.00% |
| 时间槽准确率 | 66.67% | 83.33% |
| 误报率 | 0.00% | 0.00% |
| 接地率 | 100.00% | 100.00% |

基准门槛（F1≥90%、误报≤5%、接地=100%）由 FAIL 转 **PASS**；离线 mock 基准保持 100% 不变。README 已更新为实测数据。

### 真实模型时区缺陷（已修复）

首次冒烟发现“明天下午5点”被解析为 `T17:00:00+00:00`（UTC 墙钟直标），与团队实际时区相差 8 小时；根因是提示词将 UTC `occurred_at` 原样注入且未约束输出偏移。修复后实测输出 `2026-08-30T17:00:00+08:00`。注：开发机本地时区（UTC-4）与团队时区（Asia/Shanghai）不一致，因此设计为**可配置 IANA 时区**而非机器本地时区。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction (mock): PASS (20 samples, 100% F1)
orgpilot eval-extraction --provider aihubmix: PASS (20 samples, 96.00% F1)
pytest: 168 passed
coverage: 90.19% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

---

## 2026-08-29：多成员并发交互机制与真实飞书连接冒烟（推进计划第二步·上）

### 已实现（多成员并发三件套）

- **SyncSession SQL 持久化**（新表 `sync_sessions`）：进度同步会话（各成员探针状态、多轮澄清记录、已生成简报）随每轮交互落库，网关重启后自动恢复，进行中的散采-汇聚（scatter-gather）周期不再因重启孤儿化，成员重启后的回复不会丢失上下文。
- **回复处理前代理状态刷新**：`handle_sync_member_reply` 与 `run_agent_turn` 在决策前经 `_refresh_agent_from_store` 从事件库补拉事件、案例与审批，探针回复与普通消息摄入并发到达时不再基于过时投影做协调决策。
- **每项目回合锁**：`GatewayService` 以 per-project `asyncio.Lock` 串行化同一项目的协调回合，杜绝两名成员并发回复导致的重复追问或重复发消息。

### 新增回归测试（3 个）

- 同步会话跨网关重启恢复并完成收敛（文件级 SQLite 模拟真实重启）；
- 过时代理在回合前补齐事件与案例，不重复协调（两个先后物化的代理恰产生一次动作）；
- 并发双回合经锁串行化后仅产生一次协调动作（`asyncio.gather` 竞态回归）。

### 真实飞书凭证冒烟（只读验证，未发送任何消息）

- `tenant_access_token` 获取成功（真实凭证 + 真实网络出站）；
- 飞书官方 WebSocket 长连接真实建立：`connected to wss://msg-frontier.feishu.cn/ws/v2`，零公网部署路径全链路验证通过；
- `im/v1/chats` 返回权限不足（应用未开通 `im:chat:readonly` 读会话权限）——不影响收发消息主链路（机器人按来信者 `open_id`/`chat_id` 应答）；如需会话枚举可在飞书开放平台为应用补开该权限。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples, 100% F1, 100% Grounding)
pytest: 167 passed
coverage: 90.47% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

---

## 2026-08-28：修复与加固（推进计划第一步）

### 已修复

- `SqlStateStore.save_state` 调用点签名错误：进度同步成员回复持久化路径（`gateway/service.py`）与 `bootstrap-sandbox` 端点（`gateway/routes/coordination.py`）多传了 `project_id` 参数，执行即抛 TypeError；该两条路径此前无测试覆盖，Bug 由本次新增回归测试暴露。
- 同一同步回复路径上的第二个隐藏缺陷：`handle_sync_member_reply` 访问不存在的 `event_log.events` 属性（`InMemoryEventLog` 的公开访问器是 `all()`）。现改为幂等回灌完整内存日志，由 SQL 存储按 event_id 去重——即该路径自引入起从未跑通，本次修复后才真正可用。
- `SqlEventStore` 哈希时区表示缺陷：`occurred_at` 入库时统一规范化为 UTC，但内容哈希按事件原始时区偏移序列化计算，导致任何非 UTC 时区事件持久化后经重放读回再入库必然误报 `DuplicateEventConflict`。哈希现按 UTC 规范化瞬间计算，同一事件在不同时区表示下哈希一致。

### 已实现

- 新增 `ORGPILOT_DEMO_BOOTSTRAP`（默认 false）：飞书 Webhook 与 WebSocket 路径上的演示任务链自动注入及演示问候语改为显式 opt-in，生产部署默认不再注入演示数据；本地 `.env` 已开启以保留单人体验。`.env.example`、`docs/feishu-setup-guide.md`、`README.md` 已同步说明。
- 清理从未被系统产生的死枚举值：`AgentTerminationReason.MAX_ROUNDS/DUPLICATE_BLOCKED`、`CoordinationCaseStatus.APPROVED/EXECUTING`（含活跃状态集合引用）、`CommitmentStatus.AT_RISK/BROKEN`（注释标注预留至 M3 承诺风险跟踪实现时回归）、`SyncSessionStatus.TIMEOUT`、`ProbeMemberStatus.TIMEOUT`、`CommandStatus.FAILED/TIMEOUT`。

### 工程卫生

- `orgpilot.db` 运行时数据库从 git 跟踪中移除并加入 `.gitignore`；根目录个人文档（`/*.docx`）加入 `.gitignore`。
- 测试套件耗时结论：全部单测实际执行约 5 秒（`--durations` 逐项合计），无任何单测退化；墙钟波动（约 50s–330s）来自执行环境对文件 I/O（SQLAlchemy/aiosqlite 连接、模块导入收集）的可变开销，与仓库代码无关。8 月 26 日同套件曾记录 5.58s。
- 覆盖率门槛恢复：最近 5 次提交（飞书 WS 监听器、沙盒分屏聊天路由、简报卡片等）引入代码未同步补测试，分支覆盖率一度降至 87.61%（低于 90% 门槛）。本次针对性补齐后恢复至 90.23%。

### 新增回归测试（15 个）

- `bootstrap-sandbox` 端点状态初始化（HTTP 层，覆盖 save_state 修复）；
- 进度同步成员回复持久化快照与健康事件（服务层，覆盖上述两处 Bug 修复路径）；
- 事件时区回放去重（SqlEventStore，覆盖哈希修复）；
- 演示 bootstrap 默认关闭 / 显式开启（Webhook HTTP 层）；
- `ORGPILOT_DEMO_BOOTSTRAP` 环境变量解析；
- 沙盒分屏聊天全流程（同步启动 → 自主澄清 → 全员采集 → DAG 简报合成 → 普通消息）；
- 飞书同步意图触发进度同步会话；
- `build_executive_briefing_card` 风险/健康两种形态；
- `SlotCompletenessEvaluator` 四个未覆盖分支（关键词完备、无关键词追问、缺根因追问、双缺槽位合并追问、默认兜底）。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples, 100% F1, 100% Grounding)
pytest: 164 passed
coverage: 90.23% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

---

## 2026-08-26：W1 Web 全局可视化控制台与实时 DAG 拓扑看板

### 已实现

- 创建分支 `w1/web-dashboard-and-dag` 并编写 `ADR-0006`；
- 构建 DAG 拓扑计算引擎（`src/orgpilot/gateway/routes/dag.py`）：
  - `GET /api/v1/projects/{project_id}/dag`：自动计算拓扑分层（Topological Layers）、入度/出度、关键路径（Critical Path）与多跳风险传播高亮（Impacted Tasks）；
  - `GET /api/v1/projects/{project_id}/timeline`：聚合不可变事件、Case 状态转移、审批请求与执行审计，生成全链路可解释性时序时间线；
- 编写 Single-Page 现代化控制台 UI（`src/orgpilot/gateway/static/index.html`）：
  - 暗色现代化架构（Tailwind CSS），支持项目快速切换与 5s 自动轮询；
  - 原生可交互 SVG 渲染自适应 DAG 拓扑图，支持缩放/拖拽与节点点击抽屉（Node Inspector Drawer）；
  - 包含 PM 快速审批决策面板与自然语言消息摄入/轮次触发模拟台；
- 在 FastAPI 网关挂载根路径静态控制台（访问 `http://localhost:8000/` 或 `/dashboard` 即开即用）；
- 编写单元与端到端集成测试（`tests/test_gateway_dag_and_timeline.py`）。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples, 100% F1, 100% Grounding)
pytest: 144 passed in 5.58s
coverage: 93.76% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 192 files formatted)
```

---

## 2026-08-26：集成硬化与完成度校正

### 已修复

- 审批状态机严格校验指定审批人，拒绝请求体或卡片回调中的身份冒用；
- Agent 产生的任务更新事件写入 SQL 事件库，快照恢复后不会丢失已批准改期；引用未持久化事件的异常快照会被丢弃并完整重放；
- 消息事件 ID 改为基于上游消息 ID 或完整消息身份的稳定哈希，消除秒级碰撞；
- 增加显式 `OrgPilotSettings`，外部 LLM 和飞书适配器默认关闭，避免测试意外调用和计费；
- 接入 AIHubMix Anthropic Messages 兼容客户端，按内容块类型选择文本并执行 Pydantic 校验；
- 真实飞书适配器接入 GatewayService，Webhook 增加 Verification Token 与审批人 `open_id` 校验；
- 网关默认使用持久化 SQLite，并可通过 `ORGPILOT_DATABASE_URL` 配置 PostgreSQL；项目 REST API 支持可选 Bearer Token 门禁；
- 校正文档：WebSocket、日历、多维表格、在线模型准确率和生产级多实例一致性均不再被标记为已完成。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 个离线规则样本；不代表在线模型准确率)
pytest: 138 passed
coverage: 93.81% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

真实 AIHubMix 和飞书调用未执行，避免未经确认产生费用或外部副作用。

---

## 2026-08-26：F1 飞书开放平台适配层与交互式卡片原型

### 已实现

- 创建分支 `f1/feishu-integration` 并编写 `ADR-0005`；
- 编写《飞书开放平台极速配置指南》（`docs/feishu-setup-guide.md`），提供 2 分钟极速建应用与权限配置文档；
- 构建飞书 2.0 交互式卡片生成器（`src/orgpilot/feishu/cards.py`），支持延期追问卡片、带回调按钮的审批卡片、原地审批完成态卡片与群周知卡片；
- 实现统一飞书 OpenAPI 客户端（`src/orgpilot/feishu/client.py`）：
  - `AsyncFeishuClient`：管理 `tenant_access_token` 自动获取、TTL 提前刷新与消息/卡片/任务 API 调用；
  - `MockFeishuClient`：实现离线调用审计与 100% 确定性自动化测试。
- 实现 `FeishuCollaborationAdapter`（`src/orgpilot/feishu/adapter.py`），将 4 类 ActionCommand 转为飞书交互卡片与任务 API 更新；
- 实现飞书 Webhook 与事件调度器（`src/orgpilot/feishu/webhook.py`），处理 URL Challenge 握手、自然语言消息摄入与卡片按钮回调；
- 在 FastAPI 网关挂载 `/api/v1/feishu/events` 路由；
- 编写完整的单元与端到端集成测试（`tests/test_feishu_cards.py`, `tests/test_feishu_client.py`, `tests/test_feishu_adapter.py`, `tests/test_feishu_webhook.py`）。

### 当前验证结果

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples, 100% F1, 100% Grounding)
pytest: 122 passed in 2.23s
coverage: 94.76% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 93 files formatted)
```

---

## 2026-08-26：P1 SQL 异步持久化存储与 FastAPI 事件网关

### 已实现

- 创建分支 `p1/storage-and-gateway` 并编写 `ADR-0004`；
- 引入 SQLAlchemy 2.0 Async 现代声明式模型与异步会话管理（`src/orgpilot/storage/`）；
- 实现双驱动支持：生产环境支持 PostgreSQL（`asyncpg`），本地开发与测试原生支持异步 SQLite（`aiosqlite`）；
- 实现数据库级不可变追加日志 `SqlEventStore`，支持 `(project_id, event_id)` 唯一索引幂等与 SHA-256 哈希冲突校验；
- 实现 `SqlStateStore`，支持 `OrgState` 快照持久化，以及 `CaseLedger` 与 `ApprovalManager` 的数据库持久化与瞬时恢复；
- 构建 FastAPI 异步事件网关与 REST API（`src/orgpilot/gateway/`）：
  - `/api/v1/projects/{project_id}/events`（结构化事件摄入与流式查询）；
  - `/api/v1/projects/{project_id}/messages`（自然语言文本摄入、自动 ClaimExtractor 抽取与可选 Agent 轮次驱动）；
  - `/api/v1/projects/{project_id}/cases`（Case 查询与单 Case 详情）；
  - `/api/v1/projects/{project_id}/approvals`（待审批列表与批准/拒绝决策 Webhook）；
  - `/api/v1/projects/{project_id}/run-turn`（显式触发 Agent 协调轮次）；
  - `/api/v1/projects/{project_id}/state`（项目当前投影状态快照）。
- 编写完整的异步测试套件（`tests/test_sql_event_store.py`, `tests/test_sql_state_store.py`, `tests/test_gateway_api.py`）。

### 当前验证结果

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples, 100% F1, 100% Grounding)
pytest: 107 passed in 2.81s
coverage: 94.83% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 68 files formatted)
```

---

## 2026-08-26：M2 LLM 声明抽取与置信度评估

### 已实现

- 完成 M1 代码审查并合并至 `main`，发布 `m1.0.0` 标签；
- 创建分支 `m2/llm-claim-extraction` 并编写 `ADR-0003`；
- 建立 `src/orgpilot/extraction/` 结构化声明抽取模块；
- 定义 Pydantic v2 强类型输出契约（`ExtractedHealthClaim`, `ExtractedCommitment`, `ExtractionResult`）；
- 实现 `GroundingVerifier`，严格进行引文反查与上下文实体合法性校验；
- 实现 `TemporalResolver`，支持相对时间表达式向绝对 ISO 8601 时区时间戳的精确归一化；
- 实现 Provider-Agnostic `LLMClient` 协议体系（`MockLLMClient`, `RecordedReplayClient`）；
- 构建包含 20 个覆盖各类口语、假警报、多任务、相对时间、承诺与恢复样本的 Gold Dataset（`evals/extraction/gold_dataset.yaml`）；
- 实现 `orgpilot eval-extraction` 评测命令行工具并输出定量指标；
- 实现从非结构化自然语言文本到 M1 `CoordinationAgent` 闭环协调的端到端集成测试。

### 当前验证结果

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples)
  - Health Status Precision: 100.00%, Recall: 100.00%, F1: 100.00%
  - Task ID Accuracy: 100.00%
  - Slot DateTime Accuracy: 100.00%
  - False Alarm Rate: 0.00%
  - Grounding Valid Rate: 100.00%
pytest: 95 passed in 2.06s
coverage: 94.07% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 64 files formatted)
```

---

## 2026-08-26：M1 Mock 闭环协调 Agent

### 已实现

- 完成 P0 代码审查并合并至 `main`，发布 `p0.1.0` 标签；
- 创建分支 `m1/mock-coordination-loop` 并编写 `ADR-0002`；
- 引入持久化 `CaseLedger`，支持 8 种生命周期状态及自动化对齐消除（Reconciliation）；
- 规范三阶段 Action 生命周期（`CoordinationAction` 候选 → `ActionCommand` 指令 → `ActionResult` 执行结果）；
- 实现 `ApprovalManager` 审批状态机，严格防范未授权修改、过期审批、拒绝执行及重复消费；
- 实现 `MockCollaborationAdapter`，支持 4 种核心操作、调用审计与模拟反馈事件生成；
- 实现有边界的 `CoordinationAgent`（Agent Loop），支持可重复验证的确定性轨迹；
- 构建 5 个多轮交互场景（S1-S5），并通过 CLI 与自动化测试进行严格断言。

### 当前验证结果

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
pytest: 83 passed in 1.82s
coverage: 94.96% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 52 files formatted)
```

---

## 2026-08-26：P0 领域、事件与 Ground Truth

### 已实现

- 建立 `main` Git 基线提交，并创建 `p0/domain-events-ground-truth` 分支；
- 建立 `src/` Python 工程结构和锁定依赖环境；
- 分离任务正式 workflow 与来源支持的 health 状态；
- 实现六类严格、不可变、带时区的版本化事件；
- 实现进程内 append-only Event Log、幂等和 ID 冲突拒绝；
- 实现成员、任务、健康声明和承诺投影；
- 实现同一陈述者声明 supersession 和多来源冲突保留；
- 实现依赖图校验、循环拒绝、传递影响路径；
- 实现 Coordination Case、缺失信息识别和恢复时间询问候选；
- 将风险与审批判断放入独立 Policy Engine；
- 建立四个 YAML Ground Truth 场景和统一 CLI 回放入口。

### 当前验证结果

```text
orgpilot replay --all: 4/4 PASS
pytest: 38 passed
coverage: 95.75% branch-aware total coverage
ruff: All checks passed
```

# 开发记录

## 2026-10-11：意图识别第 1 步——schema、对抗评测集与现有系统基线（分支 `feat/intent-eval-baseline`）

### 已实现（按 ADR-0008 实施顺序第 1 步，不改任何运行时行为）

- **ADR-0008 落库**：从设计工作区复制 `docs/adr/0008-layered-intent-recognition.md`（意图分类、正交属性、三层路由、评测先行原则），作为意图层工作的依据；
- **意图输出 schema**（`src/orgpilot/intent/models.py`，仅定义未接入运行时）：`IntentType` 覆盖 ADR-0008 第 2 节全部 15 个意图（P0 九个 / P1 五个 / P2 一个）；`IntentItem` 含正交属性 `speech_act`/`polarity`/`certainty` 与槽位（task_id、health_status、原文 time_expression、带时区 expected_completion、blocker、必填 source_quote、confidence）；`IntentResult` 支持一条消息多个条目、answers_pending 与 reasoning；
- **对抗评测集**（`evals/intent/`）：固定项目夹具（1 PM + 3 工程师，5 个任务，含"支付SDK接入/支付全链路压测""收银台页面改版/商户后台管理端"两组易混淆名称、负责人与依赖）；dev 92 条 / test 53 条共 **145 条**，按类别分层（见下），每条含 id、category、message、sender、occurred_at(+08:00)、可选 pending_question/最近对话上下文，expected 分 behavior（claims/commitments/starts_sync/确认卡片/追问/回复类别）与 intents（意图 + 正交属性）两部分。样本按真实团队中文口语编写，未参考 `MockLLMClient` 关键词；
- **评测运行器与基线**（`src/orgpilot/intent/evaluator.py` + CLI `orgpilot eval-intent --split dev|test --mode mock|replay|live`）：被测系统协议为"一条样本 → 观测行为"；baseline 通过真实入口 `FeishuWebhookHandler` 驱动现有系统（内存 SQLite + mock adapter + 记录用 Feishu client），从事件库、同步会话与外发消息采集行为；带 pending_question 的样本先构造探针为 CLARIFYING 的活跃会话（updated_at 锚定样本时间，避开 F1 空闲超时）；指标含错误 claim 率、同步误触发率、claim P/R 与 task/健康度/时间准确率、回复类别准确率、多轮补全成功率及按类别统计；意图层指标对 baseline 记 N/A。报告输出 JSON + Markdown（含按类别表与期望/实际失败清单）至 `evals/intent/reports/`。本步不设通过门槛、不接 CI 门禁；replay/live 模式明确报"需意图层/在线模型，当前步骤仅支持 mock"；
- **LLM 兼容性脚本**（`scripts/llm_compat_check.py`，只写未运行、不入 CI）：三重门槛（provider=aihubmix、AIHUBMIX_API_KEY、--confirm-spend）不满足即退出；探测 (a) 工具调用 tool_use 块、(b) IntentResult schema 写入 prompt 的 N 次 JSON 解析/校验通过率、(c) 延迟 p50/p95、(d) token 用量；单元测试全部经 httpx MockTransport，未发生任何真实外部调用。

### 评测集类别分布（dev + test）

report_status 8、make_commitment 8、answer_pending 8（多轮回答）、query_status 8（疑问）、request_sync 8（同步正例）、acknowledge 8、chitchat 8、help 8、out_of_scope 8、negation 8、hypothetical 6、vague 6、multi_task 8、disambiguation 6、time_chinese 10、substring_trap 6、p1_p2 10、sync_near_negative 6、sync_non_pm 3、multi_turn_topic_shift 4；合计 145（dev:test ≈ 6:4）。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
pytest: 192 passed
coverage: 90.97% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
orgpilot eval-intent --split dev --mode mock / --split test --mode mock: 均运行并生成报告
```

### baseline 核心指标（无门槛，仅记录现状）

```text
dev (92 条):  claim P 47.06% / R 23.53%；task_id 58.82%；健康度 80.00%；时间 0.00%
              错误 claim 率 11.29%；同步误触发 0%；回复类别 34.78%；多轮补全 12.50%（8 条多轮）
test (53 条): claim P 0.00% / R 0.00%；task_id 20.00%；健康度 0.00%；时间 0.00%
              错误 claim 率 11.11%；同步误触发 0%；回复类别 26.42%；多轮补全 0.00%（8 条多轮）
失败最集中类别：时间表达解析（time_chinese 10/10）、P1/P2 误处理（p1_p2 10/10 产生副作用或答非所问）、
report_status/make_commitment/query_status 各 split 的措辞变体、多轮回答未合并进待补全 claim。
```

### 与任务说明的差异（如实标注）

- 分支基点：任务书写"从最新 origin/main 新建"，但上一轮受"不要 push"约束，F1/F2 合并只存在于本地 main（6bf22a2），origin/main 仍停在没有 F1/F2 的 7a9ed18；为满足"含 F1、F2 已合并"的前提，本分支自本地 main 新建；
- 回复类别在任务书示例的 7 类之外补充了 `clarify`（机器人追问轮次），示例列表为"例如"性质，已 在 schema docstring 注明；
- `sends_confirmation_card` 语义锚定 ADR-0008 §4 的"同步确认卡片"（自然语言同步须经确认），不涵盖改期审批卡片；
- 评测生成的报告文件随本分支提交（作为基线留档），后续运行会覆盖。

---


## 2026-10-11：修复飞书消息链路 4 个 P0 缺陷（分支 `fix/feishu-sync-routing-p0`）

### 已修复

- **B1 探针回复路径在网关层必然崩溃**（`gateway/service.py`、`coordination/sync_coordinator.py`）：
  - 根因：`handle_sync_member_reply` 读取了 `InMemoryEventLog` 上不存在的 `.events` 属性（只有 `all()`），并以错误签名调用 `save_state(project_id, state)`；`handle_member_reply` 丢弃 `run_turn` 返回值导致本轮生成的事件从未持久化；缓存的 coordinator agent 落后于数据库；
  - 修复：`handle_member_reply` 返回（收敛标志、澄清问题、本轮全部新事件）；`handle_sync_member_reply` 在每次处理回复前用 `get_or_replay_agent` 刷新 coordinator 的 agent 与 adapter，仅持久化本轮新事件，并经 `save_agent_state` 保存快照、cases 与 approvals；`bootstrap-sandbox` 路由同类的 `save_state` 签名错误一并改正；
- **B2 同步意图判断顺序错误且无角色校验**（`feishu/webhook.py`）：
  - 根因：关键词子串匹配排在"是否为探针回复"之前，探针成员回复含"当前进度"等词时误开新会话；任何人都可触发全员同步；
  - 修复：存在活跃会话且发送者在 `member_probes` 中时一律按探针回复处理；仅投影角色为 `pm` 的成员可触发同步，其余消息走普通抽取；关键词列表保持不变。sandbox-chat 路由存在同款顺序与角色缺陷，按相同策略修正；
- **B3 收敛回复被二次处理、重复投递不幂等**（`feishu/webhook.py`、`coordination/sync_coordinator.py`、`domain/sync_models.py`）：
  - 根因：`handle_sync_member_reply` 返回 converged 后未 return，同一条消息继续走 `ingest_message`，两条路径生成的 event_id 不同（sync 路径的 MessageContext 未带 source_ref），产生重复 claim；飞书重复投递同一 message_id 会重复累加 `turns_count`；
  - 修复：探针回复处理完一律 return，返回体区分 `clarification_sent` 与 `probe_collected`；飞书 message_id 作为 source_ref 贯穿 sync 路径，两条管线派生相同 event_id 由事件库去重；`MemberProbeState` 记录 `processed_source_refs`，重复投递不再计数、不再产生事件；
- **B4 未注册发送者写入"毒事件"导致项目永久不可用**（`gateway/service.py`、`feishu/webhook.py`）：
  - 根因：`ingest_message` 先把抽取事件写入事件库再投影，`_report_health` 校验 `unknown event actor` 抛出 `DomainInvariantError` 时事件已落库，此后每次重放必然失败；
  - 修复：抽取事件先在内存投影副本上整批校验，通过后才写事件库；校验失败返回显式错误结果（不抛未处理异常），`auto_run_turn=False` 同样满足；webhook 层对未注册发送者直接拒收（记日志 + 回复简短提示，不产生任何事件），未实现自动注册成员（留待产品决策）；`/messages` 与 sandbox-chat 将校验错误转译为 400 / message_rejected。

### 追加修复（同分支第二轮）

- **F1 仅等待中的成员按探针回复处理；同步会话增加超时**（`feishu/webhook.py`、`gateway/routes/coordination.py`、`coordination/sync_coordinator.py`）：
  - 根因：路由仅凭"发送者在 member_probes 中"判定探针回复，已 COLLECTED 的成员再发无关消息会被重新追问，探针状态从 collected 退回 clarifying；会话没有超时，只要有一人不回复就永远活跃；
  - 修复：仅探针状态为 PENDING / CLARIFYING 的成员消息按探针回复处理，webhook、sandbox-chat 与 `handle_member_reply` 内部防护三处一致；COLLECTED 成员的后续消息走同步关键词判断再走普通抽取；
  - `ProgressSyncCoordinator` 新增 `session_timeout`（默认 24 小时，可经构造参数与 `GatewayService(sync_session_timeout=...)` 配置），超时判断基于消息的 occurred_at（默认回退墙钟）：空闲超时后会话标记 `SyncSessionStatus.TIMEOUT`、未收敛探针标记 `ProbeMemberStatus.TIMEOUT`，用已收集信息合成简报（推荐项中列出未回复成员）并投递 PM，`get_active_session` 不再返回已超时会话；
- **F2 飞书适配器探针/追问消息签名错误**（`feishu/adapter.py`）：
  - 根因：`send_private_message` 文本分支调用 `client.send_message(receive_id=..., content=...)` 缺少必填 `msg_type`，且 content 传纯字符串，真实飞书中立即抛 TypeError，被 sync_coordinator 的 try/except 吞掉仅记日志，探针询问与多轮追问从未送达；
  - 修复：改为 `msg_type="text"`、`content={"text": msg_text}`；任一目标发送失败即返回 `CommandStatus.FAILED` 并附错误信息（记录审计日志），不再静默成功；成功仍返回 SUCCESS。

#### 追加测试

- `test_collected_member_is_not_re_probed_by_later_messages`：COLLECTED 成员在会话活跃期间的无关消息走普通路由，探针状态不回退、不再追问；
- `test_session_timeout_briefs_collected_info_and_routes_normally`：25 小时后消息触发会话超时，消息走普通路由，简报基于已收集信息生成并标注未回复成员，`get_active_session` 不再返回该会话；
- `test_feishu_adapter_sends_probe_and_clarification_text`：探针与追问以 `text` + `{"text": ...}` 结构送达；
- `test_feishu_adapter_reports_failed_when_text_send_raises`：发送失败返回 FAILED 且携带错误信息；
- sandbox 端到端测试补充 COLLECTED 成员后续消息走 `normal_turn` 的一致性断言。

#### 配套改动（非缺陷本身，如实说明）

- `tests/test_feishu_webhook.py`、`tests/test_gateway_api.py` 的固定时间基准（2026-09-10）与真实时钟的审批过期窗口冲突，4 个既有测试自 2026-09-12 起在 origin/main 上持续失败；改为锚定真实时钟并动态推导期望改期时间（独立 `test:` 提交）；
- origin/main 实测覆盖率 86.1%（低于 90% 门槛，开发日志中的 93% 早于 sync 与 ws 模块加入），为恢复门槛补充了 sandbox 端到端、简报卡片、dotenv/配置校验、`/messages` 拒收、审批决策错误分支与飞书 client 回退分支的测试（独立 `test:` 提交）。

### 新增测试（T1-T6 均为飞书 webhook 端到端，内存 SQLite + mock adapter）

- T1 `test_sync_probe_reply_persists_health_event_and_replays_consistently`：PM 发起同步、探针成员完整回复，事件落库且重放健康度一致；
- T2 `test_probe_reply_with_sync_keyword_is_not_treated_as_new_sync`：活跃会话中含同步关键词的回复按回复处理，不新建会话；
- T3 `test_non_pm_member_cannot_trigger_progress_sync`：非 PM 成员发"同步进度"不创建会话；
- T4 `test_converged_probe_reply_is_idempotent_per_message_id`：收敛回复仅产生一组 claim；同一 message_id 重复投递不改变 turns_count 与事件数；
- T5 `test_unregistered_sender_writes_no_events_and_pm_still_works`：未注册成员发风险消息零事件，PM 消息正常处理；
- T6 `test_sync_reply_persists_turn_outputs_and_survives_replay`：sync 路径 run_turn 生成的事件、cases 与 approvals 重放后仍在。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
pytest: 166 passed
coverage: 90.59% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

### 未覆盖与已知遗留（如实标注）

- `feishu/ws.py`（WebSocket 监听器内部逻辑）覆盖率仍约 47%，本次未扩展；
- `feishu/adapter.py` 澄清分支调用 `client.send_message(receive_id=..., content=...)` 缺少必填的 `msg_type`，属潜在缺陷，超出本次 4 个缺陷范围未改动、未覆盖；
- `bootstrap-sandbox` 路由的"重置"语义在二次调用时因 member.registered 幂等冲突会失败（事件 ID 含时间戳），属演示端点遗留问题，未改动；
- 真实飞书开放平台联调与真实 LLM 抽取未执行，避免未经确认的外部副作用。

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

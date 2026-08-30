# 开发记录

## 2026-08-30：真实飞书受控验收准备

### 定位

PR #1 审查修复后进入真实租户验收准备，但仍不把“配置了飞书适配器”视为发送授权。本阶段
先建立默认关闭的写入总闸、零写入预检和分级验收手册；未发送真实消息、卡片或任务更新。

### 已实现

- 新增 `ORGPILOT_FEISHU_ALLOW_WRITES=false` 默认门禁；关闭时真实客户端在任何网络写请求前
  fail-closed，全功能网关不启动飞书 WS、不处理业务 Webhook，也不清扫 outbox，避免历史待发送
  项被误投或误死信；HTTP URL verification 仍可安全完成。
- 新增 `orgpilot feishu-preflight`：离线检查适配器、凭证存在性、传输、演示数据与写入闸门；
  可选 `--online-auth` 只申请并隐藏 tenant token，不发送消息或更新任务。
- 修复独立 `start-feishu-ws` 仍使用默认 Mock 适配器的问题；现在与网关共享真实适配器工厂，
  并拒绝在写入闸门关闭时启动。
- 新增 `docs/feishu-live-acceptance.md`，将鉴权、单目标收发、任务/审批写入拆成独立授权层级。

### 当前边界

经用户明确授权，已完成 L1 在线鉴权：飞书官方接口成功签发 tenant token，命令只报告成功并
隐藏 token；未调用消息、卡片或任务更新接口。L2/L3 真实业务写入仍须分别取得对应授权。

### 门禁

280 项测试通过、分支覆盖率 90.56%、场景回放 9/9、离线评测 34 样本全部通过、Ruff 与
`git diff --check` 全净。本地 `.env` 已按授权将 `ORGPILOT_DEMO_BOOTSTRAP` 关闭；离线预检
五项全绿，在线鉴权随后通过，且 `ORGPILOT_FEISHU_ALLOW_WRITES` 继续保持默认关闭。

## 2026-08-30：M3 真正收官——自然语言改期闭环与审批身份修复

### 定位

复核上一笔“收官”实现时发现，`deadline_change` 仍只返回“下一里程碑启用”，并未进入
TaskManager；同时任务创建/改派审批请求虽然把 PM 记为 `approver_id`，审批卡命令却投递给
拟任负责人，真实飞书中会出现“收到卡的人无权批准、能批准的人收不到卡”。本项一次修完
这两个同属任务审批闭环的问题。

### 已交付

- **自然语言改期**：`deadline_change` 进入与建任务/改派一致的槽位抽取、接地、角色门禁、
  提案、审批、结算链；批准后落 `task.updated.deadline`，拒绝不改账。
- **依赖影响分析**：改期提案遍历全部传递下游；新截止不早于下游截止时列为冲突并把审批
  风险提升为 HIGH；审批卡展示影响列表，批准后通过 outbox 通知上游及下游负责人。
- **审批身份一致**：三类任务审批命令统一投递给服务端登记的提案人/审批人，不再误投拟任
  负责人；单测直接锁定 card target。
- **重复提案与时区修复**：action/approval/event ID 加提案摘要，后续新提案不覆盖旧记录；
  截止显示使用项目参考时区；修复“下周五”被解析成本周五的周偏移错误。
- **边界隔离**：NL 改期虽复用 `PROPOSE_RESCHEDULE` 适配器契约，但只结算
  `proposal_kind=deadline_change`；原 CaseLedger 风险改期审批仍由 Agent Loop 独立处理。
- **PR #1 审查收口**：Mock 改期提案只保留原文时间槽；outbox 成功重试保留累计次数并写入
  送达事件；催办端点改用项目登记的 PM/lead 且领域层拒绝非特权操作人；投影器拒绝目标不匹配
  的送达/失败事件；积压计数改为数据库 `COUNT(*)`，不再加载全部记录。

### 门禁

272 项测试通过、分支覆盖率 90.33%、场景回放 9/9、离线评测 34 样本
F1/任务/时间/接地/意图均 100%、False Alarm 0%、Ruff 与 `git diff --check` 全净。

经用户明确授权，将同一组 34 条版本库合成金标发送到 DeepSeek 官方 Anthropic 兼容接口，
`deepseek-v4-flash` 在线评测通过：健康状态 Precision/Recall/F1 100%，任务 ID 100%，
时间槽 100%，False Alarm 0%，接地 100%，意图 92.86%（门槛 ≥90%）。接入过程中捕获并修复：
DeepSeek V4 默认思考导致 `thinking-only` 响应；Anthropic 格式应使用
`thinking.type=disabled`，而非 Responses API 的 `reasoning.effort=none`；结构化输出偶发漂移
现纳入一次有界重试并只报告块类型/停止原因等安全元数据。未调用真实飞书公网接口。

## 2026-08-30：M3 收官——NL 任务创建与改派（升级方案·柱 1 完成）

### 定位

意图层已能准确识别 task_create / task_reassign，但命中后只能回复「下一里程碑启用」。本项补上执行层：PM 自然语言建任务/改派 → 提案 → 审批卡 → 确认 → 落内核事件 → DAG/通知全链路。当时记录为“四类全部可执行”，后续复核确认改期仍未接入执行层，已由上方“自然语言改期闭环”补齐。

### 已交付

- **TaskManager**（`coordination/tasks.py`，与 DirectiveManager 同构）：提案-审批-结算三段式；接地校验（负责人目录解析、同名任务冲突、截止期时区解析）；角色门禁（仅 pm/lead，成员请求拒绝并记录）；确定性任务 ID `task-{sha256(project|title)[:10]}`。
- **执行即事件**：创建落 `task.created`、改派落 `task.updated.owner_id`（投影器原生支持），零旁表可回放；通知经 outbox（新负责人必达，改派同时通知原负责人交接）。
- **抽取层**：`TaskProposal` 逐字槽位模型 + 提示词规则 7（接地红线：槽位必须逐字取自原文）；task_create/reassign 不再短路跳过 LLM（槽位抽取复用同一调用，不增调用次数）；Mock 用与路由器一致的邻接纪律解析任务句式——「已交付」的裸「交」不得劫持改派（e2e 当场抓出）。
- **门禁与结算**：任务提案挂 ApprovalManager（不进 CaseLedger，延续 ADR-0008 分离原则），`settle_task_approvals` 在审批决策后结算；飞书建任务/改派使用 `build_task_action_card`，改期使用风险改期卡；决策响应带 bot_reply 直接回显 PM 窗格。
- **踩坑记录**：审批请求最初只写进内存 agent 实例——摄入路径从未持久化 approvals store，`/approvals` 回放出的新 agent 拿不到提案；修复为提案落账即 `save_agent_state`。

### 门禁

259 项测试通过、覆盖率 90.14%、场景回放 9/9、离线评测 F1 100% + Intent 100%、ruff 全净。

## 2026-08-29：M3-R 真实集成可靠性（外部审查驱动，四轨修复）

### 定位

外部只读审查核证了架构与离线工程的质量（202 测试全绿、F1 100%），同时用运行日志实锤了集成链断层：飞书 `send_message` 缺 `msg_type`（真实送达为零）、改期字段三处错位且缺失时回退 `now()`、外发失败只记日志（账本谎报「已下达」）、歧义追问不存上下文、控制台审批身份写死必 403、LLM 同步调用阻塞事件循环 500、innerHTML 持久化 XSS。结论是插入 M3-R 阶段、暂缓 NL 建任务——采纳。

### 四轨修复（T1-T4，每轨独立提交）

- **T1 统一适配器契约**：`adapter/contracts.py` 单点解析 payload（Mock/真实同契约），规范键 `text`/`new_deadline`，必填字段 fail-closed（缺失截止期 → `CommandStatus.FAILED`，新增枚举值），13 项跨适配器契约测试。
- **T2 持久化 outbox**：`outbox` 表（幂等键唯一约束）+ `OutboxDispatcher`（先落账再发送、线性退避重试、死信、启动/周期清扫补发崩溃命令）；新增 `directive.delivered/delivery_failed` 事件投影到 `DirectiveState.delivery_status`，账本与真实送达对齐；送达事件 occurred_at 钳制在指令最后事件之后（回放按 occurred_at 排序，未来时间戳的事件曾把送达排到签发之前——单测抓出）；催办中继补独立幂等后缀（否则会被 outbox 去重吞掉）；`GET /outbox` 端点 + 控制台积压提示条。
- **T3 多轮指令闭环**：`directive.clarification_requested/resolved` 事件把澄清草稿持久化进投影状态（目标/任务/时间槽位 + 原文），发布者回复解析合并（「中午12点」合并回「明天上午12点」→ 正午 12:00），补全后按原上下文下达；支持「算了」取消；多条未确认指令按任务名/指令内容绑定，含糊回复列出清单反问；e2e 覆盖「提问→重启→应答→下达」全链路。
- **T4 韧性与身份**：LLM 抽取 `asyncio.to_thread` 下放 + 有界重试 + 熔断（开路快失败/半开探活）+ `LLMUnavailableError`，故障降级为友好提示而非 500；消息摄入全链路纳入项目锁（agent 回合拆已持锁内部方法防重入死锁）；审批身份由 `GET /context` 服务端下发（项目 PM 成员 id），替换写死的 `pm_web_operator`；前端全部动态 innerHTML 过 `esc()` 转义；WS 监听器补 `stop()`。

### 门禁

241 项测试通过、覆盖率 90.37%、场景回放 9/9、离线评测 F1 100% + Intent 100%、ruff check/format 全净；四笔提交（T1 契约、T2 outbox、T3 澄清、T4 韧性）+ 本文档笔。

## 2026-08-29：M3 第二项——指令执行链路（升级方案·柱 2）

### 定位

ADR-0007 解决了指令「识别」，本项补上「执行与传达」：PM 指令从聊天话语变成事件溯源的工作项——送达、确认、完成、催办、升级全链路闭环。

### 已交付

- **五个指令事件**：`directive.issued / acknowledged / completed / reminded / escalated`，投影到 `OrgState.directives`——重启恢复、审计回放、幂等去重复用既有事件账本，零新增存储表。
- **DirectiveManager**（独立于 CaseLedger，理由见 ADR-0008）：下达前槽位收敛（目标缺失/不存在 → 追问；唯一任务自动绑定、多任务 → 追问；「上午12点」歧义 → 追问正午/午夜；非特权角色 → 拒绝并引导走 PM）；回复拦截语义克制（仅显式"收到"确认、仅明确"完成"表述，其余回复原样走抽取管线）。
- **传达与升级**：adapter 新增 `SEND_DIRECTIVE` 动作（沙箱跨窗格通知与真实飞书私聊同路径）；每条消息摄入顺带超时清扫（默认 60 分钟自动提醒、1440 分钟向发布者升级，均可用 `ORGPILOT_DIRECTIVE_*_MINUTES` 配置）；PM 一键催办端点 `POST /directives/remind`。
- **可视化**：时间线新增指令事件解读（📩 下达 / ✅ 确认 / 🏁 完成 / ⏰ 催办 / 🚨 升级）；沙箱成员窗格新增「📩 确认收到指令」快捷按钮，PM 窗格新增「📣 催办未确认指令」。
- **修复**：指令相对时间解析锚点先转团队参考时区（UTC 存储时间戳不得作锚点，否则「明天下午5点」漂移 8 小时）——与抽取管线时区裁定对齐，附回归测试。
- **事故记录**：插入指令事件类时误截断 `CommitmentSupersededPayload` 的 `reason/replacement_commitment_id` 尾部字段，导致 replay 场景 9→3——被验证门当场逮住并修复；教训：在既有类后插入代码时 old_string 必须匹配完整类体。

### 真机验证（gpt-5.6-luna，gpt 路由 + 规则槽位）

下达（截止 08-30 17:00 正确、任务自动绑定【支付SDK接入】、Alice 窗格收到指令卡）→ Alice「收到，马上处理」确认（PM 窗格收到回执）→ 「支付SDK已经完成」完成（PM 收到 🏁）→ 催办空态报"没有待确认"；给 David 下达不确认 → 催办送达 David 窗格；时间线五类指令事件全部正确解读。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
orgpilot eval-extraction (mock): PASS (34 samples)
pytest: 202 passed (+15: 指令单测 12 + e2e/重启恢复/时区回归 3)
coverage: 90.15% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

### 已知边界

确认/完成判定基于关键词（否定/受阻词有否决保护）；同步会话进行中目标成员回复优先走探针路径，指令确认延迟（记录于 ADR-0008）；任务创建/改派的 NL 执行链（需审批门禁）为下一项。

---

## 2026-08-29：M3 第一项——角色感知意图路由层（升级方案·柱 1/柱 4）

### 背景与定位

升级方案获批后动工第 1 期首项。触发案例：PM 发「告诉Alice，必须在明天上午12点之前完成」，系统回复"无需要变更的任务状态"——抽取管线唯一认识"健康汇报"意图，指令/建任务/改派/截止期/提问全部静默丢弃，且每条消息无条件烧一次 LLM 调用。

### 已交付

- **意图路由层**（`extraction/intent.py`）：八类 `MessageIntent`，确定性规则先行 + 角色感知——同一句「延期到周五」，PM 说是指令（deadline_change），成员说是本人进度汇报（health_report）；特权角色（pm/lead）标注 `authority_ok` 供后续策略门禁；「我必须完成」自我指令守卫归为汇报；指令目标成员邻接检测（`告诉/让/要求 + 成员词元`、强制语气两种形态）。
- **成本分级**：规则自信的非汇报意图（指令/闲聊/建任务/改派/截止期/提问）直接短路，零 LLM 调用；汇报与不确定消息进既有 LLM 抽取调用，prompt 规则 6 要求同次输出 `intent` 字段兜底——**不新增第二次调用**。
- **实体提示随行**：`IntentResult.hints` 携带目标成员/涉及任务/原始时间表达（「明天上午12点」），为指令执行链路（下一里程碑）备好槽位原料。
- **网关呈现**：沙箱与 `/messages` 响应新增 `intent` 字段；指令类消息机器人明确回复"已识别为执行指令，链路下一里程碑启用"，不再"无需要变更的任务状态"式沉默。
- **评测闭环**：金标数据集 20→34 样本（+14 意图样本，含真机故障原话回归样本 s21）；新增 Intent Accuracy 指标并纳入通过门槛（≥90%）。
- **真实模型基准修复**：`temperature=0` 固定解码；prompt 规则 5 收紧两处边界语义（恢复时刻≠新交付里程碑；「N点前搞不定」的时间边界即预计完成）、规则 4 补「承诺不重复产出健康声明」——F1 96%→**100%**，意图准确率 **100%**（跨运行 92.9–100%，门槛内）。

### 真机验证（gpt-5.6-luna）

用户故障原话 → `intent=directive` + 明确机器人反馈；建任务/改派/闲聊（短路）/健康汇报（真 LLM 抽取成功）四类意图全部分类正确。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
orgpilot eval-extraction (mock): PASS (34 samples, F1 100%, Intent 100%)
orgpilot eval-extraction --provider aihubmix: PASS (F1 100%, Intent 92.9-100%)
pytest: 187 passed (+14: 意图路由 12 + 网关回归 1 + CLI/评测更新)
coverage: 90.62% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

### 边界说明（记录在 ADR-0007）

本期只识别与路由，不执行：指令下达/执行跟踪（directive 事件 + DirectiveCase + 审批卡片）为下一里程碑，界面上以"链路即将启用"明示而非静默丢弃；中文真实姓名尚未进入路由上下文（依赖 member_id 词元），规模化里程碑随成员资料扩展解决。

---

## 2026-08-29：真机联调修复三——同步闭环收敛与 DAG 可视化对齐（推进计划第二步·下）

### 真机暴露的问题

消息回复已通，但闭环仍未形成，用户反馈三点：

1. **同步会话永不收敛**：收敛条件是"全部探针 collected"，而演示链末端 QA David（`task-qa` 负责人）在分屏沙箱里**没有窗口**（原先靠"快速模拟"tab 代答），探针永远 `pending` → 简报永不合成，PM 收不到决策卡。且库中遗留两个僵尸 `clarifying` 会话（`save_sync_sessions` 为 upsert，重启后 `restore_sessions` 还会复活它们）。
2. **依赖拓扑空白 + 头部统计 undefined**：前端 `loadDag` 读 `dagData.total_tasks / health_distribution`，`renderDagSvg` 读 `n.id / n.level / n.health / edge.source / edge.target`，而后端 `DagResponse` 实际是 `summary.total_tasks / on_track_count...`、`task_id / layer / health_status / from_task / to_task`——整页字段错位，DAG 一个节点都画不出来。
3. **信息架构**：用户裁定"快速模拟与决策台"tab 不需要，成员模拟全部收进多角色分屏沙箱。

### 已修复

- **强制收敛路径**：新增 `POST /api/v1/projects/{id}/sync/complete`（PM 窗格「⏭️ 未响应成员直接出简报」按钮）——未响应探针标记 `no_response`（新枚举值），基于已回收信息立即合成简报，建议区点名未响应成员。
- **会话唯一活性**：发起新同步时自动把同项目旧活跃会话置为 `superseded`（新枚举值），僵尸会话不再累积、不再复活；`save_sync_sessions` 改为持久化协调器全部会话。
- **David 第 4 窗格**：分屏沙箱扩为 4 窗（`xl:grid-cols-4`），QA David 配快捷回复按钮；自动演练链补 David 回复步，一键跑完整闭环。
- **审批面板并入 PM 窗格**：随"快速模拟"tab 一并移除独立决策台，审批条紧凑版落位 PM 窗格下方，审批闭环不离开沙箱。
- **DAG/头部统计字段对齐**：`loadDag` 改读 `summary.*`，节点/边/抽屉全部改用后端真实字段（`task_id/layer/health_status/from_task/to_task/is_critical_path`，小写枚举值 `done`），简报卡补成员响应状态行与总结文本。
- 顺带修正：README 启动命令核对（`create_app --factory` 不带括号）。

### 回归测试（2 个）

- 强制收敛：Alice 已答、Bob/David 沉默 → complete 后两者 `no_response`、简报含未响应点名；无活跃会话时 complete 返回 `no_active_session` 而非崩溃。
- 会话接管：二次发起同步后旧会话 `superseded`，成员回复落入新会话。

### 自测（真实 LLM，全链路）

bootstrap(4 成员/3 任务) → PM 发起 → Alice 模糊触发追问 → 补时间收集 → Bob 正常 → David 回复即 `sync_completed`：简报 3 任务/2 正常/1 风险，拓扑风险正确识别 `task-payment → [task-checkout, task-qa]`；`/dag` 关键路径三层全对；二次同步 + 强制收敛验证 `no_response` 点名成立。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction (mock): PASS (20 samples, 100% F1)
pytest: 173 passed
coverage: 90.36% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

---

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

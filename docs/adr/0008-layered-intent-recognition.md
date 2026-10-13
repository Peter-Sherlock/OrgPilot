# ADR-0008：分层意图识别与评测先行

- 状态：Proposed（草案，未实现）
- 日期：2026-10-11

## 背景

当前的“意图识别”散落在三处，彼此没有协同：

1. `feishu/webhook.py` 中的同步关键词子串匹配；
2. “发送者是否被探针询问过”这一隐式会话判断；
3. 抽取器输出的 `is_actionable` 布尔值，不可处理的消息一律回复欢迎语。

实测暴露的问题：疑问句（“是不是延期了？”）、否定（“不会延期”）、假设（“如果还报错就要延期”）被当作状态汇报生成 claim；一句话多个任务只保留一条；追问的回答被当作新消息独立抽取，多轮澄清只能靠耗尽轮数“收敛”；关键词误触发全员同步。根因是缺少统一的意图层与对话状态，以及评测集与规则互相迁就，无法暴露上述问题。

## 决策

### 1. 本版范围

- **只处理私聊（p2p）**。群聊消息本版不处理，只记日志，后续单独设计（涉及旁听隐私与团队知情同意）；
- 意图按 P0 / P1 / P2 分期，本版只实现 P0 的处理逻辑；分类器识别全部意图，P1、P2 意图回复“已记录，暂不支持自动处理”，**不产生任何状态变更**，防止被误判为 P0 意图后产生副作用；
- 本版测试完成后再评估是否补充意图。

### 2. 意图分类

划分原则：意图粒度由“系统接下来做什么”决定，而非表面说法。

| 分期 | 意图 | 例子 | 系统动作 |
|---|---|---|---|
| P0 | `report_status` 汇报状态 | 支付 SDK 报错，明天下午 5 点前修好 | 抽取 health claim |
| P0 | `make_commitment` 作出承诺 | 我周四前提 PR | 抽取 commitment |
| P0 | `answer_pending` 回答追问 | 明天下午三点 | 合并进待补全的 claim |
| P0 | `query_status` 查询状态 | 支付 SDK 现在什么情况？ | 读取状态回答，不生成 claim |
| P0 | `request_sync` 请求全员同步 | 大家进展怎么样了 | 校验角色 → 确认卡片 → 发起同步 |
| P0 | `acknowledge` 确认 | 收到 / 好的 | 结合待确认项处理，否则忽略 |
| P0 | `chitchat` / `help` / `out_of_scope` | 你好 / 你能做什么 / 帮我订会议室 | 简短回复或引导 |
| P1 | `correction` 更正撤回 | 刚才说错了，是下周五 | 暂不支持 |
| P1 | `dependency_wait` 等待依赖 | 后端接口什么时候给我？ | 暂不支持 |
| P1 | `unavailability` 请假 | 我下周三请假 | 暂不支持 |
| P1 | `text_approval` 文字审批 | 同意改期 | 暂不支持 |
| P1 | `correct_bot` 纠正机器人 | 你理解错了，我说的是收银台 | 暂不支持 |
| P2 | `propose_plan_change` 计划变更 | 把验收推迟两天 / 这个转给小王 | 依赖 ADR-0007 的 PR 通道 |

正交属性（每个条目独立标注）：

- `speech_act`：`statement` / `question` / `hypothetical` / `imperative`；
- `polarity`：`affirmative` / `negative`；
- `certainty`：`certain` / `hedged`（“应该”“估计”“可能”）；
- 说话人身份由系统根据成员表确定，不由模型判断。

一条消息可包含多个条目，输出为列表。

### 3. 三层路由

**第 0 层：确定性事实（代码判定）**

- 卡片回调 → 审批处理（现有逻辑）；
- 非私聊 → 不处理；未注册发送者 → 拒收（已实现）；
- 显式命令：`/同步` 直接进入同步流程（仍校验角色），`/帮助` 返回帮助；
- 对话状态：若系统正在等待该发送者回复（探针 PENDING / CLARIFYING，或待确认的同步卡片），将“机器人上一个问题 + 目标任务”作为待回答上下文传给第 1 层。

**第 1 层：LLM 单次结构化调用**

- 输入：消息、发送者角色、发送者负责的任务、全部已知任务、待回答上下文、最近若干轮对话（**包含机器人自己的问题**）；
- 输出：`IntentResult`，包含条目列表（意图、任务、正交属性、槽位、`source_quote`）以及“是否在回答待回答上下文”；
- 时间槽位同时输出原文时间表达式与 ISO 时间；代码校验原文表达式出现在消息中，并在 `TemporalResolver` 可解析时交叉比对，不一致则降低置信度并追问。

**第 2 层：确定性护栏（代码判定）**

- grounding：`source_quote` 必须是原消息子串（现有）；
- 语气闸门：只有 `statement` 生成 claim；`question` 转为查询；`hypothetical` 不生成 claim；
- 角色闸门：`request_sync` 仅限 `pm` 角色；文字审批仅限指定审批人；
- 置信度门槛：低于阈值时追问，而不是执行；
- 副作用确认：自然语言触发的同步必须先经确认卡片；`/同步` 命令可直接执行；
- 降级：LLM 超时、报错或输出校验失败（重试一次后）时，回复“没理解，可以换个说法吗”，只响应显式命令，不改变任何状态。

### 4. 全员同步的触发

- 两种入口：`/同步` 命令；自然语言识别（`request_sync`）+ 确认卡片；
- 两种入口都仅对项目负责人（`pm` 角色）开放；非负责人的同步请求回复无权限提示，不发送任何探针。

### 5. 对话状态

- 每个探针维护一条“待补全 claim”；`answer_pending` 的槽位合并进该 claim，而不是独立重新抽取；
- 完整性判断基于合并后的 claim，而非当前一轮的抽取结果。

### 6. 消息级幂等

- 以飞书 `message_id` 为键，在调用 LLM **之前**去重；已处理的消息直接返回上次结果。避免模型输出不确定导致“同一 event_id、不同内容”的冲突。

### 7. LLM 调用工程要求（2026-10 修订：主候选 DeepSeek V4.1 Flash）

- 模型：**主候选 DeepSeek V4.1 Flash**（`deepseek-flash`），走 DeepSeek 官方 API 的 Anthropic 兼容端点 `https://api.deepseek.com/anthropic`（鉴权头 `x-api-key`）；AIHubMix 上的 `gpt-5.6-luna` 降为**可选对照**。两者共用同一个 Anthropic 兼容客户端族，可配置切换；
- **兼容性测试结论（已定，2026-10）**：采用**非思考模式 + 强制工具调用 `record_intent`（tool_choice=tool）+ temperature=0**。不采用思考模式，理由：思考模式下强制 tool_choice 返回 400；p95 延迟 23s 超过 20s 超时预算；max_tokens=4096 仍有 4/50 截断；输出 token 约为非思考的 5 倍。降级备选：schema 写入提示词 + 解析，仅在强制工具调用未返回 tool_use 块时使用一次；
- 思考开关保留为配置（`ORGPILOT_LLM_THINKING`：omit/disabled/enabled；aihubmix 默认 omit，deepseek 默认 disabled），但意图客户端固定 thinking=disabled，不随配置变化；
- 选型依据：兼容性测试 + **dev 集结果**；test 集只在选定模型与提示词之后运行**一次**；
- 数据流向：使用 DeepSeek 官方 API 时，团队消息只经过 DeepSeek 一方（AIHubMix 中转则另有一方）；无论哪条链路，正式进入企业使用前仍需完成合规评估；
- LLM 调用不得阻塞事件循环（异步客户端或 `asyncio.to_thread`），设置超时。

### 8. 评测先行

在实现第 1–3 层之前，先建立独立的意图评测集并对现有系统跑出基线：

- 位置：`evals/intent/`，与 `evals/extraction/` 分开；
- 样本覆盖：P0 各意图正例；否定、疑问、假设、含糊语气；一句多任务；相似任务名消歧；中文数字与相对时间（“三点”“5 点半”“下周三”“月底”）；子串陷阱（“没问题”）；P1、P2 意图不得产生 P0 副作用；同步触发的正例、近似反例与非负责人触发；多轮“回答追问”与“换话题”；
- 样本必须独立于现有规则关键词编写，避免评测与实现互相迁就；划分 dev / test，test 集不用于调优提示词；
- **评测以系统行为为准**（是否生成 claim、claim 内容、是否发起同步、是否追问、回复类别），使现有系统与新系统可直接对比；新系统额外评测意图层指标；
- 核心指标：错误 claim 率（不该生成却生成）、同步误触发率（反例上必须为 0）、意图准确率与 macro-F1、槽位准确率（任务、健康状态、时间）、多轮补全成功率；
- 运行模式：mock / 录制回放 / 在线。在线评测产生费用，须显式开启；在线结果录制为回放数据，保证 CI 确定性。

## 实施顺序

1. 意图 schema 与评测集；评测运行器；对现有系统跑基线（不改业务逻辑）；
2. LLM 兼容性测试（需确认费用后执行）；
3. 第 0–2 层路由、对话状态、消息级幂等、异步调用与降级；
4. claim 合并规则（按负责人或置信度加权）。

## 待决问题

1. 置信度门槛的取值，需在评测集上标定；
2. 最近对话轮数（上下文窗口）取多少，兼顾成本与指代消解；
3. 群聊支持的范围与隐私策略（后续单独 ADR）。

## 后果

正面影响：
- 疑问、否定、假设、多任务、追问回答等不再被误判为状态汇报；
- 有副作用的动作（群发探针）由角色闸门与确认卡片兜底，不依赖识别准确率；
- 评测以行为为准，改进效果可量化，模型可替换。

代价与限制：
- 每条私聊消息一次 LLM 调用，引入延迟与费用；
- 结构化输出的可靠性取决于中转端点的兼容性；
- P1、P2 意图本版只识别不处理，用户会收到“暂不支持”的回复。

## 实施记录

### 第 3a 步（2026-10-12，分支 `feat/intent-router`）：第 0–2 层、对话状态、消息幂等与 oracle 评测

模块结构：

- `intent/client.py`：`IntentClient` 协议 + `IntentContext`（消息、业务时区 occurred_at、发送者身份与任务、全部任务快照、待回答/待确认上下文、最近 N 轮对话，N 默认 6 可配置）；`HeuristicIntentClient`（包装 MockLLMClient，`ORGPILOT_LLM_PROVIDER=mock` 时的默认实现，效果不作考核）；`ScriptedIntentClient`（测试替身：预设结果，可模拟超时/异常/非法输出）；所有调用经 `classify_with_timeout`（`asyncio.wait_for`，默认 20 秒，`ORGPILOT_INTENT_TIMEOUT_SECONDS`），同步实现走 `asyncio.to_thread`；
- `intent/router.py`：`MessageRouter` 按固定顺序处理每条消息：消息幂等 → 群聊拒收（`group_not_supported`）→ 未注册拒收 → 同步探针回复（优先于命令与意图，延续 B2/F1 的决策）→ 显式命令（`/同步`、`/帮助`、未知 `/xxx`，不调用意图客户端）→ 构建对话上下文 → 意图分类（失败重试一次，仍失败回复"没理解……"并记 `fallback` 类别，不改任何状态）→ 护栏 → 处理器。每条消息只发一次回复，结果落消息表；
- `intent/dialogue.py`：`DialogueStateStore`，按（项目+发送者）存放 `PendingClarification`（问题、目标任务、待补全 claim、缺失槽位、已追问轮数、来源 probe/clarify）与 `PendingSyncConfirmation`（发起人、将被询问成员、创建时间）；过期时间追问 24 小时、确认卡片 30 分钟，均可配置。**本轮仅在内存中，重启后丢失**（见开发日志已知限制）；探针产生的追问同样登记在此，探针回复仍委托协调器收敛（保留简报与升级路径），probe 来源 claim 的合并统一留到第 4 步；
- `storage/message_store.py`：持久化 `processed_messages` 表（project_id、message_id、发送者、原文、意图摘要、回复类别、回复文本、响应、时间），message_id 已存在时直接返回上次结果，不重新识别也不重复发送；同一张表提供"最近 N 轮对话"；
- `intent/guards.py`：纯函数护栏，按 ADR 顺序执行 grounding → 任务 → 语气 → 含糊 → 置信度 → 时间 → 角色 → 分期；
- `intent/handlers.py`：各意图的处理逻辑；`extraction/event_builder.py` 把 ClaimExtractor 的事件构造抽成可复用函数（两条路径产生完全相同的事件 id 与载荷）；`GatewayService.commit_extracted_events` 复用"先内存校验再持久化再跑一轮"的既有路径；
- `intent/oracle.py` + `evaluator.LayeredSystemUnderTest`：oracle 客户端只在评测代码路径（CLI/评测器）被构造，运行时配置无法选中；置信度固定 0.95，`expected_completion` 取同任务行为标注；评测状态（待回答/待确认）一律通过 router 公开注册 API 构造，不触碰私有字段。CLI 新增 `--system baseline|layered --intent heuristic|oracle`（oracle 仅限 layered，报告标题标注"oracle：流水线上限，不代表模型效果"）。

实施中做出的设计决定（评审基准）：

1. **回复类别合并优先级**（一条消息多个条目时取最高）：`no_permission > sync_started > sync_confirm > clarify > claim_recorded > query_answer > noted > unsupported > help > smalltalk`；重新追问（R9 换话题）只置 asks_clarification 标志，不改回复类别；
2. **时间只比对日期**：`time_expression` 必须是原文子串；TemporalResolver 能解析时只比对解析日期与槽位日期，不一致才追问。原因：解析器不支持"半"和中文数字（"5点半""晚上八点"解析不出小时），按小时比对会对大量合法消息误追问；
3. **否定且无健康度的 report_status 直接丢弃**（"商户后台的接口我还没碰"断言不了任何状态），有健康度标注的否定按 R2 生成 claim；
4. **角色闸门只拦肯定句**：否定句的 request_sync（"不用同步进度了"）是拒绝而非请求，任何角色都按 noted 回复，不触发 no_permission；
5. **置信度闸门只作用于会产生 claim 的意图**（report_status / make_commitment / answer_pending）：闲聊、帮助等低置信度条目若也追问会造成每次寒暄都被反问；
6. **clarify 来源的追问合并答案时只补时间槽**：SlotCompletenessEvaluator 的"delayed 必须有阻塞根因"是探针卡片（追问卡点+时间）的规则；router 自己的追问（R6/R7）只问时间，按探针规则执行会对"大概要到下周三"这类合法回答误追问。probe 来源保留完整规则；
7. **欢迎/引导文本只出现在首次 bootstrap 与 `/帮助`**：原"不可处理一律回欢迎语"的行为删除，不可处理消息按 smalltalk 简短回复；
8. **`/messages` REST 接口保留为 legacy 低层抽取接口**（绕过意图路由直连 ClaimExtractor），docstring 已标注；webhook（含 WS）与 sandbox-chat 的重复路由逻辑（同步关键词列表、欢迎语兜底）已删除，统一走 MessageRouter。

评测结论（详见 `evals/intent/BASELINE.md`）：layered + oracle 在 dev 与 test 上均为 100% 通过，
未经确认群发、同步误触发、错误 claim 全部为 0；layered + heuristic 与 oracle 的差距即 3b 的模型误差空间。

### 第 3a.1 步（2026-10-12，分支 `feat/intent-router`）：探针回复统一接入新流水线

1. **路由顺序**：显式命令（`/同步`、`/帮助`、未知 `/xxx`）前移到探针捕获之前，且不消耗探针轮数——探针轮数只由"追问后收到回答"驱动；会话进行中 PM 重复 `/同步` 或确认新的同步，不再重复发起（`sync_already_active`），回复当前会话状态（发起时间、已回复/总人数，`SyncSession.progress_summary()`）；
2. **探针回复统一走意图流水线**：发出探针时为每个被询问成员登记 `PendingClarification`（来源 = probe），目标任务扩展为**列表**（该成员负责的全部任务，`task_ids`/`claims`/`missing` 均按任务维度组织）；删除"探针回复 → handle_sync_member_reply → ClaimExtractor"旧路径，探针回复与普通消息一样经过 意图分类 → 护栏 → 处理器（answer_pending 合并、R9 换话题等）；
3. **收集完成判定**：按任务逐个判定——on_track 无需额外槽位、at_risk 需完成时间、delayed 需完成时间和阻塞原因；无任务的整句回答（"都正常"）作用于全部目标任务；追问只列还缺信息的任务和槽位；达到最大轮数仍不完整 → 标记 `MemberProbeState.incomplete`（"信息不全"），简报 recommendations 首行如实体现；
4. **协调器职责收缩**：新增公开方法 `notify_probe_finished(project_id, member_id, complete=…)`，收集完成/超时/信息不全都经它通知；协调器只负责会话簿记、超时、收敛与简报合成，不再抽取；`handle_member_reply`、`service.handle_sync_member_reply` 与协调器内的 SlotCompletenessEvaluator 用法一并删除（完整性规则迁移到 `IntentHandlers._task_missing_slots`，与 SlotCompletenessEvaluator 同源，不留两套逻辑）；replay 场景直接驱动 CoordinationAgent 内核、不经过协调器，无需更新期望；
5. **多轮对话评测**：`evals/intent/conversations.yaml`（8 段 22 轮，覆盖任务书要求的 8 个场景）；`ConversationSystemUnderTest` 经真实 webhook 驱动，探针状态用公开 API（`router.start_sync` + `register_probe_clarifications`）建立真实会话；CLI `--conversations`；oracle 门槛=全部对话通过。会话时间锚定 `occurred_at`（`start_sync_session(occurred_at=…)`），保证固定时间评测下 24h 会话超时确定性；
6. **会话时钟与协同器刷新**：每条入站消息都会刷新协调器 agent 并触发会话超时检查（此前依赖探针分支）；超时简报/收敛简报基于刷新后的投影状态。

### 第 3b 步（上半）（2026-10-12，分支 `feat/intent-router`）：LLMIntentClient 接入 DeepSeek

1. **LLM 侧 schema 加固**：`health_status` 改为 `HealthStatus` StrEnum（JSON schema 出现 enum，合法集合与原校验器完全一致，评测行为不变——oracle dev/test 复跑 0 失败）；IntentItem/IntentResult 每个字段补充中文 description（含 expected_completion 必须带时区偏移的 ISO 8601、source_quote 必须原文子串、R11 只填完成时间等取值规则），随工具 schema 直接展示给模型；`reasoning` 移为 IntentResult 首字段并限定"不超过两句话"；LLM 边界归一化（每类计数）：丢弃幻觉字段、无时区的 expected_completion 按业务时区补齐，其余错误不修补、交给 Pydantic 驱动重试；
2. **LLMIntentClient**（`intent/llm_client.py`）：实现 IntentClient 协议，DeepSeek（官方端点）与 AIHubMix 皆可；异步 httpx、thinking=disabled、强制工具调用、temperature=0；提示词固定前缀在前（角色、意图分类表、正交属性、R1–R11 改写判定规则、输出要求；示例全部新写，与 dev/test/conversations 零重复），动态内容在后（当前时间与时区、发送者身份与任务、全部任务含截止日、待回答上下文、最近 N 轮、本条消息），以命中 DeepSeek 自动缓存，固定前缀哈希即 prompt 版本；校验失败把 Pydantic 错误摘要（loc+type）附在重试消息后再试一次，仍失败抛 `LLMIntentError` 走既有降级；强制调用未返回 tool_use 时执行一次降级（schema 写入提示词 + 解析文本块）；每次调用结构化日志（prompt 版本、延迟、input/output/cache_read tokens、stop_reason、是否重试/降级、校验错误、归一化计数、费用估算），不含 key；
3. **录制与回放**：在线调用把（请求哈希 → 响应）写入 `evals/intent/recordings/<prompt 版本>/`；replay 只读、缺失抛 `ReplayMissError`（FileNotFoundError 子类），router 对它**不再降级而是直接抛出**（缺失是基础设施错误，静默回放 fallback 会掩盖问题）；pytest/CI 只用 replay/mock；
4. **配置**：`ORGPILOT_INTENT_CLIENT=heuristic|llm`（默认 heuristic，mock 演示行为不变）；llm 要求 provider 为 deepseek/aihubmix；
5. **评测运行器**：eval-intent（单条与对话）新增 `--client llm`，在线需 `--live --confirm-spend` + 对应 key，否则必须 `--replay`；`--limit N`、`--max-spend-usd`（官方高峰价常数表估算，超出立即中止）；报告新增 llm_stats：schema 一次通过率、重试率、降级率、校验错误类型分布、延迟 p50/p95、每条消息 token 与费用、缓存命中占比；
6. **兼容性脚本顺手修复**：token 统计加入 cache_read_input_tokens；两个结构化探针记录每次校验失败的 loc+type（上限 50 条）。

### 第 3a.2 步（2026-10-12，分支 `feat/intent-router`）：追问次数封顶，mock 模式下探针能收敛

1. **追问总上限**：`PendingClarification` 新增 `reask_count`（因换话题而重新追问的计数），与 `asked_rounds`（追问后收到回答的轮数）分开计数。换话题重新追问最多 `ORGPILOT_MAX_TOPIC_REASKS`（默认 1）次，超过后回复不再附带追问（按新话题正常回复），待回答状态保留；`asked_rounds + reask_count` 达到 `max_clarification_rounds + max_topic_reasks`（默认 3+1=4）时**停止追问**——probe 来源经 `notify_probe_finished(complete=False, note="信息不全")` 通知协调器（与"多轮追问后仍不完整"同一处理），clarify 来源直接清除；
2. **换话题重问预算与总上限共用一个账户**：换话题轮（无论是否附带重问）与不完整回答轮都消耗同一总预算，任何混合序列（换话题+部分回答交替）都会收敛；探针本身的首次提问不计入（`asked_rounds` 从 0 起）；
3. **HeuristicIntentClient 最小兜底**（只为 mock 演示收敛，不追求准确率）：有待回答问题时，非命令、非明显闲聊（关键词表）、非单纯确认语（精确匹配表）的消息按 `answer_pending` 处理；任务默认取待答任务，多目标任务按消息提及优先（任务 id/标题完整包含直接命中，否则按字符 bigram 重叠判定"提到的那个"，无明确提及则任务置空、由合并逻辑作用于全部目标任务——安全失败模式：不虚构完成信息，只继续追问）；完成时间仍由 TemporalResolver 解析，未扩展解析能力；
4. **评测**：`conversations.yaml` 扩到 10 段 36 轮——conv_09 连续换话题（第 2 次换话题不再重问，第 4 次达到总上限标记「信息不全」收敛）、conv_10 成员始终不正面回答（第 5 轮含糊回复达到总上限后收敛）；conv_06 的放弃点从第 4 轮顺延到第 5 轮（总上限 3→4，标注同步修订）。CLI heuristic 门槛：**conv_01 必须收敛且完成时间正确**（`HEURISTIC_GATE_CONVERSATIONS`），其余对话只记录；oracle 门槛不变：全部对话通过（10/10，36/36 轮）。单条样本 oracle 门槛维持 100%（dev 102 / test 58），三组对比数字已在 BASELINE.md 按 3a.2 口径刷新（heuristic 多轮补全 dev 37.5%→62.5%、test 0%→50%，代价是错误 claim 4/64→6/65——含糊回复以 at_risk 兜底进 claim 的刻意取舍）。

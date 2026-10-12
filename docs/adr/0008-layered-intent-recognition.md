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

### 7. LLM 调用工程要求

- 模型：评测与上线继续使用 AIHubMix 上的 `gpt-5.6-luna`，经 `LLMClient` 抽象接入，可通过配置切换；
- 结构化输出策略由兼容性测试结果决定：端点可靠支持工具调用或 JSON schema 时优先使用；否则保留“schema 写入 prompt + 解析 + 校验失败重试一次”；
- LLM 调用不得阻塞事件循环（异步客户端或 `asyncio.to_thread`），设置超时；
- 经中转站调用意味着团队消息会经过第三方，正式进入企业使用前需完成合规评估。

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

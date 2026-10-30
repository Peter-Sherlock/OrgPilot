# 团队模拟器设计：用角色 Agent 构建闭环评测环境

> ⚠️ **未实现的设计稿。** 本文描述的 `simulation/` 包、世界模型、角色 Agent 和指标**在代码里都还不存在**，
> 文中的目录结构、类名和指标都是计划，不是现状。它是里程碑 M3（团队模拟器 + 对比实验）的输入，
> 2026-10-08 起草，2026-10-29 随生命周期场景一起入库，内容未随 main 的后续变化更新。
>
> 关联文档：`docs/full-lifecycle-scenario-design.md`（L1 剧本场景与已知缺陷）。缺陷编号以该文第九节
> 2026-10-29 的复核为准：原 D1 已拆成 D1a（追问拿到 ETA 即结案）和 D1b（终态 Case 占住槽位）。

## 摘要

用一组**模拟团队成员的角色 Agent**（Alice、Bob、David……）替代 YAML 剧本里写死的人类行为，
让 OrgPilot 在一个**闭环、带隐藏真实状态、时间会自然流逝**的环境里运行，并用统计指标衡量它的协调质量。

- OrgPilot 本身**一行不改**，它只通过 `CollaborationAdapter` 接触团队，不知道对面是模拟出来的；
- 世界持有每个任务的**真实进度**，OrgPilot 只能从成员说的话里推断——评测的核心就是两者之间的差距；
- 先用**规则驱动的角色**（确定性、零成本）搭起世界、调度和指标，再替换成 **LLM 角色**获得真实的语言多样性；
- 模拟负责**发现**问题，L1 剧本负责**固化**问题：失败的模拟轨迹导出成 YAML 场景，进入现有的缺陷棘轮。

---

## 一、背景与动机

### 1.1 剧本是开环的

L1 生命周期场景（`evals/lifecycle/e2e_01_payment_upgrade_lifecycle.yaml`）里，每个人类的发言都是提前写死的。
第 7 轮 OrgPilot 本该追问任务负责人 Bob，却问了报告人 Frank（缺陷 D2）——但第 8 轮 Bob 的回复照样到来，
因为剧本根本不关心 OrgPilot 问了谁。**错误没有后果，场景照常推进。**

剧本只能检查「OrgPilot 说了什么」，检查不了「OrgPilot 说的话有没有起作用」。

### 1.2 剧本测不到的三类东西

| 剧本的局限 | 模拟器补上什么 |
|---|---|
| 开环：人的回应与 OrgPilot 的行为无关 | **后果**：问错人就得不到回答，问得太频繁会被嫌烦，催得太急会被误升级 |
| 没有「真相」：只有说出来的话 | **隐藏真实状态**：Alice 实际落后 3 天但嘴上说「快好了」；可以衡量 OrgPilot 的认知和真实情况差多远、晚了多久才发现 |
| 语言是人写的、整齐的 | **语言多样性**：LLM 角色说出来的模糊、口语化中文，顺带把 L2（抽取 → 决策）的断层补上 |

### 1.3 定位与非目标

模拟器是**评测环境**，不是产品功能。

非目标：

- **不**把 OrgPilot 拆成多个角色 Agent 协作（MetaGPT / ChatDev 方向）。OrgPilot 的定位是「为人类团队做协调」，
  确定性内核、LLM 不直接改状态、人工审批门都是为了在替真人行动时值得信任，换成 Agent 团队自己干活会让这些设计失去意义；
- **不**模拟写代码这类实际工作，只模拟与协调有关的行为：汇报、回复、更新工单、审批；
- v1 **不**模拟 OrgPilot 的协调对项目进度的反作用（见原则 P5）。

---

## 二、设计原则（硬约束）

**P1 · OrgPilot 零感知。** OrgPilot 只能通过 `CollaborationAdapter`（`src/orgpilot/adapter/base.py`：
`send_private_message` / `request_approval` / `update_task` / `notify_group`）向团队发出动作，
只能通过与生产相同的入口接收输入（事件 + 消息抽取）。内核里不允许出现任何「如果是模拟」的分支，
否则测出来的结果不代表生产行为。

**P2 · 角色只看渲染后的内容。** 角色收到的必须是**真人会看到的东西**，而不是 `ActionCommand.payload` 原文。
这不是形式问题：当前内核发出的追问，payload 里写的是 `question: "When can this task recover?"`，
但飞书适配器在 payload 没有 `inquiry_text` / `clarification_text` 时会退回成一张通用询问卡，
只展示 `task_id`、`title`（内核 payload 不带 title，所以为空）和默认原因文案
（`src/orgpilot/feishu/adapter.py` 的 `send_private_message`）。**真人根本看不到那句问题。**
如果模拟角色直接读 payload，就会把真人收不到的信息算成 OrgPilot 的功劳。
实现时应把「命令 → 人看到的内容」抽成飞书适配器与模拟适配器共用的渲染函数，防止两边漂移。

**P3 · 真相与观测分离。** 世界持有每个任务的真实状态；角色知道自己负责的部分；OrgPilot 只能看到消息和工单。
评测比较的是 OrgPilot 的认知与世界真相，而不是 OrgPilot 的输出与预先写好的期望。

**P4 · 可复现。** 同一个世界剧本 + 同一个 seed 必须产生逐字节相同的轨迹。
不读墙上时钟（只用 `FrozenClock`），随机数按成员派生独立的随机源，LLM 调用通过录制回放固定下来（见第八节）。

**P5 · 世界外生（v1）。** 任务的真实完成时间由世界剧本决定，不受 OrgPilot 的协调影响。
唯一的例外是**官方 deadline**：OrgPilot 的改期经审批后写入工单系统，世界照此更新官方 deadline。
这个简化让 v1 能清楚地衡量「感知与沟通」，代价是暂时无法衡量「协调是否让项目更早完成」。

**P6 · 先规则、后 LLM，同一接口。** 规则角色和 LLM 角色实现同一个 `Persona` 接口，
世界、调度、指标不感知是哪种实现。

---

## 三、总体架构

```
┌──────────────────────────── 世界（World）────────────────────────────┐
│  FrozenClock          时间只由调度器推进                              │
│  TrueTask[]           每个任务的真实进度、真实完成时间、阻塞原因      │
│  TicketSystem         官方工单：workflow 状态、官方 deadline          │
│  Incidents            按时间触发：延期、阻塞、请假、失联、回退        │
│                                                                      │
│  Persona: Alice  Bob  David  Emma  Frank  Grace  Carol(PM)           │
│    = 画像 + 行为特征 + 记忆 + 独立随机源                             │
└───────┬──────────────────────────────────────────────▲──────────────┘
        │ 人类动作：说话 / 更新工单 / 审批决定           │ 渲染后的私聊、卡片、群通知
        ▼                                              │
  ┌──────────────┐   oracle 模式：意图 → 结构化事件     │
  │  输入通道     │   抽取模式：原始中文 → ClaimExtractor │
  └──────┬───────┘                                      │
         ▼                                              │
  CoordinationAgent.run_turn(events, clock.now())  ──→  SimulatedTeamAdapter
  （生产内核，未修改）                                  （实现 CollaborationAdapter）
         │
         ▼
  MetricsObserver：每次 OrgPilot 回合后，对比 OrgPilot 的认知与世界真相
```

| 组件 | 职责 | 依赖的现有代码 |
|---|---|---|
| `World` | 真实状态、工单系统、事件注入 | `FrozenClock`（`domain/clock.py`） |
| `Persona` | 决定说什么、什么时候说、要不要回复 | — |
| `Verbalizer` | 规则角色的「意图 → 中文」模板 | — |
| `SimulatedTeamAdapter` | 接住 OrgPilot 的动作，渲染后投递给对应角色 | `CollaborationAdapter` |
| 输入通道 | 把人类动作变成 OrgPilot 的输入 | `ClaimExtractor`、`MessageContext`、事件模型 |
| `Scheduler` | 离散事件调度，推进时钟 | — |
| `SimulationRunner` | 组装以上组件、驱动主循环 | `CoordinationAgent` |
| `MetricsObserver` | 采样并计算指标 | `OrgState`、`CaseLedger` |

---

## 四、核心模型

### 4.1 真实状态

```python
class TrueTask:
    task_id: str
    owner_id: str
    true_start: datetime
    true_finish: datetime          # 当前「真实」的完成时间，会被 incident 修改
    blocked_until: datetime | None
    block_cause: str | None
    reopened: bool                 # 是否发生过回退
```

任一时刻的**真实健康状态**由世界推导，不由任何人声明：

| 条件 | 真实健康 |
|---|---|
| 已完成 | `on_track` |
| `true_finish` > 官方 deadline | `delayed` |
| 被阻塞，或 `true_finish` 距官方 deadline 不足一个工作日 | `at_risk` |
| 其他 | `on_track` |

工单系统（`TicketSystem`）是世界的一部分，保存**官方**的 workflow 状态和 deadline。
成员更新工单（受「工单习惯」特征影响，可能延迟或忘记）；OrgPilot 只能经审批后改 deadline。
「真实已完成、工单还是 doing」这种不一致，本身就是 OrgPilot 应该发现的信号。

### 4.2 事件注入（Incident）

| 类型 | 含义 | v1 的效果 |
|---|---|---|
| `slip` | 真实完成时间推后 Δ | 修改 `true_finish`，附带原因 |
| `block` | 在 [t1, t2] 阻塞 | 设置 `blocked_until` |
| `leave` | 成员在 [t1, t2] 不可用 | 不回复、不更新工单 |
| `unresponsive` | 成员在 [t1, t2] 回复概率骤降 | 覆盖其回复概率 |
| `regression` | 已完成任务被打回 | 工单回到 doing，`true_finish = t + Δ` |

v1 中事件的影响**全部显式声明**：请假不会自动推导出任务延期，需要剧本同时写一条 `slip`。
这样每个结果都能追溯到剧本里的某一行。

### 4.3 角色（Persona）

```python
class Persona(Protocol):
    member_id: str

    def on_tick(self, now: datetime, view: PersonaView) -> list[HumanAction]:
        """主动行为：站会汇报、发现自己的任务出问题时主动说、更新工单。"""

    def on_message(self, msg: RenderedMessage, now: datetime, view: PersonaView) -> list[HumanAction]:
        """收到 OrgPilot 的私聊或卡片后的反应（可能不回）。"""

    def on_approval_request(self, req: RenderedApproval, now: datetime, view: PersonaView) -> list[HumanAction]:
        """只有审批人会收到。"""
```

`PersonaView` 是角色能看到的世界切片：自己负责的任务的全部真相、自己观察得到的任务（剧本中的 `observes`，
例如 QA 能看到上游接口有没有交付）、群聊历史、发给自己的消息。**角色看不到 OrgPilot 的内部状态。**

### 4.4 行为特征

特征照着真人的常见毛病设计，每一条都对应 OrgPilot 需要具备的一种能力：

| 特征 | 参数 | 真人表现 | 检验 OrgPilot 的什么 |
|---|---|---|---|
| 响应性 | 回复概率、回复延迟分布（中位数 + 离散度） | 有人秒回，有人半天才回 | 超时与升级是否合理 |
| 主动性 | 情况变差时主动汇报的概率 | 有人主动报风险，有人只在被问时才说 | 能否发现「没人说话」的问题（D3） |
| 乐观偏差 | 报出的 ETA 比真实早多少；是否把 delayed 说成 at_risk | 「快好了」 | 能否识别不实的汇报 |
| 模糊度 | 用模糊时间表达的概率 | 「大概周四吧」「下周初」 | 多轮澄清能否问出确切时间 |
| 工单习惯 | 真实完成后多久更新工单、是否会忘 | 活干完了，状态还是 doing | 能否发现工单与实际不一致 |
| 越界汇报 | 报告别人任务的概率 | QA 说「Bob 那边肯定来不及」 | 能否追问到正确的人（D2） |

审批人（PM）额外有一条**审批策略**：例如「延期 ≤ N 天且不在关键路径上就批准，否则拒绝并给出理由」，以及审批延迟分布。

### 4.5 人类动作

```python
HumanAction = (
    Say(channel: Literal["group", "dm"], to: str | None, intent: Intent, text: str | None)
    | UpdateTicket(task_id: str, to_status: WorkflowStatus)
    | DecideApproval(approval_id: str, decision: ApprovalStatus, reason: str | None)
)
```

关键设计：**角色先决定意图（`Intent`，结构化），再把意图说出来（`text`）。**
规则角色由 `Verbalizer` 把意图套成中文模板；LLM 角色在一次输出里同时给出意图和原话。
同一个意图有两种进入 OrgPilot 的方式，正好构成一组消融实验（第六节）。

```python
class Intent:
    kind: Literal["health_report", "eta", "commitment", "decline_scope", "smalltalk"]
    task_id: str | None
    health: HealthStatus | None
    eta: datetime | None
    blocker: str | None
```

`decline_scope` 是「这不归我管，你问 Bob」——问错人的直接后果。

---

## 五、仿真循环

采用**离散事件调度**：一个按时间排序的优先队列，元素是待发生的模拟事件。时钟每次跳到下一个事件的时间点。

| 模拟事件 | 触发来源 | 处理 |
|---|---|---|
| `IncidentFires` | 世界剧本 | 修改真实状态 |
| `PersonaTick` | 每个工作日固定时间（站会）+ 低频随机唤醒 | 调用 `on_tick` |
| `Deliver` | OrgPilot 发出的动作 | 调用对应角色的 `on_message` / `on_approval_request`；角色若决定回复，按其延迟分布排入一个 `HumanActionArrives` |
| `HumanActionArrives` | 角色的动作 | 消息进入 OrgPilot 收件箱；工单更新写入工单系统并产生事件；审批决定交给 `ApprovalManager` |
| `Heartbeat` | 固定间隔（如工作时间内每小时） | 触发一次 OrgPilot 回合 |

```python
while scheduler and clock.now() <= world.end:
    sim_event = scheduler.pop()
    clock.set(sim_event.at)
    handle(sim_event)                      # 可能往队列里推新事件、往收件箱放输入

    if isinstance(sim_event, Heartbeat) or (inbox and config.turn_on_input):
        events = input_channel.drain(inbox)              # oracle 或 ClaimExtractor
        trace, _ = agent.run_turn(events, clock.now())   # 生产内核
        for delivery in adapter.drain_outbound():        # OrgPilot 本回合发出的动作
            scheduler.push(Deliver(at=clock.now(), payload=delivery))
        metrics.observe(clock.now(), world, agent, trace)
```

几个细节：

- **两种回合触发**：有输入就跑一回合（与网关「收到消息即跑一轮」一致），另加固定心跳。
  当前内核在空回合里只会检查超时；缺陷 D3 要补的「时间驱动」能力，就是在心跳回合里主动扫描 deadline 和承诺，
  **这个模拟器正好是开发 D3 的试验场**；
- **工作时间**：角色只在工作时间行动，落在非工作时间的回复顺延到下一个工作时段开始；
- **审批**：PM 的审批决定直接调用 `ApprovalManager.approve` / `reject`，与网关的审批接口和飞书卡片回调走同一个状态机；
- **可调的内核参数**：内核的 2 小时回复超时是 `CoordinationAgent` 的构造参数，但「最多重试几次」在 `run_turn`
  里写死成 `max_response_rounds=1`。要对这些参数做扫描实验，需要先把后者也改为可配置——
  这是一处**不改变默认行为**的小型内核改动，属于 S0 的前置工作。

---

## 六、两种输入通道（消融实验）

| 模式 | 人类动作如何进入 OrgPilot | 衡量的是什么 |
|---|---|---|
| **oracle** | `Intent` 直接转成结构化事件（`task.health_reported`、`commitment.made`……），相当于「完美抽取」 | 纯协调能力 |
| **extractor** | `text` 经 `ClaimExtractor.extract_from_message(text, MessageContext(...))` 抽取，抽不出来就丢失 | 端到端能力 |

同一个世界、同一个 seed 分别跑两种模式，**指标之差就是抽取环节造成的损失**。
抽取模式下失败的原话会被单独导出，可以直接补进 `evals/extraction/gold_dataset.yaml`，
让抽取评测集随模拟持续扩充。

注意：当前 `ClaimExtractor` 默认使用规则驱动的 `MockLLMClient`。规则角色的中文模板如果写得太「标准」，
测到的只是模板和规则之间的匹配度；因此模板必须包含模糊时间、口语化表达和不提任务名的说法，
真实的语言多样性要等 S2 的 LLM 角色。

---

## 七、评测指标

指标分三组，全部按 seed 多次运行后报告分布（中位数、P90、置信区间），而不是单次结果。

### 感知：OrgPilot 知道得对不对、及不及时

| 指标 | 定义 |
|---|---|
| **发现时延** | 真实健康状态变差的时刻 → OrgPilot 认知的健康状态变差或开出 Case 的时刻，按工作小时计 |
| **漏检率** | 直到真实 deadline 被突破或问题自行消失，OrgPilot 都没有发现的事件比例 |
| **认知偏差** | 每次心跳采样，OrgPilot 认知的健康状态与真实健康状态不一致的比例；区分**低估**（以为比实际好，危险）和**高估** |
| **ETA 误差** | OrgPilot 掌握的预计完成时间与 `true_finish` 的绝对误差 |
| **静默事件发现率** | 当事人从不主动开口（主动性为 0）的事件中，被 OrgPilot 发现的比例。在 D3 实现之前预计接近 0，作为基线 |

### 沟通：问得对不对、打扰多不多

| 指标 | 定义 |
|---|---|
| **问对人率** | OrgPilot 的追问中，接收者是任务负责人或剧本声明的知情人的比例 |
| **打扰成本** | 每人每周收到的 OrgPilot 消息数；其中发给不相关成员的数量 |
| **收敛轮次** | 从发现问题到拿到一个确切 ETA，OrgPilot 与成员来回了几次 |

### 决策：升级和审批是否合理

| 指标 | 定义 |
|---|---|
| **误升级率** | 升级发生时，被升级的成员其实可用（未请假、未失联），只是还没来得及回复 |
| **审批负担** | PM 收到的审批请求数、过期未处理数、批准后又被再次改期的比例 |
| **终止原因可信度** | 回合报告的终止原因与世界状态是否相符（例如项目已无待办却报 `escalated`，即 D5） |

一个可以提前预见的发现：按真实的回复延迟分布（以小时计），内核当前「2 小时超时 + 只等 1 轮」的参数
大概率会产生大量误升级。这正是模拟器适合回答的问题：给出不同参数下误升级率和发现时延的取舍曲线。

**度量管线自检**：用一支「完美团队」（回复概率 1、主动性 1、乐观偏差 0、模糊度 0）跑一遍，
发现时延应接近 0、误升级应为 0。如果不是，说明出错的是度量管线本身，而不是 OrgPilot。

---

## 八、确定性与成本

**随机源**：主 seed 与成员 ID 经 SHA-256 派生出每个角色独立的 `random.Random`。
不能用 Python 内置的 `hash()`——字符串哈希每个进程随机加盐，会让同一个 seed 在不同进程里给出不同结果。
增删一个角色不影响其他角色的随机序列。

**时间**：全程只用 `FrozenClock`，由调度器 `set()`。上一轮引入的时钟注入在这里是必要条件。

**事件 ID**：由 seed、成员、序号确定性生成，例如 `sim-{seed}-{member}-{seq}`，
同时作为 `MessageContext.source_ref`，保证抽取产生的事件 ID 稳定。

**LLM 录制回放**：以「模型 + 系统提示 + 消息历史 + 采样参数」的 SHA-256 作为键，把 LLM 输出存成 JSONL 录像。三种模式：

| 模式 | 行为 | 用途 |
|---|---|---|
| `replay` | 只读录像，缺失即报错 | CI：确定、零成本 |
| `record` | 缺失时调用模型并写入录像 | 生成或更新录像 |
| `live` | 不读不写 | 探索 |

提示词一改，录像就失效，需要重新录制——这是预期行为。

**成本估算**（S2 起）：假设每个角色每个工作日平均触发 3–5 次 LLM 调用，7 个角色 × 30 个工作日，
单次运行约 600–1000 次调用。角色模型用 Haiku 级别的小模型，并且只在 `record` / `live` 模式下产生费用。

---

## 九、角色保真度

LLM 天生倾向于配合和诚实，很难「演」好一个乐观、拖延、不爱回消息的人；隐藏的真实状态写在提示里，也容易被它直接说出来。
如果角色没有演出设定的特征，模拟测到的就是 LLM 的性格，而不是我们想研究的人类行为。

所以每条特征都要有一个**可测量的特征指纹**，角色上线前先过保真度测试：

| 特征 | 指纹 | 通过标准（示例） |
|---|---|---|
| 乐观偏差 | 报出的 ETA 与 `true_finish` 的差 | 平均偏差落在设定值 ± 容差内 |
| 模糊度 | 回复中模糊时间表达的比例 | 接近设定概率 |
| 响应性 | 回复率与回复延迟分布 | 与设定分布一致 |
| 越界汇报 | 汇报中涉及他人任务的比例 | 接近设定概率 |
| 信息隔离 | 从未说出 `PersonaView` 之外的信息 | 0 次 |

规则角色同样跑这套测试，它是验证测试本身是否正确的对照组。

---

## 十、与现有评测体系的关系

| | L1 剧本场景 | 团队模拟器 |
|---|---|---|
| 人类行为 | 写死 | 由特征 + 真实状态驱动，对 OrgPilot 的行为做出反应 |
| 确定性 | 天然确定 | 靠 seed + 录像保证 |
| 断言方式 | 逐轮精确断言 | 多次运行的统计指标 |
| 擅长 | 锁定已知行为，防回归 | 发现未知问题，比较不同版本、不同参数 |

两者构成一个循环：**模拟发现 → 剧本固化 → 修复 → 模拟验证**。

固化工具：把一次失败模拟中 OrgPilot 每个回合收到的输入事件和审批决定导出为 `ScenarioRound` 序列，生成 YAML 骨架。
导出的剧本又回到了开环状态，但这对回归测试足够；**ground truth 必须由人来写**，表达正确行为，
然后按 L1 的方式进入 `KNOWN_DEFECTS` 棘轮。

---

## 十一、世界剧本格式（示例）

沿用 L1 场景的「星云」项目，人类行为改由角色产生：

```yaml
schema_version: 1
world_id: xingyun_payment_upgrade
project_id: xingyun-sim
start: "2026-10-12T09:00:00+08:00"
end: "2026-11-20T20:00:00+08:00"
calendar:
  work_hours: ["09:00", "20:00"]
  weekdays_only: true
orgpilot:
  heartbeat_minutes: 60
  turn_on_input: true

members:
  - id: eng_alice
    role: backend
    traits:
      reply_prob: 0.9
      reply_latency_median_min: 40
      proactive: 0.4
      optimism_days: 1.5        # 报出的 ETA 平均比真实早 1.5 天
      vagueness: 0.6
      ticket_hygiene: 0.9
  - id: eng_bob
    role: backend
    traits: { reply_prob: 0.8, reply_latency_median_min: 90, proactive: 0.1,
              optimism_days: 0.5, vagueness: 0.3, ticket_hygiene: 0.7 }
  - id: qa_frank
    role: qa
    traits: { reply_prob: 0.95, reply_latency_median_min: 20, proactive: 0.8,
              optimism_days: 0, vagueness: 0.2, ticket_hygiene: 1.0, cross_report: 0.5 }
    observes: [t3_order]        # QA 能看到上游接口有没有交付
  - id: pm_carol
    role: pm
    approval_policy:
      approve_if_slip_days_le: 5
      decision_latency_median_min: 90
  # ……其余成员略

tasks:
  - id: t1_gateway
    owner: eng_alice
    deadline: "2026-10-20T18:00:00+08:00"
    true_start: "2026-10-13T09:30:00+08:00"
    true_finish: "2026-10-20T15:00:00+08:00"
    deps: []
  # ……其余 7 个任务略

incidents:
  - { at: "2026-10-15T16:00:00+08:00", kind: slip, task: t1_gateway, delta: 2d,
      cause: 旧网关回调验签与新协议不兼容 }
  - { at: "2026-10-27T12:00:00+08:00", kind: slip, task: t3_order, delta: 16h,
      cause: 联调环境数据库迁移失败 }          # Bob 主动性很低，大概率不会主动说
  - { kind: leave, member: eng_emma, from: "2026-10-29", to: "2026-11-02" }
  - { at: "2026-10-28T14:00:00+08:00", kind: slip, task: t5_cashier_app, delta: 3d,
      cause: 负责人休婚假 }                    # v1 中请假的影响需要显式声明
  - { kind: unresponsive, member: eng_david, from: "2026-10-30T09:00:00+08:00",
      to: "2026-11-02T09:00:00+08:00" }
  - { at: "2026-11-07T09:00:00+08:00", kind: regression, task: t1_gateway, delta: 2d8h,
      cause: 高并发下验签偶发失败 }
```

---

## 十二、模块规划

```
src/orgpilot/simulation/
  world.py          # TrueTask、TicketSystem、World、incident 处理
  script.py         # 世界剧本加载（严格 schema，与 scenarios/loader.py 同风格）
  persona.py        # Persona 协议、Traits、Intent、HumanAction、PersonaView
  personas/
    rule_based.py   # 规则角色
    llm.py          # LLM 角色（S2）
  verbalizer.py     # 意图 → 中文模板
  render.py         # 命令 → 人看到的内容（与飞书适配器共用，见 P2）
  adapter.py        # SimulatedTeamAdapter
  scheduler.py      # 离散事件队列
  runner.py         # SimulationRunner
  metrics.py        # 指标采样与报告
  cassette.py       # LLM 录制回放（S2）
evals/simulation/
  worlds/xingyun_payment_upgrade.yaml
  cassettes/
tests/test_simulation_*.py
```

命令行：

```
orgpilot simulate evals/simulation/worlds/xingyun_payment_upgrade.yaml \
    --seeds 1-20 --input oracle|extractor --personas rule|llm --cassette replay|record|live
```

模拟器运行在进程内，直接组装 `CoordinationAgent` 与 `ClaimExtractor`，与 `ScenarioRunner` 的做法一致，
不经过 HTTP 网关。这样可以绕开网关层当前已知的三个缺陷（沙盒初始化、进度同步成员回复、跨项目主键冲突）；
经过网关的全栈模拟留到 S3。

---

## 十三、分阶段计划

| 阶段 | 内容 | 退出标准 |
|---|---|---|
| **S0 骨架** | 世界、工单系统、调度器、规则角色（只产生意图）、oracle 通道、`SimulatedTeamAdapter`、核心指标、单次运行命令；把 `max_response_rounds` 改为可配置 | 同 seed 两次运行轨迹逐字节一致；「星云」世界端到端跑通；「完美团队」自检通过 |
| **S1 抽取通道** | `Verbalizer` 中文模板（含模糊、口语化表达）、extractor 通道、两种模式的对比报告、失败原话导出 | 报告给出每项指标在两种模式下的差值；导出的失败样本可直接补进抽取评测集 |
| **S2 LLM 角色** | 角色提示词、LLM 角色实现、录制回放、保真度测试 | 每个角色通过保真度测试；CI 以 `replay` 模式运行，零调用 |
| **S3 扩展** | 固化工具（模拟 → YAML）；多 seed 批量运行与分布报告；进度同步会话的路由；经过 HTTP 网关的全栈模式；可选的「人扮演 PM」半自动模式 | 至少一个模拟发现的问题被固化进 L1 棘轮 |

「人扮演 PM」的半自动模式值得一提：用户在控制台里扮演 PM 做审批，其余成员由模拟器扮演。
这相当于 `776adb2` 沙盒分屏的自动化版本，适合演示。

---

## 十四、前置依赖

1. **先修已知缺陷 D1、D2、D4、D5。** 否则模拟指标会被它们主导：D5 让终止原因失去意义，
   D1 让同一任务的第二次问题必然漏检，D2 会让问对人率在一开始就被压低。模拟器应该用来发现**新**问题；
2. **D3 不是前置条件**。模拟器是开发 D3 的试验场，D3 实现之前的「静默事件发现率」就是它要超越的基线；
3. S0 需要一处不改变默认行为的内核改动：把 `max_response_rounds` 改为可配置（见第五节）。

---

## 十五、风险与开放问题

**R1 · 模拟与现实的差距。** 角色的行为来自我们对人的假设，优化模拟指标，实际上是在优化 OrgPilot 对「我们想象中的人」的表现。
缓解办法：接入真实飞书后，用真实日志（回复延迟分布、主动汇报比例）校准特征参数；准备多个差异较大的世界剧本；
模拟结果主要用于**相对比较**（版本之间、参数之间），而不是当作绝对的准确率。

**R2 · LLM 角色过于配合。** 见第九节，保真度测试不通过的角色不进入评测。

**R3 · 世界外生的局限。** v1 无法回答「OrgPilot 的协调让项目提前了多少」。
v2 可以引入有限的反作用，例如升级之后阻塞解除得更快，但每一条反作用都是一个需要论证的假设，应当谨慎添加。

**R4 · 指标被针对性优化。** 只盯一个指标会被「刷分」：例如对所有人每小时追问一次，发现时延会很好看，
打扰成本会很难看。所以三组指标必须一起报告，不设单一总分。

**开放问题**

- 心跳间隔怎么定？它同时影响成本、发现时延，以及 D3 的设计；
- 审批人是否默认由模拟器扮演，还是默认交给人（半自动模式）？
- LLM 角色跨越 30 个工作日的记忆如何压缩：按天摘要，还是只保留与自己任务相关的对话？

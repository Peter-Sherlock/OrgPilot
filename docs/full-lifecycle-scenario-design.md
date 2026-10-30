# 全生命周期协同案例设计：从立项到结项

> 目的：用一个贴近真实研发团队的六周项目，端到端检验 OrgPilot 作为「组织协调 Agent」的能力边界。
> 本文档先做**设计与差距判定**，不含实现。结论是：当前内核能承载其中约 40% 的剧情，其余部分缺的不是代码质量，而是**领域建模**。

> **版本说明（2026-10-29）**：第一至八节是 2026-10-08 在旧基线 776adb2 上写的原文，保留未改，
> 其中的行号和「现状」描述可能已经过时。移植到 main（ed8bf27）后的复核、各根因的 file:line、
> D1 的拆分和修复顺序见**第九节**，有出入时以第九节为准。

---

## 一、为什么现有评测不够

现有三套评测彼此不相交，中间有一道断层：

| 层级 | 覆盖什么 | 现状 |
|---|---|---|
| L1 确定性内核回放 | 打好标签的 `OrgEvent` → Agent → 断言状态/Case/终止原因 | ✅ 9 个场景（4 P0 + 5 M1） |
| L2 自然语言纵切 | 原始中文消息 → 抽取 → 内核 → 断言全链路 | ❌ **不存在** |
| L3 全栈集成 | HTTP + SQL 持久化 + 适配器 + 多项目并存 | ❌ **不存在** |

L2 断层的证据：`ScenarioRunner` 只消费已经打好类型的事件，整个回放路径里没有任何一行调用 `ClaimExtractor`
（见 `src/orgpilot/scenarios/runner.py` 的 `_run_interactive`）。
而 `orgpilot eval-extraction` 只单独测抽取的 20 个样本，输出的 claim 从不喂给内核。
**抽取对不对、内核对不对分别被验证过，但「抽取出来的东西能不能驱动内核做出正确决策」从未被验证。**

L3 断层的代价是可量化的：2026-10-08 实测，`POST /bootstrap-sandbox`、进度同步的成员回复路径均为 HTTP 500，
且两个项目只要有同名 `task_id` 就会撞主键——这三个缺陷单测全绿，因为单测直接调内部对象，绕过了 service/route 层。

此外，`ProgressSyncCoordinator`（主动进度同步、多轮澄清）**在评测体系中完全没有位置**：
`ScenarioRunner` 不认识它，`ScenarioRound` 的 schema 里也没有对应字段。最新的功能恰恰是验证最弱的。

一个覆盖立项到结项的案例，正好可以同时把这三层串起来。

---

## 二、案例设定

### 项目

**「星云」电商 App 支付链路升级** — 2026-10-12 立项，2026-11-20 结项，6 周。

目标：替换自研支付网关为三方聚合支付，覆盖 App 端与 H5 收银台，灰度上线。

### 团队（7 人）

| member_id | 姓名 | 角色 | 在案例中的戏份 |
|---|---|---|---|
| `pm_carol` | Carol | PM | 审批人，改期/范围变更的决策者 |
| `arch_grace` | Grace | 架构师 | 第二审批人，技术方案评审 |
| `eng_alice` | Alice | 后端 | 关键路径承担者，会延期、会破坏承诺 |
| `eng_bob` | Bob | 后端 | 与 QA 产生多来源冲突声明 |
| `eng_david` | David | 前端 | 中途失联，触发升级 |
| `eng_emma` | Emma | 客户端 | 中途请婚假，任务转交 |
| `qa_frank` | Frank | QA | 联调发现缺陷，触发回退 |

### WBS 与依赖（DAG）

```
T1 支付网关重构 (Alice)
 ├─> T2 三方渠道接入 (Alice)
 │    ├─> T4 收银台前端 (David) <─┐
 │    └─> T5 App 收银页 (Emma)    │
 └─> T3 订单服务改造 (Bob) ───────┘

 T4 ┐
 T5 ┴─> T6 全链路联调 (Frank) ─> T7 压测验收 (Frank) ─> T8 灰度发布 (Alice)
```

基线关键路径：`T1 → T2 → T4 → T6 → T7 → T8`

里程碑：**M1 联调就绪 11/03**、**M2 灰度上线 11/20**

> ⚠️ 建模约束：`OrgProjector` 要求 `task.created` 的 `dependencies` 指向**已存在**的任务
> （`src/orgpilot/state/projector.py:88-90` 对缺失依赖直接抛 `DomainInvariantError`）。
> 因此立项阶段的 8 个任务**必须按拓扑序创建**，不能先建 T4 再建 T2。

---

## 三、六周剧情 · 10 个 Beat

每个 Beat 给出：现实发生了什么 → 期望 OrgPilot 做什么 → 当前内核能否承载。

### Beat 0 · 立项（W0，10/12）

**现实**：Kickoff 会议，确定目标与周期，7 人入组，拆出 8 个任务与依赖，定下两个里程碑和排期基线。

**期望**：OrgPilot 建立项目基线快照，渲染 DAG，算出关键路径 `T1→T2→T4→T6→T7→T8`，不产生任何 Case（无风险）。

**判定**：⚠️ **部分可承载**。成员与任务可以表达，DAG 与关键路径可以算。
但**没有「项目」这个实体**——没有 `project.created` 事件，没有目标/周期/基线的落点，
也没有里程碑概念。没有基线，后面所有「偏差了多少」的判断都失去参照系。

---

### Beat 1 · 正常推进（W1，10/13–10/17）

**现实**：日常站会。Alice 把 T1 从 todo 推到 doing，口头承诺「T1 我 10/20 之前一定 done」。Bob 报 T3 进展正常。

**期望**：记录承诺（`commitment.made`，predicate=`workflow_status`、expected_value=`done`、due_at=10/20），健康全绿，无 Case。

**判定**：✅ **可承载**。`commitment.made` + `task.workflow_changed` + `task.health_reported` 足够。

---

### Beat 2 · 第一个模糊信号（W2，10/21）

**现实**：Alice 在群里说：**「三方 SDK 的文档跟实际返回对不上，可能还得再花两天」** —— 没说是哪个任务、「两天」从哪天算起。

**期望**：

1. 抽取出 `task.health_reported`，但 `task_id` 与 `expected_completion` 都不确定；
2. Agent 判定槽位不完整 → 发起 `ask_clarification`；
3. Alice 回「大概周四吧」→ 仍不精确 → 二次追问；
4. 收敛到 `2026-10-23T18:00+08:00`，落库。

**判定**：⚠️ **功能在，但评测测不到**。`ClaimExtractor` + `TemporalResolver` + 多轮澄清都已实现，
但 `ScenarioRunner` 吃不了自然语言，这段剧情**无法写进 L1 回放场景**。这是 L2 断层的直接后果。

---

### Beat 3 · 承诺到期破裂（W2，10/20 23:59）

**现实**：Alice 承诺的 10/20 到了，T1 还在 doing。**没有人说任何话。**

**期望**：OrgPilot 自己发现「承诺到期未兑现」，把该承诺标记为 `BROKEN`，开 Case，私聊 Alice 问情况，并提示 PM。

**判定**：❌ **完全无法承载——这是最严重的缺口**。

- `CommitmentStatus.AT_RISK` 与 `CommitmentStatus.BROKEN` 在 `domain/enums.py` 里定义了，
  但全仓库**从未被写入过任何地方**：承诺只能 ACTIVE → FULFILLED 或 ACTIVE → SUPERSEDED。
- 没有任何代码扫描 `commitment.due_at`。
- 更根本的是：`CaseLedger._is_risky()` 只看 `WorkflowStatus.BLOCKED` 或健康为 `AT_RISK/DELAYED`，
  **完全不看 `task.deadline`**。`task.deadline` 只在已经有人上报健康之后，用来和 `expected_completion` 做比较。

**一句话概括**：**当前系统纯粹是反应式的——没有人说话，就什么都不会发生。**
而真实项目里最危险的信号恰恰是沉默。这个 Beat 把「它还不是一个完全体 Agent」具体化了。

---

### Beat 4 · 影响传播与改期审批（W3，10/23）

**现实**：T1 确认延期 2 天。下游 T2→T4→T6→T7→T8 全在关键路径上。

**期望**：Agent 计算多跳影响，提出改期提案 → PM Carol 审批 → 批准后写回 deadline，并通知所有下游负责人。

**判定**：⚠️ **只有「直报 ETA」这一条路径可承载**。

- 成员**直接**报告延期并附带 ETA 时，影响传播、提案、审批门、写回都能走通（2026-10-08 验证）。
- 但真实场景更常见的是**先模糊、后追问出 ETA**，这条链是断的：追问拿到 ETA 时，`reconcile()` 直接把 Case
  标成 `resolved`（「recovery estimate received」），根本不和 deadline 比较，改期提案永远不会发出。
  这是 L1 压测发现的（见第六节 D1），读代码阶段没看出来。
- 但**改期只改自己那一个任务，不级联下游**——T1 推迟 2 天，T2~T8 的 deadline 原地不动。
- 且 `critical_path_impact_days` 在 `src/orgpilot/coordination/sync_coordinator.py:341` 是
  `1.5 if delayed > 0 else 0.0`，**硬编码常量**，没有真正按 DAG 算。

---

### Beat 5 · 多来源冲突声明（W3，10/24）

**现实**：Bob 在周会上说 T3「一切正常」；同一天 QA Frank 说「Bob 的接口到现在都没给我，T3 肯定来不及」。

**期望**：保留两条互相矛盾的声明（不是后者覆盖前者），把任务标为 `health_conflict`，开 Case，分别向 Bob 和 Frank 澄清。

**判定**：✅ **可承载，且是现有内核的强项**。P0 阶段就实现了「同一声明者 supersede、不同来源保留冲突」，
`ExpectedTaskState.health_conflict` 本来就是 ground truth 的断言字段。

---

### Beat 6 · 范围变更 + 人员变动（W4，10/28）

**现实**：两件事同时发生。

1. 合规要求插入新需求 **T9 风控接入**，依赖 T2，且 **T4 必须改为依赖 T9**；
2. Emma 临时请婚假 5 天，T5 转交给 David。

**期望**：重排 DAG、重算关键路径、识别 David 现在同时扛 T4 和 T5 的负载风险、把基线偏差记入变更台账。

**判定**：❌ **范围变更无法表达**。

- `TaskUpdatedPayload` 的字段只有 `task_id / deadline / title / owner_id`
  （`src/orgpilot/events/models.py:70`），**没有 `dependencies`**；
- 依赖只能在 `task.created` 时一次性定死（`projector.py:98`）。
- 结论：**任何依赖关系的变更在当前模型里都是不可表达的**。而范围变更是真实项目的常态。

人员变动可以靠 `task.updated.owner_id` 硬转交，但「Emma 10/28–11/01 不可用」这个事实无处安放——
没有人员可用性/负载模型，Agent 也就不可能发现 David 被压了两个任务。

---

### Beat 7 · 沉默与升级（W4，10/30）

**现实**：David 连续三天不回任何消息，T4 的 deadline 就在 11/01。

**期望**：探针 → 无响应 → 超时 → 升级给 PM，并在升级消息里带上「已问过几次、各在什么时间」。

**判定**：⚠️ **半可承载**。M1 的 `m1_05_unresponsive_member_escalates` 场景证明升级路径存在，
但触发它依赖「轮次推进」这个外部驱动；`ProbeMemberStatus.TIMEOUT` 在同步协调器里定义了却没有接线。
同 Beat 3 —— **缺的是「时间自己会走」这件事**。

---

### Beat 8 · 缺陷回流（W5，11/06）

**现实**：Frank 联调发现 T2 有阻塞性缺陷，T2 从 `done` 退回 `doing`。而下游 T6 已经开工。

**期望**：识别出「已完成任务发生回退」这个高危信号（和普通 todo→doing 完全不同量级），
立刻重算 DAG，把已经开工的下游标为受污染，通知 Frank 暂停联调。

**判定**：⚠️ **状态能表达，语义缺失**。`task.workflow_changed` 可以记录 done→doing，
但内核对回退没有任何特殊处理——`done→doing` 和 `todo→doing` 走同一条路径，不会升高风险等级。

---

### Beat 9 · 冲刺同步与结项（W6，11/18–11/20）

**现实**：PM 发起全员进度同步，收齐信息后确认可以上线；8 个任务全部 done；项目结项，要出复盘。

**期望**：

1. scatter-gather 探针 → 槽位完整性判定 → 收敛 → 生成决策简报；（这部分已实现）
2. 结项判定：所有任务 done 且所有承诺已结算；
3. 复盘产出：基线 vs 实际的偏差、关键路径实际走向、承诺兑现率、Agent 介入了多少次/拦下了什么。

**判定**：❌ **结项完全无法承载**。没有 `project.closed`，没有结项判定逻辑，没有基线因此也没有偏差，
没有复盘聚合。而且 Beat 9 第 1 步虽然功能已实现，但 HTTP 层成员回复路径当前是 500，
且评测体系里没有它的位置。

---

## 四、能力缺口清单（按严重度）

| # | 缺口 | 影响的 Beat | 证据 | 严重度 |
|---|---|---|---|---|
| 1 | **时间不驱动任何事**：无自主 tick，`_is_risky()` 不看 deadline，`commitment.due_at` 无人扫描 | 3, 7 | `coordination/ledger.py` `_is_risky()`；全仓库无定时扫描 | 🔴 P0 |
| 2 | **依赖不可变**：`task.updated` 不支持改 dependencies | 6 | `events/models.py:70`；`state/projector.py:98` | 🔴 P0 |
| 3 | **评测层断裂**：回放只吃 typed event，自然语言与同步会话进不了场景 | 2, 9 | `scenarios/runner.py`；`ScenarioRound` schema | 🔴 P0 |
| 4 | **无项目/里程碑/基线实体** | 0, 9 | 事件词表只有 7 类，全是 member/task/commitment 级 | 🟠 P1 |
| 5 | **承诺生命周期不完整**：`AT_RISK`/`BROKEN` 定义了但从不写入 | 1, 3 | `domain/enums.py` 对比 `state/projector.py` 全文 | 🟠 P1 |
| 6 | **无人员可用性/负载模型** | 6 | 无对应事件与状态 | 🟠 P1 |
| 7 | **改期不级联下游** | 4 | `coordination/service.py` 只构造单任务 `new_deadline` | 🟡 P2 |
| 8 | **回退无语义**：done→doing 不升高风险 | 8 | `projector.py` 的 workflow 变更分支无特判 | 🟡 P2 |
| 9 | **`critical_path_impact_days` 硬编码 1.5** | 4, 9 | `coordination/sync_coordinator.py:341` | 🟡 P2 |
| 10 | **跨项目隔离破损**：`case_id`/`approval_id` 非项目命名空间，表主键未复合 | L3 前置 | `storage/models.py:45,62` 对比 `:37` 事件表的正确做法 | 🔴 P0（阻塞集成测试） |

> 注：第 10 项是已实测复现的线上缺陷（`UNIQUE constraint failed: coordination_cases.case_id`），
> 它会让「多项目并存」的集成测试根本跑不起来，因此是本案例的**前置阻塞项**。

---

## 五、分层实施路线

这个案例不应该一次写完，而是切成三层逐层解锁。

### L1 · 已完成（2026-10-08，未改动内核代码）

文件：`evals/lifecycle/e2e_01_payment_upgrade_lifecycle.yaml`（22 轮、46 个事件），
棘轮测试：`tests/test_lifecycle_scenario.py`。实测结果见第六节。

用现有 7 类事件 + `ScenarioRound` 的多轮结构，覆盖 Beat 0（降级）→ 1 → 2（降级）→ 3 → 4 → 5 → 6（仅人员变动）→ 7 → 8 → 9（降级）：

- 立项降级为「7 个 member.registered + 8 个 task.created（拓扑序）」，不含项目实体与里程碑；
- Beat 2 用打好标签的 `task.health_reported` 代替原始中文消息（L2 才接自然语言）；
- **Beat 3 和 Beat 7 用「空轮次」表达时间推进**——回放里轮次本身就是时钟 tick。
  这比设计时的预估更进一步：原本以为 L1 表达不了时间驱动，实际上可以表达，只是内核会失败；
- Beat 6 只覆盖请假改期，依赖变更在当前事件词表里不可表达；
- 结项降级为「8 个任务全部 done + 承诺结算」。

为了让场景真正具备检验能力，补了评测引擎（`src/orgpilot/scenarios/`，不涉及内核）：

- schema 里早就声明的 `expected_termination`、`expected_case_statuses` **从来没被 evaluator 检查过**，现在逐轮检查；
- 新增逐轮 `expected_waiting_for`（Agent 在等谁——「问错人」是协调 Agent 的典型失败）；
- 脚本化的人工审批如果没命中任何待审批请求，过去会**静默变成空操作**，现在记为失败断言；
- `InteractiveGroundTruth` 新增 `commitments`；`ExpectedTaskState.health_status` 改为可选（与 `deadline` 一致）。

`InteractiveGroundTruth.executed_command_types` 同样是声明了但从未被检查的字段，本次未处理。

该场景的 ground truth 写的是**正确行为**，所以不放进 `evals/scenarios/`（那里要求全部通过），
单独运行：`uv run orgpilot replay evals/lifecycle/e2e_01_payment_upgrade_lifecycle.yaml`。

### L2 · 需要补评测基建

1. 给 `ScenarioRound` 增加 `messages` 字段（原始中文 + actor + 时间），让 runner 先过 `ClaimExtractor` 再进内核；
2. 给 `ScenarioRound` 增加 `sync` 字段，把进度同步会话纳入回放；
3. 解锁 Beat 2、Beat 9 完整版。

**价值**：补上「抽取 → 决策」的纵切验证，这是目前最大的评测盲区。

另一条补齐 L2 的路线是用角色 Agent 模拟团队成员，构建闭环评测环境，见 `docs/team-simulator-design.md`。

### L3 · 需要扩领域模型

按缺口清单依次补：

1. 先修 #10（跨项目隔离），否则集成测试起不来；
2. 补 #1 自主 tick + deadline/承诺扫描 → 解锁 Beat 3、Beat 7，**这是「从反应式工具变成自主 Agent」的分水岭**；
3. 补 #2 依赖变更事件 → 解锁 Beat 6；
4. 补 #4 项目/里程碑/基线 → 解锁 Beat 0、Beat 9 的结项与复盘。

---

## 六、L1 实测结果（2026-10-08）

**59 项检查：38 项通过，21 项失败。** 21 项失败全部归入 6 个根因，每个根因都用逐轮诊断实际复现过。

| # | 根因 | 失败检查数 | 首次出现 | 位置 |
|---|---|---|---|---|
| D1 | **终态 Case 占住任务的 Case 槽位**：`case_id` 由 task_id 确定性生成，`reconcile()` 只在槽位为空时开新案。任务的 Case 一旦终结，该任务再也开不了新案 | 8 | 第 4 轮 | `coordination/ledger.py` `reconcile()` |
| D2 | **问错人**：追问对象是「谁报的风险」而不是「谁负责这个任务」。QA 报了后端任务的风险，Agent 去问 QA 什么时候能恢复 | 1 | 第 7 轮 | `coordination/service.py` `plan_actions_for_case()` |
| D3 | **错过 deadline 不触发任何动作**：T3 逾期 16 小时、承诺已过期、没人说话，Agent 报告 `all_resolved` | 1 | 第 9 轮 | `coordination/ledger.py` `_is_risky()` |
| D4 | **迟到兑现记为兑现**：承诺兑现不比较 `due_at`，`BROKEN` 从未被写入 | 1 | 第 10 轮 | `state/projector.py` `_fulfill_matching_commitments()` |
| D5 | **升级状态一直不消失**：只要历史上有过一次升级，之后每一轮都报 `escalated`。第 14 轮 David 那次升级，让第 15–22 轮（三周，直到结项）全部报 `escalated` | 8 | 第 15 轮 | `agent/loop.py` `_determine_termination_reason()` |
| D6 | **任务完成后风险状态不清除**：结项时 8 个已完成任务中 4 个仍显示 at_risk/delayed，依赖分析器仍从已完成任务传播出 17 条影响路径 | 2（不变量） | 结项 | `state/projector.py`；控制台 `gateway/routes/dag.py:146` |

D1 最能说明问题：第 17 轮 T1 被打回 doing、ETA 晚于 deadline，内核自己判断 `_is_risky=True`，
但待审批数为 0、什么都没发生——因为第 4 轮那个已结案的 Case 还占着 `case:task-health:t1_gateway`。

**通过的对照组**（M1 已验证过的路径在长链路里依然成立）：模糊风险 → 追问负责人；
追问 → ETA 在期限内 → 结案；直报 ETA → 改期审批 → deadline 写回（T5 → 11/06）；
失联 → 超时升级；按时兑现的承诺记为 fulfilled；冲突声明被保留。

**棘轮机制**：`KNOWN_DEFECTS` 按根因登记每一项失败检查。出现未登记的失败 = 回归，测试变红；
已登记的检查转绿 = 有缺陷被修了，测试同样变红，要求删除对应登记项，把修复锁住。
两个方向都用变异实验验证过（模拟修复 D2 / 删掉 D3 的登记，测试均精确报出对应检查）。

**修复顺序建议**：D2、D4 各是一处局部改动，D5 改终止原因的判定逻辑；D1 要重新设计 Case 的身份标识
（同时 `approval_id` 由 case_id 派生，第二次审批会覆盖第一次），是改动面最大、收益也最大的一项；
D3 就是第四节的缺口 #1，属于新能力而不是 bug 修复。

---

## 七、验收定义

这个案例算「跑通」，需要同时满足：

1. **确定性**：同一份 YAML 连跑两次，事件序列、Case 状态转移、审批记录逐字节一致；
2. **时间无关**：把整条时间线整体平移到 2019 年或 2088 年，结论不变（时钟注入已为此铺好路）；
3. **可解释**：结项时能对每一次 Agent 介入回答「依据哪条事件、哪条声明的原文」；
4. **守住边界**：全程 LLM 没有直接改过任何状态，所有写操作都经过 Policy → Approval；
5. **账对得上**：结项复盘里的承诺兑现率、基线偏差，能从事件日志重放出来，而不是另算一套。

---

## 八、结论

读代码阶段的预估是：这个案例的 10 个 Beat 里能承载 4 个，约 40%。
L1 实测给出的是更细的数字：**59 项检查通过 38 项**，而且失败的地方不止领域建模缺失——
D1、D2、D4、D5 是**现有能力本身的缺陷**，只有拉长到一个完整项目周期才会暴露：
现有 9 个场景最多 2 轮、2 个任务，「同一个任务第二次出问题」「升级三周后的状态」这类情况在里面根本不会出现。

内核的确定性、幂等、审批门、冲突保留做得扎实，这一点经过 22 轮压测依然成立（重放逐字节一致）。
挡路的除了上面这四个缺陷，还有**三个建模缺失**：没有时间驱动、没有依赖变更、没有项目实体。

其中最关键的是第一个。一个只会对消息做出反应的系统，是一个**协调工具**；
一个会因为「时间到了而没人说话」主动行动的系统，才是**协调 Agent**。
这个案例最大的价值，就是把这条线画清楚。

---

## 九、2026-10-29 复核：移植到 main（ed8bf27）后的基线

这是里程碑 M1（让现有能力扛住一个完整项目周期）的度量基线。本节只做度量，不修内核。

### 9.1 移植了什么

- 场景 `evals/lifecycle/e2e_01_payment_upgrade_lifecycle.yaml` 与棘轮测试 `tests/test_lifecycle_scenario.py`；
- 评测引擎三处改动（`scenarios/evaluator.py`、`models.py`、`runner.py`）。这三个文件在 main 上自 776adb2 起
  没有变过，补丁原样应用；
- 本文与 `docs/team-simulator-design.md`（M3 用的设计稿，未实现，已在文件开头标明）；
- 新增 `tests/test_scenario_round_checks.py`：直接锁住引擎的逐轮检查语义（等错人、脚本审批落空、承诺、
  不断言健康度），这样内核缺陷修好之后这些路径仍有覆盖；
- **没有移植** Clock 注入：已经由 PR #5 合入；
- `orgpilot replay --all` 只扫描 `evals/scenarios/*.yaml`（`scenarios/loader.py` 的 `discover_scenarios`
  不递归），仍是 9/9，生命周期场景不进入。引擎现在会真正检查以前声明了却没检查的逐轮字段，
  现有 9 个场景里只有 m1_02、m1_03 声明了逐轮审批，两条都确实命中了待审批请求。

### 9.2 结果：数字没有变

**59 项检查：38 通过，21 失败**，与 10-08 在旧基线上完全一致，失败集合也逐项相同。
main 在这期间的改动——意图路由（ADR-0008）、名单导入（ADR-0009）、PR #5 的 `new_deadline` 键与写回失败升级——
都不在 `ScenarioRunner` → `CoordinationAgent` 这条路径的被测行为上，所以既没修好也没引入任何一项失败。
注意范围：这个场景只测内核，不经过意图路由、进度同步协调器和飞书适配器。

### 9.3 根因复核（行号指向 ed8bf27）

| # | 根因 | 位置 | 失败检查 | 状态 |
|---|---|---|---|---|
| D1a | **追问拿到 ETA 就结案，不和 deadline 比较** | `coordination/ledger.py:163-173` | 5 | 仍复现；从原 D1 拆出 |
| D1b | **终态 Case 占住任务的 Case 槽位** | `coordination/ledger.py:195-197`；`coordination/approval.py:24` | 3 | 仍复现 |
| D2 | **问报告人而不是负责人** | `coordination/service.py:135`（`build_cases` 的 `:60` 是同一规则） | 1 | 仍复现 |
| D3 | **时间不驱动任何事** | `coordination/ledger.py:176-188` 是 reconcile 里唯一的时间比较；`_is_risky` `:221-226` 不看 deadline；无人读 `due_at` | 1 | 仍复现 |
| D4 | **迟到兑现记为兑现** | `state/projector.py:219-230`；`BROKEN`（`domain/enums.py:34`）在 src 里从未写入 | 1 | 仍复现 |
| D5 | **升级状态一直不消失** | `agent/loop.py:271-274`；`coordination/ledger.py:121-123` 跳过终态 Case | 8 | 仍复现；PR #5 新增第二条入口 |
| D6 | **任务完成后风险不清除** | `state/projector.py:103-119`；`dependencies/analyzer.py:47-52`；控制台 `gateway/routes/dag.py:146` | 2（不变量） | 仍复现 |

- **没有消失的缺陷，也没有新的失败检查。**
- **D1 拆成两个根因。** 原登记把第 4 轮和第 17 轮都归到「槽位被占」，实际是两处独立代码：第 4 轮是
  `ledger.py:163-173` 在等待回复时一拿到 ETA 就结案；第 17 轮才是 `:195-197` 的槽位问题。
  两处各自修好都不引起回归（见 9.4）。`case_terminal_reason:t1_gateway` 修好任意一处都会转绿，
  登记在 D1a 名下，先合入的那个 PR 负责删掉它。
- **PR #5 扩大了 D5 的影响面。** 批准后写回失败现在会升级（`agent/loop.py:249-257`），这是第二条进入
  「粘住的 escalated」的路。Mock 适配器从不失败，本场景测不到这一条。
- **潜在问题，本场景观测不到：** `approval_id` 由 `case_id + action_id` 派生（`approval.py:24`），同一个
  Case 第二次改期会覆盖第一次已消费的审批记录。加不加唯一 id，本场景的结果都一样，所以没有对应检查。
- 第二节提到的几处旧行号顺带核对：`critical_path_impact_days` 仍是硬编码 `1.5`，行号移到
  `sync_coordinator.py:344`；`ProbeMemberStatus.TIMEOUT` 现在已经接线（`sync_coordinator.py:76-77` 判定超时，`:83` 起的 `_timeout_session` 处理），
  但是惰性的——下次查询会话时才判定超时，仍不是「时间自己会走」。其余描述未复核。

### 9.4 验证方法：逐根因的最小修复实验

只读代码定位根因不够：两个根因可能共享一项检查，一个看似局部的修复也可能牵动别处。因此对每个根因
只改上表引用的那几行，**在内存里**打补丁（没有提交、没有改动任何文件），然后重跑生命周期场景、
`evals/scenarios/` 的 9 个场景和整个测试套件：

| 最小修复补丁 | 生命周期转绿 | 生命周期新增失败 | 9 个回放场景 | 其余 399 个测试 |
|---|---|---|---|---|
| D1a：ETA 晚于 deadline 时把 Case 退回 OPEN，而不是结案 | 5（恰为 D1a 的登记项） | 0 | 9/9 | 全过 |
| D1b：只在出现**比结案更新的风险声明**时重开 Case | 4（D1b 的 3 项 + 共享项） | 0 | 9/9 | 全过 |
| D1b 朴素版：只要任务仍有风险就重开 | 7 | **12** | **7/9**（m1_01、m1_05 失败） | — |
| D2：追问对象改为 `task.owner_id` | 1 | 0 | 9/9 | 全过 |
| D3 粗略版：逾期且没有未来 ETA → 单独开 `case:overdue:<task>` 追问 | 1 | 0 | 9/9 | 全过 |
| D4：兑现时比较事件时间与 `due_at`，迟到写 `BROKEN` | 1 | 0 | 9/9 | 全过 |
| D5：任务完成时把它的 escalated Case 关掉 | 8 | 0 | 9/9 | 全过 |
| D6：任务 done 时只退役风险声明，按剩下的声明重算健康度 | 2 | 0 | 9/9 | 全过 |
| 七个补丁（D1b 用非朴素版）叠加 | **21 → 59/59** | 0 | 9/9 | — |

几点结论：

1. 每个补丁都恰好让自己名下的检查转绿，不多不少，所以登记表的归因是对的；棘轮两个方向都实测过：
   打上 D2 补丁时，测试报出「D2 的 1 项已通过，请删除」；打上朴素 D1b 补丁时，报出 12 项未登记失败；
2. **D1b 不能「任务还有风险就重开」**：T3 在第 8 轮拿到期限内的 ETA 后仍是 at_risk，已完成的任务在 D6
   修好之前也一直带着风险，朴素重开会让 Case 来回抖动，并打破 m1_01、m1_05；
3. **D3 需要独立的 Case**：ground truth 要求 T3 的 task-health Case 最终保持 `resolved`（理由「recovery
   estimate received」），所以逾期追问要么用自己的 case_id，要么等 D1b 先合入。粗略补丁只证明了
   「第 9 轮该醒来」这件事可以单独做到，不是设计：没有风险声明的逾期任务，`plan_actions_for_case`
   在 `service.py:118-123` 直接返回空，正式实现还得补这条规划路径；
4. 现有单元测试里没有一条把这些缺陷行为当成预期写死，所以每个修复都不需要改已有测试，
   只需删自己的登记项、补自己的回归测试。

这些补丁是**实验用的草图**，不是修复方案，没有进入本 PR。

### 9.5 修复顺序与改动文件

按源文件分成三条互不重叠的线，线内顺序合入，线间可以并行：

| 波次 | 线 A：`coordination/ledger.py` | 线 B：`state/projector.py` | 线 C：`coordination/service.py` |
|---|---|---|---|
| 1 | **D5**（8 项）`ledger.py:121-123`，必要时 `agent/loop.py:266-288`；测试 `test_case_ledger.py` / `test_agent_loop.py` | **D4**（1 项）`projector.py:119,179,219-230`；测试 `test_projector.py` | **D2**（1 项）`service.py:60,135`；测试 `test_policy_and_coordination.py` |
| 2 | **D1a**（5 项）`ledger.py:163-173`；测试 `test_case_ledger.py` | **D6**（2 项）`projector.py:103-119`，可能连带 `dependencies/analyzer.py:47-52`；测试 `test_projector.py` / `test_dependencies.py` | — |
| 3 | **D1b**（3 项）`ledger.py:195-197`、`approval.py:24`；测试 `test_case_ledger.py` / `test_approval_manager.py` | — | — |
| 4 | **D3**（1 项 + 新场景）`ledger.py`（deadline/承诺扫描）+ `service.py:118-123`（无风险声明的逾期任务如何追问），等线 C 合入后再开；新增 `evals/scenarios/m1_06_*` | | |

- D5 排第一：失败项最多，而且修好之前「终止原因」这个指标在任何长场景里都没有意义，后面每一步的度量都依赖它；
- D1a 在 D1b 之前：改动面小，覆盖的是最常见的真实路径（先模糊、追问出 ETA、发现超期）；D1b 要定下 Case 的身份
  与重开条件，是设计决策；
- D3 放最后：它是 M1 计划里的「时间驱动的 deadline 与承诺跟进」，属于新能力。承诺到期变 `BROKEN` 由谁写
  （投影器按事件时间判定，还是扫描时发出新事件）会和 D4 的做法相交，D4 合入后再定；
- **共享文件**：每个修复 PR 都要从 `tests/test_lifecycle_scenario.py` 的 `KNOWN_DEFECTS` 删掉自己的登记项。
  相邻条目被并行删除时 git 会报一个无关紧要的冲突，保留两边的删除即可；逐个合入、合入前同步 main 可以避免。
- 以上行号都不在 `gateway/`、`feishu/`、`intent/`、`storage/`、`sync_coordinator.py` 里；D6 修好后控制台的
  `dag.py:146` 会自动跟着变，不需要改网关。

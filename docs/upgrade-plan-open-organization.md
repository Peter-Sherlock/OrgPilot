# 升级方案：从「预设任务状态采集器」到「开放世界组织协调体」

> 状态：提案（待评审）　日期：2026-08-29　触发：真机测试发现 PM 指令「告诉Alice，必须在明天上午12点之前完成」无法被识别与执行（机器人回复"当前无需要变更的任务状态"）。

## 一、问题定性

该条消息暴露的是三个系统级缺口，而非单点 bug：

1. **意图单一**。`ExtractionResult`（`extraction/models.py`）只有 `claims`（健康声明）与 `commitments`（承诺）两类产出。PM 指令（directive）、自然语言建任务、任务改派、截止期变更、提问，全部落入 `is_actionable=false`。系统唯一认识的"动作"是**成员汇报任务健康度**。
2. **封闭世界假设**。`MessageContext.known_tasks` 是白名单，prompt 严格规定"只能从已知任务列表逐字选取"。这是治幻觉、保评测 F1 的正确设计，但副作用是系统永远无法认识新事物——不能有新任务、新约束、新关系。演示沙箱一旦超出预设的 4 人 3 任务即失能。
3. **无指令路由与执行跟踪**。即便识别出"要 Alice 明天 12 点前完成"，也不存在把指令送达 Alice、确认回执、超时升级的机制。`ActionType` 中没有 DIRECTIVE 类动作，事件本体中没有指令类事件。

规模化缺口（几十~几百成员）同样存在：探针扇出为单循环 O(N)、每条消息一次 LLM 抽取调用（无成本分级）、成员模型扁平（无组织层级）、简报全量合成。

## 二、升级总原则

**保持内核之魂**：一切新能力仍走 `事件 → 投影 → 拓扑影响 → 协调案例 → 策略引擎 → 人工门禁 → 适配器执行`。升级不是"让 LLM 自由发挥"，而是**扩大类型化事件本体 + 有根据的抽取 + 分级审批门禁**。可回放、可审计、可评测三大差异化特性全部保留。

## 三、四大升级柱

### 柱 1：开放任务本体（解决"预设完就手足无措"）

- **意图路由层（Intent Router）**：在 `ClaimExtractor` 之前加轻量意图分类（规则初筛 + LLM 兜底），意图空间：`health_report | directive | task_create | task_reassign | deadline_change | question | chit_chat`。`ExtractionResult` 增加 `intent` 字段；无法确定意图时转为追问而非忽略。
- **事件本体扩展**：新增 `directive.issued / directive.acknowledged / directive.completed`；`task.created` / `task.updated` 从"仅 bootstrap/API"开放为"NL 可触发（必须过审批门禁）"。改派可直接复用 `task.updated.owner_id`（内核侧已支持）。
- **动态任务注册**：PM 说"新增任务：网关压测，David 负责，周五前" → LLM 产出提案（slug 任务 ID + 标题 + 负责人 + 截止）→ **GroundingVerifier 扩展**：负责人存在性校验（防幻觉成员）、任务 ID 冲突检测 → 审批卡片 → PM 确认后才落 `task.created` 事件。
- **指令语义解析与时间歧义**："告诉Alice明天上午12点前完成" → `ExtractedDirective(target=ou_alice, task_id, deadline=?)`。"上午12点"本身歧义（中午 12:00 还是凌晨 00:00？）→ 复用既有 `SlotCompletenessEvaluator` 追问机制，属现成能力的自然延伸。
- **策略引擎扩展**：截止期指令 vs 依赖 DAG 的冲突检测——"明天12点完成"是否早于其上游任务预计完成时间？冲突则自动进入 `PROPOSE_RESCHEDULE` 链路或向下游预警。

### 柱 2：指令生命周期（让聊天变成可审计的工作项）

- PM 指令成为一等公民协调案例（DirectiveCase），复用 `CaseLedger` 状态机：`OPEN → WAITING_FOR_RESPONSE（等 Alice 确认）→ EXECUTING → DONE`，超时升级回 PM。
- Agent 中继执行：经 adapter 送达 Alice（沙箱窗格与真实飞书私聊同路径），Alice 的后续回复自动关联到该指令（会话关联已有 SyncSession 先例）。
- 决策简报纳入"进行中指令及其状态"，PM 全景可见。

### 柱 3：组织规模化（几十~几百成员）

- **组织结构事件**：`team.created / member.joined_team / member.reports_to`；任务 DAG 之上叠组织树。
- **分级同步（Hierarchical Sync）**：PM → 各 Team Lead → Lead 下探成员 → 自底向上聚合简报。扇出从单循环 O(N) 变为树形分治，每级复用既有 scatter-gather。
- **外发消息队列**：探针/指令出站经队列 + 速率控制（适配飞书 API 限流），失败重试 + 幂等键（已有）。
- **成本分级抽取**：规则/小模型做意图初筛（多数 `chit_chat` 直接短路，零 LLM 成本），仅 actionable 消息进大模型；同会话上下文缓存。100 人高频消息的成本模型必须先行测算。
- **目录与分页**：成员按 team 分页路由，简报分级合成（团队简报 → 项目简报）。

### 柱 4：评测与治理先行（没有度量就没有信任）

- **黄金数据集扩展**：新增 directive / task_create / reassign / 歧义时间样本类目；新指标：Intent Accuracy、Directive Completion Rate、Task Creation Grounding Valid Rate。
- **规模仿真基准**：合成 100 人 × 5 团队 × 200 任务事件流回放，测定全量同步时延、LLM 调用数与成本、探针送达率、简报合成时间；SLO 写入 README。
- **治理红线**：NL 建任务/改派一律过审批门禁（角色化：仅 pm 角色可发起创建）；所有 LLM 提案必须携带可追溯 `source_quote`；成员越权操作（非 pm 建任务）默认拒绝并记录。

## 四、分期落地（每期交付一个可演示闭环，验证门全绿后提交）

### 第 1 期（M3：指令与开放任务）——直接解决本次暴露的问题

1. Intent Router + `ExtractionResult.intent`（含"意图不明→追问"）
2. 指令事件三元组 + DirectiveCase + 审批卡片
3. NL 任务创建（经审批）+ 任务改派（复用 `task.updated.owner_id`）
4. 时间歧义追问（"上午12点"类）
5. 沙箱升级：PM 窗格"下达指令"快捷按钮；黄金数据集 +30 样本

**验收演示**：PM 输入"告诉Alice，明天上午12点前完成" → 追问歧义或生成指令卡 → Alice 窗格收到指令 → Alice 确认 → PM 收到执行确认；超时未确认自动升级提醒。

### 第 2 期（M4：规模化组织）

1. 组织树事件 + 分级同步
2. 外发队列 + 限流重试
3. 成本分级抽取
4. 100 人合成基准 + SLO 报告

**验收演示**：100 人 5 团队沙箱一键压测，输出时延/成本/送达率报告。

### 第 3 期（M5：复杂信息治理）

1. 多话题会话状态（同一成员同时谈两个任务的分段抽取）
2. 指令-依赖冲突分析器（directive vs DAG）
3. 策略规则 YAML 化（不改代码调策略）
4. 跨项目依赖（可选）

## 五、与既有路线图的关系

- 原 M3（协调评测基线 B0–B3、Blocker Recall / False Reminder Rate）不砍掉，吸收进第 1 期的黄金数据集扩展与指标体系。
- 原 M4（发布工程）顺延为第 3 期后的收尾里程碑。
- 实施第 1 期时补 ADR-0007（开放任务本体与指令生命周期架构决策）。

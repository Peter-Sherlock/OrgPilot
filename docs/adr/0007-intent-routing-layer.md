# ADR-0007: Role-Aware Intent Routing Layer Before Claim Extraction

- 状态：Accepted
- 日期：2026-08-29
- 关联：ADR-0003（LLM 声明抽取边界）、docs/upgrade-plan-open-organization.md（升级总方案·柱 1/柱 4）

## 背景

真机测试暴露：PM 指令「告诉Alice，必须在明天上午12点之前完成」被系统回复"当前无需要变更的任务状态"。根因是抽取管线唯一认识的意图是"成员汇报任务健康度"，指令、建任务、改派、截止期变更、提问全部落入不可操作被静默丢弃；同时每条消息无条件消耗一次 LLM 抽取调用，规模化（百人团队）成本不可接受。

## 决策

1. **路由先于抽取**：新增 `IntentRouter`（`extraction/intent.py`），在 `ClaimExtractor.extract_from_message` 最前执行确定性规则分类，意图空间为八类 `MessageIntent`（health_report / directive / task_create / task_reassign / deadline_change / question / chit_chat / uncertain）。
2. **混合判定，意图与抽取共用一次 LLM 调用**：规则能自信判定的非汇报类意图（指令、闲聊、建任务等）直接短路——零 LLM 成本；汇报类与规则无法判定的消息进入既有 LLM 抽取调用，抽取 prompt（规则 6）要求模型在同一次输出里给出 `intent` 字段兜底精分类。不引入第二次 LLM 调用。
3. **角色感知**：同一句话按发送人角色区分语义——PM 的「延期到周五」是排期指令（deadline_change，confidence 0.85+），成员的「延期到周五」是本人进度汇报（health_report）。特权角色（pm/lead）发出的指令/创建/改派/截止期意图标注 `authority_ok=true`，非特权角色降低 confidence——策略执行层（后续里程碑）据此门禁。
4. **自我指令守卫**：「我必须明天完成」是承诺/汇报而非指令；指令必须以**他人**为目标成员（成员提及检测基于上下文成员目录，含 `ou_xxx` 后缀词元匹配）。
5. **实体提示随行**：`IntentResult.hints` 携带目标成员、涉及任务与原始时间表达（如「明天上午12点」），为下一里程碑（指令事件三元组 + DirectiveCase + 歧义时间追问）备好槽位原料；本期只识别不执行。
6. **可评测性**：金标数据集扩至 34 样本（+14 意图样本，含真机故障原话回归样本）；新增 Intent Accuracy 指标并纳入通过门槛（≥90%）；`ExtractionResult.intent` 为可选字段，旧回放/夹具数据零破坏。
7. **真实评测可复现**：LLM 客户端固定 `temperature=0`；抽取 prompt 规则 5 补充「恢复时刻 ≠ 新交付里程碑」与「完不成的时间边界即预计完成时间」两条裁定，规则 4 补充「承诺不重复产出健康声明」。

## 后果

- 正：闲聊与指令类消息零 LLM 成本；意图可度量、可回归（真机故障原话进入金标）；真实模型基准 Intent Accuracy 100%（92.9–100% 跨运行，门槛内）、抽取 F1 100%（此前 96%）。
- 负：规则词表需随语料维护（漏检时兜底走 LLM，误检需样本回归）；中文成员真实姓名尚未进入路由上下文（当前依赖 member_id 词元），成员资料扩展在规模化里程碑处理；指令的执行/传达链路（directive 事件、DirectiveCase、审批卡片）为下一里程碑，本期界面上以"已识别、链路即将启用"明示，不静默丢弃。

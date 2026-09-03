# ADR-0009: Real-Integration Reliability (M3-R)

- 状态：Accepted
- 日期：2026-08-29
- 关联：ADR-0005（飞书适配器）、ADR-0008（指令执行链）、docs/upgrade-plan-open-organization.md

## 背景

外部审查对真机集成链做了逐条核证：202 项离线测试全绿、F1 100% 的同时，运行日志里飞书 `send_message()` 缺 `msg_type`、AIHubMix `ReadTimeout` 传播为 500、改期字段在内核（`new_deadline`）、真实适配器（`deadline` + 回退 `now()`）、审批卡（`proposed_deadline`）三处各说各话；外发失败只记日志，账本「已下达」而成员未收到；歧义追问不保存上下文，用户回答后无法恢复原指令；控制台写死 `pm_web_operator` 身份必然 403；同步 LLM 调用阻塞事件循环。

结论：**架构方向正确，但「测试全绿」与「真实送达」之间存在系统性断层**。在进入 NL 任务创建（M3 收官）与规模化（M4）之前，必须先插入 M3-R（真实集成可靠性）阶段。

## 决策

1. **统一适配器契约（`adapter/contracts.py`）**。每个 `ActionType` 的 payload 解析收敛到单一模块，Mock 与真实适配器共用；规范键 `text` / `new_deadline`，旧键保留一个版本的别名通道。**必填字段 fail-closed**：截止期缺失或不可解析即返回 `CommandStatus.FAILED`，绝不静默回退 `datetime.now()`。跨适配器契约测试（`tests/test_adapter_contract.py`）保证两个实现行为等价。
2. **持久化 outbox（事务性外发）**。外发命令先落 `outbox` 表（幂等键唯一约束），再尝试执行；崩溃在「事件已持久化、命令未发送」之间时，启动/周期清扫补发（at-least-once）。传输失败按线性退避重试（`ORGPILOT_OUTBOX_RETRY_SECONDS`），超过 `ORGPILOT_OUTBOX_MAX_ATTEMPTS` 进入死信。指令中继额外落 `directive.delivered / directive.delivery_failed` 事件，投影到 `DirectiveState.delivery_status`——**事件账本不再谎报「已下达」**。agent 循环内的探针/卡片/任务更新保持内联执行（脚本化回复时序依赖），但结果同样结算进 outbox：成功成为台账，失败转为待重试行。送达事件的 occurred_at 钳制到指令最后事件时间之后，保证按 occurred_at 排序的回放永远因果有序。
3. **澄清必须闭环**。「上午12点」类追问不再是一次性提问：`directive.clarification_requested` 事件把待补全草稿（目标/任务/时间槽位 + 原文）持久化进投影状态，发布者的下一条回复被解析合并（中午→12:00、凌晨/晚上→00:00 合并回原表达），补全后按原上下文正式下达；支持显式取消。同一成员多条未确认指令时，确认/完成回复按任务名或指令内容绑定，无法唯一绑定时列出清单反问，绝不静默绑定最新一条。
4. **运行时韧性与身份**。LLM 抽取经 `asyncio.to_thread` 下放线程 + 有界重试 + 连续失败熔断（开路快速失败、半开探活），故障降级为友好提示而非 500；整条消息摄入（抽取→指令链→投影→外发→agent 回合）纳入项目级锁，agent 回合拆出已持锁内部方法避免重入死锁。控制台审批身份由服务端 `GET /api/v1/projects/{id}/context` 提供（项目 PM/lead 成员），前端不再冒充常量。所有持久化数据进 innerHTML 前经 `esc()` 转义，阻断持久化 XSS。飞书 WS 监听器补 `stop()`，lifespan 统一取消清扫任务并关闭连接。

## 后果

- 正：外发从「尽力而为」变为「可恢复、可重试、可审计」；账本与真实世界不再分叉（送达/失败有事件级证据）；澄清与多指令消歧补齐多轮会话语义；LLM 故障、并发摄入、注入攻击、身份冒充四类生产边界有明确防线。
- 负/已知边界：outbox 为 at-least-once，理论上目标可能收到重复消息（飞书侧幂等键已带、去重依赖端)；死信需要人工介入（控制台有积压提示）；契约别名通道（`inquiry_text`/`deadline`/`proposed_deadline`）只保留一个版本，生产者须迁移到规范键；熔断冷却期内的消息会得到降级提示而不是排队等待。

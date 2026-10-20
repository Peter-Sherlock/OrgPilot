# ADR-0012：Outbox-first 执行与结果真实性

- 状态：Accepted
- 日期：2026-09-12

## 背景

M3-R 已建立持久化 outbox，但网关中的部分 Agent 动作仍先调用适配器，再补写 delivery ledger；
进度同步的探针、澄清和简报也存在直接外发路径。审计进一步复现了两个错误状态：适配器返回
`ActionResult.failed` 时 outbox 仍可记为 `delivered`；审批后的任务更新失败时 Case 仍可进入
`resolved/all_resolved`。

这些行为破坏了执行层的基本约束：审批通过不等于执行成功，执行尝试不等于平台接受，平台接受
也不等于业务状态已经更新。

## 决策

1. `GatewayService` 创建的 Agent 使用 deferred outbound。Agent 只生成命令和领域状态转换，网关
   先把命令写入 outbox，再调用真实适配器。独立场景回放仍可使用同步 Mock 执行。
2. outbox 同时检查异常和 `ActionResult.status`。只有 `success` 可进入 `delivered`；显式
   `failed/rejected` 作为确定性失败进入 dead letter。
3. 适配器成功产生的 `task.updated` 等领域事件由 outbox 执行结果回收并持久化。任务更新首次失败
   时 Case 进入 `escalated`，只有重试真正产生对应更新事件后才恢复为 `resolved`。
4. 进度同步的探针、澄清和简报也使用 deferred outbound，由网关统一进入 outbox。
5. 网关维护循环除重试 outbox 外，还按持久化事件项目扫描指令超时。无人继续发消息时，提醒与
   升级仍可发生；事件 ID 和 idempotency key 防止重复发送。
6. 服务时钟可注入。审批边界测试不再依赖执行测试当天的墙钟；过期飞书卡片返回明确冲突结果，
   不把领域异常泄漏为未处理的 500。

## 后果与边界

- 网关路径不再发生“先发送、后记账”，返回的 turn trace 会在 delivery 结算后重新读取 Case 状态。
- 单条 outbox 命令仍是 at-least-once；超时等不确定结果仍可能需要平台幂等键或人工对账。
- 领域事件、Case 快照、同步会话与 outbox 当前由多个短事务保存，尚未形成跨表单事务 Unit of
  Work。崩溃在“领域事件已保存、outbox 尚未入队”的极窄窗口仍需后续用可重建命令或同事务提交
  消除。
- 当前锁和后台扫描仍是单进程语义；多实例 claim/lease、PostgreSQL 并发与领导者选举不在本 ADR
  的验证范围。
- 本 ADR 只收口离线执行真实性，没有完成真实飞书 L3-A 或业务任务写入验收。

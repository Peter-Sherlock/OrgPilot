# ADR-0004：SQL 异步持久化存储与 FastAPI 事件网关

- 状态：Accepted
- 日期：2026-08-26

## 背景

在 P0、M1、M2 阶段，OrgPilot 验证了确定性状态推导、Case 状态机、Mock 闭环协调和自然语言声明抽取，但数据与状态全保存在单进程内存中：
1. 服务重启导致全部历史事件与进行中的 Case 丢失；
2. 缺乏支持外部 Webhook 推送与多租户/多项目并行接入的标准化 HTTP 接口；
3. 缺乏数据库层面的唯一约束幂等与乐观锁事务保障。

## 决策

1. **SQLAlchemy 2.0 异步双驱动存储引擎**：
   - 采用现代化 SQLAlchemy 2.0 Declarative Mapped ORM 模型；
   - 生产环境默认连接 PostgreSQL（通过 `asyncpg`），本地开发、CI/CD 与单元测试原生支持异步 SQLite（通过 `aiosqlite`），保持 100% 异步 API 一致性与毫秒级执行。

2. **数据库级 Append-only `SqlEventStore`**：
   - 表 `events` 设立 `(project_id, event_id)` 唯一联合索引；
   - 写入时计算 `payload_hash`（SHA-256）：完全相同的事件返回 `duplicate`，相同 `event_id` 但内容不一致直接抛出 `EventIdConflictError` 拒绝入库。

3. **状态持久化与瞬时恢复 (`SqlStateStore`)**：
   - 分别设计 `tasks`、`cases`、`approval_requests` 与 `traces` 表；
   - 支持从事件日志全量重放，也支持直接持久化与加载当前 `OrgProjector.state`、`CaseLedger` 与 `ApprovalManager` 快照。

4. **FastAPI 事件网关与标准 REST API**：
   - `/api/v1/projects/{project_id}/events`：结构化事件摄入与事件流查询；
   - `/api/v1/projects/{project_id}/messages`：自然语言聊天文本摄入，自动调用 `ClaimExtractor` 抽取并入库；
   - `/api/v1/projects/{project_id}/cases`：Case 账本生命周期查询与详情；
   - `/api/v1/projects/{project_id}/approvals`：待审批列表与审批决策（批准/拒绝）Webhook 回调；
   - `/api/v1/projects/{project_id}/run-turn`：显式触发 Agent 协调轮次。

## 后果

正面影响：
- 具备跨进程、跨实例的持久化与状态恢复能力；
- 提供了标准化 Webhook 接入点，为后续接入飞书真实 Webhook（F1）打下坚实基础；
- 保持了严格的幂等性与事件溯源体系。

代价与限制：
- 引入了异步数据库会话管理与 JSON 序列化开销；
- SQLite 模式在极高并发写时需依赖连接池排队。

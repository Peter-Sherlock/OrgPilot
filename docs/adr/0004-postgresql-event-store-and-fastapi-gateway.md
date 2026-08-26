# ADR-0004：SQL 异步持久化存储与 FastAPI 事件网关

- 状态：Accepted
- 日期：2026-08-26

## 背景

在 P0、M1、M2 阶段，OrgPilot 验证了确定性状态推导、Case 状态机、Mock 闭环协调和自然语言声明抽取，但数据与状态全保存在单进程内存中：
1. 服务重启导致全部历史事件与进行中的 Case 丢失；
2. 缺乏支持外部 Webhook 推送与多租户/多项目并行接入的标准化 HTTP 接口；
3. 缺乏数据库层面的唯一约束幂等与持久化恢复。

## 决策

1. **SQLAlchemy 2.0 异步双驱动存储引擎**：
   - 采用现代化 SQLAlchemy 2.0 Declarative Mapped ORM 模型；
   - 默认使用文件型异步 SQLite；可通过 `ORGPILOT_DATABASE_URL` 显式选择 PostgreSQL（`asyncpg`）；
   - PostgreSQL 驱动兼容性不等于生产验证，部署前必须补充目标环境的迁移、并发与恢复测试。

2. **数据库级 Append-only `SqlEventStore`**：
   - 表 `events` 设立 `(project_id, event_id)` 唯一联合索引；
   - 写入时计算事件语义信封哈希（SHA-256）：完全相同的事件返回 `duplicate`，相同 `event_id` 但内容不一致直接拒绝入库；
   - 捕获并重新分类并发唯一键竞争，避免 SELECT 后 INSERT 的竞态被误报为数据库错误。

3. **状态持久化与瞬时恢复 (`SqlStateStore`)**：
   - 当前表包括 `events`、`state_snapshots`、`coordination_cases` 与 `approval_requests`；
   - 事件日志是权威来源，快照作为重放缓存；Agent 产生的任务更新事件必须先写事件库，再保存状态、Case 与 Approval 快照。

4. **FastAPI 事件网关与标准 REST API**：
   - `/api/v1/projects/{project_id}/events`：结构化事件摄入与事件流查询；
   - `/api/v1/projects/{project_id}/messages`：自然语言聊天文本摄入，自动调用 `ClaimExtractor` 抽取并入库；
   - `/api/v1/projects/{project_id}/cases`：Case 账本生命周期查询与详情；
   - `/api/v1/projects/{project_id}/approvals`：待审批列表与审批决策（批准/拒绝）Webhook 回调；
   - `/api/v1/projects/{project_id}/run-turn`：显式触发 Agent 协调轮次。

## 后果

正面影响：
- 具备单服务实例重启后的持久化与状态恢复能力；
- 提供了标准化 Webhook 接入点，为后续接入飞书真实 Webhook（F1）打下坚实基础；
- 保持了严格的幂等性与事件溯源体系。

代价与限制：
- 引入了异步数据库会话管理与 JSON 序列化开销；
- SQLite 模式在极高并发写时需依赖连接池排队。
- 当前没有 Alembic 迁移和多实例项目级锁，因此尚不能宣称生产级多实例一致性。

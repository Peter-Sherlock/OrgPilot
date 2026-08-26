# P0 架构

## 目标

P0 只验证一条可程序断言的协调链：

```text
versioned OrgEvent
        ↓
append-only Event Log
        ↓
deterministic OrgProjector
        ↓
official workflow + source-backed health claims
        ↓
DependencyAnalyzer
        ↓
CoordinationCase + candidate actions
        ↓
independent PolicyDecision
```

它不是飞书机器人，也还不是能够真实发送消息的 Agent。它是后续 Agent Loop
依赖的确定性协调内核。

## 模块边界

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| `events` | 事件契约、反序列化、幂等事件日志 | 业务状态推导 |
| `domain` | 领域词汇、状态模型、稳定异常 | 外部平台结构 |
| `state` | 按事件顺序投影当前状态 | 依赖分析、外部写入 |
| `dependencies` | 校验依赖图并传播风险影响 | 判断消息含义 |
| `coordination` | 创建证据可追溯的协调 case 和候选动作 | 授权、执行动作 |
| `policy` | 独立判断风险和审批要求 | 生成行动理由 |
| `scenarios` | 加载、回放并验证 Ground Truth | 修改投影内部状态 |

## 关键设计选择

### 官方流程状态与运行健康状态分离

`TaskState.workflow_status` 是任务系统里的正式状态；`health_status` 是依据活跃声明
计算出的运行判断。成员报告风险时，系统不得把 `doing` 偷偷改成 `blocked`。

### 事件不可变，状态可重建

原始事件使用 frozen Pydantic 模型。Event Log 对完全相同的重复事件返回
`duplicate`，对相同 ID、不同内容的事件直接拒绝。

### 声明不会覆盖证据

每次 `task.health_reported` 都产生独立 `TaskHealthClaim`。同一陈述者的新声明会将
旧声明标为 `superseded`，不同陈述者的冲突声明同时保留。当前健康状态采取保守的
最高风险值，并显式设置 `health_conflict`。

### Planner 不能自行授权

`CoordinationAction` 不包含可由 Planner 填写的 `requires_approval`。Policy Engine
根据动作类型独立产生 `PolicyDecision`。公开通知和正式任务更新默认需要审批。

## 当前数据存储

P0 的 Event Log 和投影均为进程内实现，目的是验证语义与回放确定性。接口语义稳定
后再引入 PostgreSQL；当前结果不代表持久化、并发或事务能力已经完成。

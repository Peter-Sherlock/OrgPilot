# 真实飞书受控验收手册

本手册只验证 OrgPilot 的真实租户链路，不把 Mock/离线绿灯当成真实送达证据。所有真实写入
必须使用专用测试账号或测试群，并由操作者明确授权目标与验收窗口。

## L0：离线安全预检（零网络、零写入）

保持以下配置：

```ini
ORGPILOT_COLLABORATION_ADAPTER=feishu
ORGPILOT_FEISHU_ALLOW_WRITES=false
ORGPILOT_DEMO_BOOTSTRAP=false
```

执行：

```powershell
uv run orgpilot feishu-preflight
```

退出码必须为 0，并确认适配器、凭证存在性、传输模式、演示数据开关和写入闸门均为 PASS。
命令只报告凭证是否存在，不显示 App ID、App Secret 或令牌。
闸门关闭时网关仍允许 HTTP URL verification，但拒绝业务事件处理，也不会启动 WS 或清扫
outbox。

## L1：在线鉴权预检（发送凭证、零业务写入）

在允许 App ID/App Secret 发往飞书官方鉴权接口后执行：

```powershell
uv run orgpilot feishu-preflight --online-auth
```

验收证据为 `tenant token issued (value hidden; no write performed)`。不得把令牌、请求头或
`.env` 内容复制进日志、Issue、PR 或聊天记录。

## L2：单目标真实收发（需要再次明确授权）

开始前必须同时满足：

1. 指定唯一测试 `open_id` 或测试群，禁止使用生产群和批量成员列表；
2. `ORGPILOT_DEMO_BOOTSTRAP=false`，避免注入演示任务；
3. 记录当前项目 ID，并确认 outbox 没有历史 pending/dead 记录需要人工裁决；
4. 明确允许发送的消息文本、卡片类型和最大条数；
5. 临时设置 `ORGPILOT_FEISHU_ALLOW_WRITES=true`。

推荐启动全功能网关：

```powershell
uv run uvicorn orgpilot.gateway.app:create_app --factory --port 8000
```

由测试账号向机器人发送一条约定消息，仅验证一轮：入站事件持久化、目标收到回复、outbox
状态为 delivered、`directive.delivered`（如适用）的 target 与累计 attempts 正确。不要在
这一阶段测试批量通知或真实任务改期。

## L3：任务/审批写入（独立授权）

只有 L2 证据完整后才进入。每种动作单独授权并限制为一次：审批卡、指令催办、飞书任务
截止期更新。任务更新必须使用专用测试任务 GUID；执行前后都记录原截止期，以便人工回滚。

## 结束与回滚

1. 停止网关/监听器；
2. 立即恢复 `ORGPILOT_FEISHU_ALLOW_WRITES=false`；
3. 检查 outbox，pending/dead 不得直接重放，先核对目标和载荷；
4. 如更新了测试任务，恢复原截止期；
5. 归档脱敏证据：时间、项目 ID、动作类型、目标类别、message/task id 后四位、outbox 状态、
   attempts 和用户侧可见结果。不得归档凭证、完整 token 或 Authorization header。

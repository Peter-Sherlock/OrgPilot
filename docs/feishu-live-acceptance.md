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

1. 确认测试者已经在线、准备好发送，再启动窗口。默认自动生成一次性随机挑战文本，由首个
   完全匹配的私聊用户自动绑定唯一 open_id；禁止生产群、
   批量成员列表和模糊匹配；
2. `ORGPILOT_DEMO_BOOTSTRAP=false`，避免注入演示任务；
3. 使用全新的 `feishu-l2-...` 验收标识（CLI 参数名保留为 `--project-id`），对应
   `var/feishu-acceptance/<标识>.json`。已有报告不可覆盖或复用；此模式不读写业务数据库；
4. 只允许一次固定文本发送尝试；非文本、非私聊、非用户、缺失 open_id、不匹配消息及卡片均忽略。
   挑战只是校验文本，不进入 GatewayService、LLM 或任务/指令处理器；
5. 环境中的 `ORGPILOT_FEISHU_ALLOW_WRITES` 必须继续为 false。L2 命令只为本次有界进程
   构造写入客户端，不永久修改 `.env`。

执行一次性命令：

```powershell
uv run orgpilot feishu-l2-smoke `
  --project-id "feishu-l2-<日期>-<随机码>" `
  --timeout-seconds 300 `
  --confirm-single-reply
```

命令最多等待 30 秒连接，出现 `[READY]` 后立即显示本轮挑战文本；此时复制到机器人私聊。
消息受理窗口为 30–300 秒，之后另有有界线程清理。可用 `--challenge` 指定约定文本，但仍须
保持唯一，且只有固定回复会被执行。未经测试者准备确认，不连续启动多个空等窗口。

本地 JSON 报告在发送前记录入站消息与目标的 SHA-256 截断指纹以及已占用的单次发送额度；
不保存消息正文、完整 ID、挑战、凭证或 SDK 连接地址。报告位于已忽略的 `var/`，不提交 Git。

| 字段 | 解释与下一步 |
| --- | --- |
| `connected` | 当前窗口是否确认 WebSocket 连接建立 |
| `received_messages` / `filtered` | 消息回调次数与过滤原因；0 不能单独证明后台配置有误，也可能测试者未发送 |
| `matched_messages` / `send_attempts` | 匹配次数与发送尝试，均不应超过 1 |
| `reply_status=not_attempted` | 没有发送尝试，结合过滤原因检查输入和订阅 |
| `reply_status=api_accepted` | 平台返回成功码和消息 ID；仍需测试账号确认实际可见 |
| `reply_status=unknown` | 请求已尝试但最终结果无法确认，先去飞书核对，禁止自动重发 |
| `listener_stopped` | SDK 连接清理与自有线程退出是否确认；false 不能宣称已正常关闭 |
| `user_visible_confirmed` | 工具始终写 false，用户可见确认须另行记录，不能自动推断 |

L2 通过必须同时具备连接、匹配、单次 API 接受、确认关闭与用户可见证据。此路径不产生业务事件
或 outbox；后续 L3 才验证真实业务摄入、审批、outbox delivered/attempts 与 `directive.delivered`。
2026-09-04 的 `feishu-l2-20260904-080302-4baeaeb9` 已满足上述 L2 证据：单次匹配、单次 API
接受、用户确认固定回复可见、窗口关闭且进程退出。人工确认独立保存于同目录
`<验收标识>.user-confirmation.json`，原始机器报告保持不变。
该次退出的 SDK 缓存清理告警已随后完成代码修复与真实 SDK 离线回归，未为修复重发消息。
历史报告保留当时结果，不宣称修复后的线上关闭已重新验证；L3 仍须单独限定动作与授权。
详见[开发记录](development-log.md)。此结果不构成其他目标或业务动作的授权。

## L3：任务/审批写入（独立授权）

只有 L2 证据完整后才进入。每种动作单独授权并限制为一次：审批卡、指令催办、飞书任务
截止期更新。任务更新必须使用专用测试任务 GUID；执行前后都记录原截止期，以便人工回滚。

### L3-A：一张测试审批卡与一次选择

工具已实现，当前仅完成离线验证。首先可查看固定内容，无需配置或网络：

```powershell
.\.venv\Scripts\python.exe -m orgpilot feishu-l3-approval --preview
```

卡片标题为“OrgPilot L3 审批链路测试”；正文说明只记录审批选择，不创建/修改任务、不催办、
不追加消息；按钮为“同意测试”和“拒绝测试”。允许本轮测试卡后，用全新验收标识启动：

```powershell
.\.venv\Scripts\python.exe -u -m orgpilot feishu-l3-approval --run-id feishu-l3-<唯一标识> --timeout-seconds 300 --confirm-single-card
```

1. 保持环境写闸 false、演示注入 false、WebSocket 模式 true；测试者准备好机器人私聊。
2. `[READY]` 后将本轮 `ORGPILOT-L3:...` 挑战原样私聊发送一次，发送者绑定为唯一审批人。
3. 看到测试卡后点击一个按钮一次；两种选择都能验证链路，均不执行任务操作。
4. 收到“已记录”提示后在本任务确认看到了卡片、点击哪一项及是否看到了提示。未收到结果时
   先核对报告，不自动重发；超时后原卡片会保留在飞书里，但按钮不再推进本轮状态。

报告与独立 SQLite 在 `var/feishu-acceptance/<run-id>/`，不读写业务 `orgpilot.db`。
outbox 的 `held` 表示发送额度已占用、禁止自动恢复；只有收到平台成功码及消息 ID 才转为
`delivered`。held 的 attempts=1 不等于确定送达；需先人工核对。目录已有任何内容就不复用。

通过条件：`matched_messages=1`、`send_attempts=1`、`reply_status=api_accepted`、
`outbox_status=delivered`、`accepted_callbacks=1`、`approval_status=approved/rejected`、
`approval_consumed=false`、确认退出，且用户确认可见。回调核对操作者、卡片消息 ID、本轮
审批 ID 及随机 nonce；机器报告不自动推断卡片/toast 可见，也不把此结果当作完整 L3 业务验收。
详细边界见 [ADR-0011](adr/0011-isolated-feishu-approval-acceptance.md)。

## 结束与回滚

1. 停止网关/监听器；
2. 确认环境中的 `ORGPILOT_FEISHU_ALLOW_WRITES` 仍为 false；
3. 检查 outbox，pending/dead 不得直接重放，先核对目标和载荷；
4. 如更新了测试任务，恢复原截止期；
5. 归档脱敏证据：时间、验收标识、动作类型、目标/消息指纹、API 返回状态、关闭结果和用户侧
   可见确认；L3 额外记录 outbox/任务验证。不归档凭证、完整 token 或 Authorization header。

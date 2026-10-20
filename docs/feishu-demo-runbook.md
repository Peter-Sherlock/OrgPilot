# 真实飞书演示手册

目标：在真实飞书租户里录一段 2–3 分钟的视频，展示一条完整链路：

> 成员私聊报延期 → 系统判定延期并算出下游影响 → PM 收到改期审批卡片 → PM 批准 →
> 截止时间更新、卡片原地变为"已完成" → 控制台和 `team show` 显示新的截止时间

本手册的第 3、4 节由 `tests/test_feishu_demo_script.py` 按同样的顺序、同样的消息自动跑过
（只把飞书 OpenAPI 客户端换成了 Mock）。**真实租户里还没有完整跑过**，第一次请按每一步的
"检查点"逐项核对；哪一步不符合，先看第 6 节。

## 1. 准备

- **两个飞书账号**，在同一个企业（测试企业即可）：
  - A：你自己，演示里的 PM；
  - B：同学或朋友，演示里的工程师。只有一个账号也能跑，但 PM 只能兼任负责人，说服力会打折扣。
- **飞书自建应用**：按 [`feishu-setup-guide.md`](feishu-setup-guide.md) 第一步到第三步完成——机器人能力、
  消息与通讯录权限、**长连接**方式订阅消息事件和卡片回调。应用的可用范围要包含 A 和 B。
- **运行目录**：在一个检出了最新 `main` 的目录里运行。`.env` 和 `team.yaml` 都被 `.gitignore`
  忽略，需要从之前实测用的目录复制过去。
- **（可选）DeepSeek key**：用 LLM 理解消息时需要。不配置也能跑，第 4 节的演示语句在
  启发式客户端下同样有效。

## 2. 配置

`.env`（仓库根目录）：

```dotenv
ORGPILOT_COLLABORATION_ADAPTER=feishu
FEISHU_APP_ID=<你的 app id>
FEISHU_APP_SECRET=<你的 app secret>
ORGPILOT_FEISHU_PROJECT_ID=feishu-project
ORGPILOT_DEMO_MODE=false
# 改期只更新 OrgPilot 里的任务（名单导入的任务不在飞书任务里），保持 false
ORGPILOT_FEISHU_TASK_WRITEBACK=false

# 可选：用 LLM 理解自然语言
# ORGPILOT_LLM_PROVIDER=deepseek
# DEEPSEEK_API_KEY=<你的 key>
# ORGPILOT_INTENT_CLIENT=llm
```

`team.yaml`：把 `mobile`（或 `email`）换成 A、B 的真实值。`task-payment` 的截止时间设为
**演示当天 18:00**，只要早于第 4 节消息里的"明天下午 5 点"就会被判定为延期：

```yaml
project: feishu-project
members:
  wang: {mobile: "<A 的手机号>", name: 王鹏飞, role: pm}
  peer: {mobile: "<B 的手机号>", name: 同学A, role: engineer}
tasks:
  - id: task-payment
    title: 支付SDK接入
    owner: peer
    status: doing
    deadline: <演示当天> 18:00
  - id: task-checkout
    title: 收银台前端结账
    owner: wang
    deadline: <演示当天 + 2 天> 18:00
    depends_on: [task-payment]
  - id: task-qa
    title: 支付全链路验收
    owner: wang
    deadline: <演示当天 + 3 天> 18:00
    depends_on: [task-checkout]
```

从干净的数据库开始（之前实测留下的数据会干扰演示），然后导入：

```powershell
Remove-Item .\orgpilot.db* -ErrorAction SilentlyContinue
uv run orgpilot team import team.yaml --dry-run
uv run orgpilot team import team.yaml
uv run orgpilot team show
```

**检查点**：预览里是 2 个成员、3 个任务；`team show` 里两个成员都有 `ou_` 开头的 open_id，
王鹏飞的角色是 `pm`。

## 3. 启动

```powershell
uv run uvicorn orgpilot.gateway.app:create_app --factory --port 8000
```

网关会在同一个进程里建立飞书长连接，**不要再同时运行 `orgpilot start-feishu-ws`**：两个连接会
分走同一个应用的事件。

**检查点**：

- 启动日志里没有飞书鉴权或长连接的报错（OrgPilot 自己的 INFO 日志默认不显示，看不到"连接成功"属于正常）；
- 浏览器打开 `http://localhost:8000/`，项目 ID 是 `feishu-project`，能看到 3 个任务的 DAG；
- 让 B 给机器人发一句 `/帮助`，能收到回复。这是长连接真正通了的唯一可靠检查，收不到就先别录，看第 6 节。

## 4. 演示脚本（录屏）

建议分屏：左边 B 的飞书，右边 A 的飞书，下方或切换到浏览器控制台。

| 步骤 | 谁 | 操作 | 应该看到 |
|---|---|---|---|
| 1 | B | 私聊机器人：`支付 SDK 报错，排查需要到明天下午 5 点` | B 收到：`已记录：支付SDK接入 → 延期，预计 <明天> 17:00 完成（阻塞：支付 SDK 报错）`（用 LLM 客户端时阻塞原因的措辞可能略有不同） |
| 2 | A | （不用操作） | A 收到"⚠️ 任务改期调整审批请求"卡片：受影响任务 `task-payment` 支付SDK接入，建议新截止时间 `<明天> 17:00`，波及下游 `收银台前端结账`、`支付全链路验收` |
| 3 | — | 切到控制台刷新 | DAG 里支付SDK接入为延期，下游两个任务受影响；审批台里有一条待审批（控制台的具体样式没在真实租户里核对过，以实际显示为准） |
| 4 | A | 在卡片上点"🟢 批准改期" | 卡片原地变为"任务改期审批 [已完成]"，写着审批人王鹏飞和审批时间 |
| 5 | — | 刷新控制台；终端运行 `uv run orgpilot team show` | 支付SDK接入的截止时间变成 `<明天> 17:00` |

讲解要点：B 说的是一句自然语言，系统做了四件事——识别出这是状态汇报、把"明天下午 5 点"
解析成具体时间、判断它晚于截止时间所以是延期、沿依赖图算出两个下游任务受影响；改期是高影响
动作，所以只发审批卡片给 PM，不会自己改。

### 可选加演

- **重新导入不回滚审批**：运行 `uv run orgpilot team import team.yaml --dry-run`。team.yaml 里
  的截止时间还是今天 18:00，但预览不会把已批准的改期改回去——任务字段按"上次导入 / 当前名单 /
  运行时"三方比对，运行时改过而名单没改，保留运行时的值。
- **全员同步**：A 发 `/同步`。B 和 A 都会收到进度探针（A 也负责任务）。B 回复例如
  `支付SDK今晚8点前修好`；**A 也要回复自己的探针**，否则简报要等会话超时（24 小时）才生成。
  两人都回复后，A 收到汇总简报。
- **拒绝路径**：重来一遍（第 2 节清库重导），第 4 步改点"🔴 拒绝申请"，协调事项会升级。

## 5. 重来一遍

停掉 uvicorn（Ctrl+C），回到第 2 节从清库开始。

演示中途重启服务（例如改了配置）不会丢掉进行中的追问、同步确认卡片和 `/同步` 会话：它们保存在
同一个 `orgpilot.db` 里，重启后接着处理（ADR-0013）。只有清库才会把它们一起清掉。注意两点：

- 发送和保存不是同一个事务。如果恰好在“消息已发出、状态还没保存”的瞬间停掉服务，简报可能再发一次；
  刚发起的同步可能没保存下来，需要重新 `/同步`，成员会收到两遍探针；
- 追问 24 小时、确认卡片 30 分钟、同步会话空闲 24 小时的期限从原来的时间算起，不因重启重新计时。

## 6. 出问题时

| 现象 | 先查什么 |
|---|---|
| 机器人不回复 | uvicorn 日志有没有报错；`.env` 里是否 `ORGPILOT_COLLABORATION_ADAPTER=feishu`；是否同时开着 `start-feishu-ws` |
| B 收到"没理解，可以换个说法吗" | LLM 客户端超时或 key 有误时会这样降级；先去掉三行 LLM 配置，用启发式客户端跑 |
| B 收到的是追问而不是"已记录" | 消息里缺时间时系统会追问，按追问补一句时间即可；录屏时用第 4 节的原句 |
| A 收不到审批卡片 | `team show` 里 A 的角色是否为 `pm`、open_id 是否正确；日志里有没有发送失败 |
| 点"批准"没有反应 | 开发者后台是否通过长连接订阅了卡片回调；日志里有没有 card action 的记录 |
| 审批人显示成 `ou_` 开头的 ID | 点卡片的人不在名单里，或名单里的 open_id 和点卡片的账号对不上 |

## 7. 讲的时候注意边界

- 改的是 **OrgPilot 里的任务截止时间**，不是飞书任务：名单导入的任务不在飞书任务里。
  `ORGPILOT_FEISHU_TASK_WRITEBACK=true` 只适用于任务 ID 本身就是飞书任务 guid 的项目，这条路径
  只在 Mock 上测过；
- 只处理私聊，群聊消息直接拒收；
- 这是两个人的演示，不代表多人团队里的效果；协调层的对比实验还没做。

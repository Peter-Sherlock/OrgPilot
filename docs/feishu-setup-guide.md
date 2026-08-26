# 飞书开放平台 HTTP Webhook 配置指南

本阶段只支持 HTTP Webhook。WebSocket 长连接、飞书多维表格和日历尚未实现。

---

### 第一步：创建企业自建应用

1. 打开 [飞书开放平台开发者后台](https://open.feishu.cn/app) 并登录；
2. 点击 **“创建企业自建应用”**：
   - **应用名称**：`OrgPilot 协调助手`
   - **应用描述**：`基于多智能体内核的组织风险发现与排期协调助手`
   - 上传一个机器人图标；
3. 创建成功后，在 **“凭证与基础信息”** 页面，复制保存以下两个凭据：
   - **App ID** (如 `cli_a1b2c3d4e5...`)
   - **App Secret** (如 `secret_xxxxxxxxxxxx...`)

---

### 第二步：添加机器人能力

1. 在左侧菜单栏点击 **“添加应用能力”** -> 选择 **“机器人”** -> 点击 **“添加”**；
2. 开启机器人能力后，OrgPilot 就可以在飞书内收发私聊消息、拉入群聊并发送交互卡片。

---

### 第三步：配置权限 (Permissions)

在左侧菜单栏点击 **“权限管理”**，搜索并开通以下必要权限：
- `im:message`（获取与发送单聊/群聊消息）
- `im:message.p2p_msg:readonly`（读取单聊消息）
- `im:message.group_at_msg:readonly`（读取群聊中 @机器人的消息）
- `task:task:v2` / `task:task:v2:readonly`（创建与更新飞书任务）
- `contact:user.id:readonly`（获取用户基础信息）

> 💡 开通权限后，点击页面右上角 **“创建版本”** 并发布（自建应用通常免审或企业管理员一键同意）。

---

### 第四步：配置事件订阅 (Event Subscription)

在左侧菜单栏点击 **“事件订阅”**：

#### HTTP Webhook 模式
- **请求网址 URL**：`https://<your-domain>/api/v1/feishu/events`
- 添加事件订阅：
  - `im.message.receive_v1`（接收消息）
  - `card.action.trigger`（卡片回传交互/按钮点击）

---

### 第五步：在 OrgPilot 中配置并启动

设置运行环境变量。真实密钥不得提交到 Git：

```powershell
$env:ORGPILOT_COLLABORATION_ADAPTER = "feishu"
$env:ORGPILOT_FEISHU_PROJECT_ID = "feishu-project"
$env:FEISHU_APP_ID = "<set securely>"
$env:FEISHU_APP_SECRET = "<set securely>"
$env:FEISHU_VERIFICATION_TOKEN = "<required>"
$env:ORGPILOT_API_TOKEN = "<required for project REST APIs>"
```

启动网关服务：
```powershell
uv run uvicorn orgpilot.gateway.app:create_app --factory --port 8000
```

启动后先完成 URL Challenge，再用单独测试账号做消息、审批和任务改期 smoke test。操作者的飞书 `open_id` 必须与项目中登记的审批人 ID 一致。

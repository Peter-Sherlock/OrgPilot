# 飞书开放平台配置与本地长连接部署指南

OrgPilot 支持 **WebSocket 长连接（官方推荐，零公网依赖）** 与 **HTTP Webhook** 两种接入模式。

---

### 第一步：创建企业自建应用

1. 打开 [飞书开放平台开发者后台](https://open.feishu.cn/app) 并登录；
2. 点击 **“创建企业自建应用”**：
   - **应用名称**：`OrgPilot 协调助手`
   - **应用描述**：`基于多智能体内核的组织风险发现与排期协调助手`
   - 上传机器人图标；
3. 创建成功后，在 **“凭证与基础信息”** 页面，复制保存：
   - **App ID** (如 `cli_a1b2c3d4e5...`)
   - **App Secret** (如 `secret_xxxxxxxxxxxx...`)

---

### 第二步：添加机器人能力与配置权限

1. 在左侧菜单栏点击 **“添加应用能力”** -> 选择 **“机器人”** -> 点击 **“添加”**；
2. 在左侧菜单栏点击 **“权限管理”**，搜索并开通以下必要权限：
   - `im:message`（获取与发送单聊/群聊消息）
   - `im:message.p2p_msg:readonly`（读取单聊消息）
   - `im:message.group_at_msg:readonly`（读取群聊中 @机器人的消息）
   - `task:task:v2` / `task:task:v2:readonly`（创建与更新飞书任务）
   - `contact:user.id:readonly`（获取用户基础信息）
3. 开通权限后，点击右上角 **“创建版本”** 并发布。

---

### 第三步：配置长连接事件与回调（零内网穿透）⭐

1. 在左侧菜单栏点击 **“事件与回调”** -> **“回调配置”**：
   - 订阅方式选择：**【使用 长连接 接收回调 (推荐)】**；
   - 点击 **保存**。
2. 在 **“事件配置”** 与 **“已订阅的回调”** 中添加：
   - `im.message.receive_v1`（接收消息）
   - `card.action.trigger`（卡片按钮回调）
3. 再次发布一个新版本使事件订阅生效。

---

### 第四步：在本地配置环境变量并启动

在项目根目录下创建或编辑 `.env` 文件（或在 PowerShell 中设置环境变量）：

```ini
# 启用飞书适配器与 WebSocket 长连接
ORGPILOT_COLLABORATION_ADAPTER=feishu
ORGPILOT_FEISHU_USE_WS=true
ORGPILOT_FEISHU_PROJECT_ID=feishu-project

# 飞书应用凭证
FEISHU_APP_ID=cli_xxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# （可选）大模型语义抽取配置
# ORGPILOT_LLM_PROVIDER=aihubmix
# AIHUBMIX_API_KEY=your_key_here

# （可选）单人体验演示：首次收到消息且项目无任务时，自动注入演示任务链。
# 默认关闭；生产环境请保持 false。
# ORGPILOT_DEMO_BOOTSTRAP=true
# Directive lifecycle thresholds (minutes until reminder / escalation)
# ORGPILOT_DIRECTIVE_REMINDER_MINUTES=60
# ORGPILOT_DIRECTIVE_ESCALATION_MINUTES=1440
```

#### 启动方式 A：启动全功能网关（Web 看板 + 飞书 WebSocket 长连接）
```powershell
uv run uvicorn orgpilot.gateway.app:create_app --factory --port 8000
```
- 服务启动后会自动向飞书建立安全 WebSocket 长连接；
- 同时在 `http://localhost:8000/` 提供实时 DAG 拓扑大盘与时间线。

#### 启动方式 B：独立运行飞书长连接监听器
```powershell
uv run orgpilot start-feishu-ws
```

---

### 第五步：在飞书桌面端测试验证

1. 打开 **飞书桌面端客户端**；
2. 搜索您的机器人（例如 `@OrgPilot 协调助手`），发送消息：
   > *“支付 SDK 报错，排查需要到明天下午 5 点”*
3. 机器人将通过 WebSocket 长连接即时捕获消息，识别风险并返回交互式卡片！
4. 点击飞书开放平台“回调配置”页面的 **【验证】** 按钮，系统会显示连接成功状态！

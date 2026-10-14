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
   - `contact:user.id:readonly`（通过手机号或邮箱获取用户 ID，`orgpilot team import` 用它把成员的邮箱或手机号换成 open_id）
3. 在 **“权限管理”→“数据权限”/“通讯录权限范围”** 中，把团队成员所在的部门或人员加入可访问范围。范围外的成员查不到 open_id，导入会中止并列出这些成员；
4. 开通权限后，点击右上角 **“创建版本”** 并发布。

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

# 仅演示：第一个给空项目发消息的人自动成为 PM 并获得 3 个演示任务。
# 真实团队保持 false，用下一步的 team.yaml 导入成员和任务。
ORGPILOT_DEMO_MODE=false

# （可选）大模型语义抽取配置
# ORGPILOT_LLM_PROVIDER=aihubmix
# AIHUBMIX_API_KEY=your_key_here
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

### 第五步：导入团队名单（team.yaml）

机器人只处理名单里成员的私聊消息，名单外的人会收到“请联系项目负责人”的提示。

1. 复制示例文件并填写真实成员和任务（`team.yaml` 已被 Git 忽略，因为里面有同事的邮箱和手机号）：
   ```powershell
   Copy-Item team.example.yaml team.yaml
   ```
   - `project` 必须等于 `ORGPILOT_FEISHU_PROJECT_ID`；
   - 每个成员填 `email` 或 `mobile`，也可以两个都填（先按邮箱查，查不到再按手机号查）；
   - `email` 必须是**飞书账号绑定的邮箱**。在管理后台填写的“工作邮箱 / 企业邮箱”查不到（2026-10-14 实测）；用手机号注册、没有绑定邮箱的成员，直接填 `mobile`；
   - `mobile` 中国大陆号码直接写 11 位数字，其他国家和地区写 `+区号`；
   - 至少一名成员的 `role` 为 `pm`：只有 pm 能发起全员同步、接收改期审批。
2. 先预览，确认无误后再写入：
   ```powershell
   uv run orgpilot team import team.yaml --dry-run
   uv run orgpilot team import team.yaml
   uv run orgpilot team show
   ```
3. 之后修改 `team.yaml` 再导入，只会写入差异：新增成员和任务、任务的标题 / 负责人 / 截止时间 / 状态变化。成员改名改角色、改依赖、删除不会执行，只在输出里列出。
   如果某个字段在运行时被改过（例如 PM 批准了改期），而 `team.yaml` 里这一项自上次导入后没动，导入会保留运行时的值。

---

### 第六步：在飞书桌面端测试验证

1. 打开 **飞书桌面端客户端**；
2. 用名单里的账号搜索您的机器人（例如 `@OrgPilot 协调助手`），发送消息：
   > *“支付 SDK 报错，排查需要到明天下午 5 点”*
3. 机器人将通过 WebSocket 长连接即时捕获消息，识别风险并返回交互式卡片！
4. 点击飞书开放平台“回调配置”页面的 **【验证】** 按钮，系统会显示连接成功状态！

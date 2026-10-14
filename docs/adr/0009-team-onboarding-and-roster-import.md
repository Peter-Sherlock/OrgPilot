# ADR-0009：团队接入与成员 / 任务名单导入（P-1）

- 状态：**Accepted**（2026-10-13 实现于 `feat/team-onboarding`；2026-10-14 按测试企业实测补充手机号标识，见第 3 节“实测修订”）
- 日期：2026-10-13

## 背景

内测前的第一个阻塞项是团队接入（P-1）。当前飞书上成员和任务只有两个来源：

1. `feishu/webhook.py` 的自动初始化：项目里**没有任务**时，第一个发消息的人被注册为 `pm`，并灌入 3 个演示任务；
2. `POST /projects/{id}/bootstrap-sandbox`：给 Web 沙箱写入固定的 PM / Alice / Bob / David 与演示任务链。

两者都只服务演示。没有成员名单、角色配置和真实任务导入，第二个人发消息会被路由层以 `sender_not_registered` 拒收。

梳理代码时还发现两个必须在本阶段一起处理的问题：

- **自动初始化可能写入毒事件。** 初始化的触发条件是“没有任务”，并且直接调用 `event_store.append`，绕过了 B4 修复引入的“先在内存投影副本上校验”。一旦项目里已有成员但还没有任务（P-1 允许只导入成员时就会出现），第一条消息会为已注册成员再写一条 `member.registered`，之后每次重放都抛 `member ... already exists`，项目永久不可用；
- **角色口径不一致。** 审批升级对象是 `pm` / `manager` / `lead`（`coordination/service.py` 的 `_find_manager_targets`），而发起全员同步只认 `pm`（`intent/router.py`、`intent/handlers.py`）。`lead` 能收审批却不能发起同步。

## 决策

### 1. 范围

本版实现：

- `team.yaml` 描述成员与任务，CLI 导入；
- 成员用**邮箱或手机号**标识，导入时调用飞书通讯录接口换成 open_id（初稿只用邮箱，实测后补充手机号）；
- 重复导入按**差异**生成事件，删除类变化只报告不执行；
- 自动初始化收进 demo 模式，并修复毒事件问题；
- 角色口径统一为“只有 `pm` 有特权”。

非目标：

- 导入**不主动给成员发消息**。告知同事属于 P-4；
- 不新增成员更新 / 停用、任务删除、依赖变更等事件类型；
- 不提供 REST 导入接口，只提供 CLI；
- 不处理 `bootstrap-sandbox` 端点（它服务 Web 沙箱，独立于飞书项目）。

### 2. `team.yaml` 格式

```yaml
project: pay-v2            # 必须与 --project 一致，防止导错项目

members:                   # key 是文件内别名，只在本文件中用于引用
  wang:
    email: wang@example.com
    name: 王鹏飞
    role: pm
  alice:
    mobile: 18000000001     # 邮箱和手机号至少填一个
    name: 支付工程师 Alice
    role: engineer

tasks:
  - id: task-payment
    title: 支付SDK接入
    owner: alice           # 引用成员别名
    status: doing          # todo | doing | blocked | review | done，默认 todo
    deadline: 2026-10-24 18:00
  - id: task-checkout
    title: 收银台前端结账
    owner: alice
    deadline: 2026-10-27 18:00
    depends_on: [task-payment]
```

格式规则（Pydantic，`extra="forbid"`，未知字段直接报错）：

- 每个成员至少有 `email` 或 `mobile` 之一；手机号去掉空格、横线和 `+86` 前缀后保存，其他国家和地区保留 `+区号`；
- 成员别名、邮箱（不区分大小写）、手机号、任务 ID 各自唯一；
- `owner` 必须引用本文件中的成员别名；
- `depends_on` 可以引用本文件中的任务，也可以引用项目里已有的任务；
- `deadline` 必须写到分钟（`YYYY-MM-DD HH:MM`）或带时区偏移的 ISO 时间。不带时区时按业务时区（`ORGPILOT_TIMEZONE`）解释。**只写日期会被拒绝**，避免“当天几点算截止”的歧义；
- `role` 是任意非空字符串，`pm` 有特权（见第 7 节）。值为 `manager` / `lead` 时给出警告；
- 导入后项目里必须至少有一个 `pm`，否则审批没有接收人、也没人能发起同步，直接报错；
- YAML 中重复的键（例如两个同名成员别名）直接报错，而不是像 PyYAML 默认那样静默保留后一个；
- 真实的 `team.yaml` 含同事邮箱和手机号，已加入 `.gitignore`；仓库只跟踪 `team.example.yaml`。

### 3. 邮箱 / 手机号 → open_id 解析

`AsyncFeishuClient` 新增 `batch_get_open_ids(emails, mobiles)`，调用 `POST /open-apis/contact/v3/users/batch_get_id?user_id_type=open_id`：

- 每次请求最多 50 个邮箱加 50 个手机号，超过时分批，邮箱和手机号放在同一个请求里；`include_resigned=false`；
- 以响应中的 `email` / `mobile` 字段回填映射，缺少 `user_id` 的条目视为未解析；
- **先按邮箱查，查不到再用手机号**；两者都查到但指向不同用户时报错；
- **只要有一个成员解析失败，整个导入中止**，并列出失败的成员和常见原因：用户不在应用的通讯录权限范围内；用户已离职；邮箱不是账号绑定的邮箱（管理后台填的“工作邮箱”查不到）；
- 接口本身报错（例如缺少 `contact:user.id:readonly` 返回 `Access denied`）时，CLI 输出接口信息和要检查的权限，不再抛出 Python 异常；
- 两个别名解析到同一个 open_id 时报错；
- 邮箱和手机号**只用于解析，不写入事件**。事件里只保存 open_id、名字和角色；错误信息里的手机号中间四位打码；
- `MockFeishuClient` 增加邮箱和手机号 → open_id 的目录，供离线测试使用。

**实测修订（2026-10-14，飞书测试企业）**：

- 未匹配时，接口返回 `code: 0`，对应条目只有 `{"email": "..."}`，**没有 `user_id`**，而不是省略这一条。这解决了初稿里“文档未说明、待确认”的问题；
- 用手机号注册的账号，资料里没有账号邮箱。在管理后台把同一个地址填进“工作邮箱（Work email）”之后，按邮箱仍查不到；按手机号能直接查到。因此初稿“只按邮箱查”在手机号注册的租户里不可用，补充了 `mobile`；
- 实测过程中发现，`OrgPilotSettings.from_env` 会把 `./.env` 写进 `os.environ`，并且在整个进程里一直生效。worktree 里放了真实凭据后，有一个测试真实调用了一次 `batch_get_id`（只读，查询的是 example.com 示例邮箱）。现在测试用一个自动生效的 fixture 屏蔽 `.env` 加载。

需要飞书凭据（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`）。`--dry-run` 同样会调用这个只读接口。

### 4. 导入语义：差异计划

导入分两步：先**计划**（`plan_import(spec, state, open_ids, now)`，纯函数），再**应用**。`--dry-run` 只输出计划。

成员按 open_id、任务按 ID 与当前投影状态比对。任务字段做**三方比对**（见下文），其余规则如下：

| 变化 | 动作 |
|---|---|
| 新成员 | `member.registered` |
| 新任务 | `task.created`（新任务之间按依赖拓扑序生成） |
| 任务标题 / 负责人 / 截止时间变化 | `task.updated`（只带变化的字段），受三方比对约束 |
| 任务状态变化 | `task.workflow_changed`（带 `from_status`），受三方比对约束 |
| 截止时间从有变为无 | 跳过并报告（`task.updated` 无法清除截止时间） |
| 成员改名、改角色 | 跳过并报告（没有对应事件类型） |
| 任务依赖变化 | 跳过并报告（`task.updated` 不含依赖） |
| 项目里有、yaml 里没有的成员或任务 | 只报告，不删除 |

**三方比对**（实现时补充）。截止时间和状态在运行时也会变：PM 批准改期、成员在聊天里汇报状态。如果只和当前状态比对，PM 拿旧的 `team.yaml` 重新导入（比如只是想加一个新成员），会把已批准的改期悄悄改回去。因此每个任务字段以“上次导入写入的值”为基准（从事件历史中 `source_ref` 以 `team-import@` 开头的事件求得）：

| yaml 值 vs 基准 | 当前值 vs 基准 | 动作 |
|---|---|---|
| 等于当前值 | — | 无变化 |
| 等于基准（yaml 没改） | 不等（运行时改过） | 保留运行时的值，报告 `=` |
| 不等（yaml 改了） | 等于基准（运行时没改） | 写入 yaml 的值 |
| 不等 | 不等，且 yaml ≠ 当前值 | 冲突：不修改，报告 `!` |
| 没有基准（任务不是导入创建的，如演示任务） | — | 写入 yaml 的值 |

计划输出示例：

```text
$ orgpilot team import team.yaml --project pay-v2 --dry-run
  + member  david   QA 测试 David (qa)
  + task    task-refund   退款流程   owner=alice
  ~ task    task-payment  deadline 10-20 18:00 → 10-24 18:00
  ! member  bob     角色 engineer → lead：不支持修改，已跳过
  ! task    task-old      不在 team.yaml 中：不删除
  = task    task-qa       截止运行时已改为 10-30 18:00，team.yaml 仍是 10-28 18:00：保留运行时的值
将写入 3 个事件，跳过 2 项（dry-run，未写入）
```

### 5. 事件生成、校验与幂等

- 成员事件 `source=human`，任务事件 `source=task`（与沙箱、演示初始化的惯例一致；`source` 不参与任何业务判断）；`source_ref=team-import@<文件 sha256 前 12 位>`，与文件名无关，便于追溯是哪一版名单产生的，也是三方比对识别“导入写入的值”的依据；`actor_id` 为空（导入不是某个成员的动作，投影要求非空 actor 必须是已注册成员）；
- `occurred_at` / `received_at` 由调用方传入的 `now` 决定，规划函数本身不读墙钟；
- **创建类事件 ID 确定**：`team-import:<project>:member:<open_id>`、`team-import:<project>:task:<task_id>`。并发重复导入时由事件表唯一约束去重；
- **更新类事件 ID 含本次导入批次**：`team-import:<project>:<run_id>:task:<id>:updated|workflow`，`run_id` 由文件摘要和导入时刻组成。**不用内容哈希**，原因是：截止时间经导入 A→B→A→B 往返时，第四次导入的内容哈希与第二次相同，会被事件表当作重复而静默丢弃。更新的幂等由“差异为空就不生成事件”保证；
- **整批先校验后写入**：计划出的事件经 `GatewayService.commit_extracted_events` 在投影副本上依次 `apply`，任何一条失败则零写入。导入、消息抽取和演示初始化走同一条路径；
- **依赖成环**在计划阶段检测：新任务按拓扑序创建，无法排序即报错。由于依赖只能在创建时声明、且必须指向已存在的任务，逐条创建的依赖图天然无环，不需要额外的写入后检查；
- 写入是逐条追加，不是单个事务。中途崩溃时已写入的是一个**已校验序列的前缀**，前缀本身投影合法；重新执行导入会按差异补齐剩余部分。

### 6. CLI

```text
orgpilot team import <team.yaml> [--project ID] [--dry-run]
orgpilot team show [--project ID]
```

- `--project` 默认取 `ORGPILOT_FEISHU_PROJECT_ID`，必须与文件里的 `project` 一致；
- 直接写 `ORGPILOT_DATABASE_URL` 指向的数据库。网关每次请求都会从事件库重放，所以导入后无需重启网关；
- `team show` 列出当前成员（别名不持久化，显示名字、角色和 open_id 前缀）与任务，用来核对导入结果；
- 导入时刻在 CLI 边界读取一次墙钟，作为 `now` 传入；计划函数本身不读墙钟。

### 7. 角色口径

- 只有 `pm` 能发起全员同步、接收审批升级。`_find_manager_targets` 改为只认 `pm`；
- 其他角色（`engineer`、`qa`、`designer` 等）只是展示标签；
- 导入时遇到 `manager` / `lead` 给出警告：这两个角色过去会收到审批，现在不会。

### 8. demo 模式与自动初始化修复

- 新增 `ORGPILOT_DEMO_MODE`，**默认 `false`**。关闭时没有自动初始化，未注册发送者按现状拒收；
- 打开时，初始化的触发条件从“没有任务”改为“**没有成员**”，并通过第 5 节的批量校验写入，不再直接 `append`；
- 欢迎语“已为您自动初始化体验项目”只在 demo 初始化时发送；
- 未注册提示改为引导用户联系项目负责人，把自己加入 `team.yaml`。

## 备选方案

- **在未注册回复里附 open_id，由 PM 填进 yaml**：不需要新权限、不调外部 API，但要成员和 PM 来回转发一串 ID，团队规模一大就不可行；
- **只增不改**：实现最简单，但改一次截止时间就得绕开名单，名单很快和实际状态脱节；
- **完整同步**（yaml 是唯一真相，支持删除 / 改角色 / 改依赖）：要新增多种事件类型，并处理“被删成员名下还有任务”等语义，超出 P-1 解除阻塞的目标，留待内测反馈；
- **REST 导入接口**：要额外设计鉴权和上传，CLI 足以覆盖“PM 维护一份名单”的场景。

## 后果

正面：

- 团队成员可以按名单接入，第二个人发消息不再被拒收；
- 名单可放进版本控制，`source_ref` 能追溯每个事件来自哪一版名单；
- 消除了自动初始化写入毒事件的隐患，审批与同步的角色口径一致。

负面 / 风险：

- 导入依赖真实飞书接口和新权限，离线环境只能用 Mock 目录；
- 成员角色写错后无法通过重新导入修正，只能等后续的成员更新事件；
- 关闭 demo 模式后，原来“一个人私聊就能体验”的流程需要显式设置 `ORGPILOT_DEMO_MODE=true`；
- 三方比对依赖事件历史中的 `source_ref` 前缀；手工写入事件时若伪造这个前缀，会被当作“导入写入的值”；
- 账号绑定邮箱与“工作邮箱”的区别是根据实测现象推断的（填工作邮箱后结果不变），没有拿到字段级的直接证据；
- 名单内第二个真实账号的收发尚未在真实租户中验证（单账号实测），该逻辑由自动化端到端测试覆盖。

## 测试计划

- `team.yaml` 格式：重复别名 / 邮箱 / 任务 ID、未知负责人、未知依赖、只写日期的截止时间、没有 `pm`、未知字段；
- 邮箱 / 手机号解析：超过 50 个时分批、邮箱与手机号同批请求、邮箱查不到时回退手机号、两者指向不同用户时报错、部分未解析时整体中止并列出原因（手机号打码）、两个别名指向同一 open_id、大小写和手机号格式归一；
- 接口报错：CLI 输出整齐的错误信息，不抛出异常；
- 测试隔离：开发者的 `.env` 不会进入测试进程；
- 差异计划：新增、更新、状态变化、不支持的变化被跳过、缺失项只报告；
- 三方比对：运行时改过而 yaml 未改时保留运行时的值；两边都改时报告冲突且不写入；非导入创建的任务按 yaml 写入；
- 幂等：同一文件导入两次，第二次零事件；导入 A→B→A→B 往返时每次更新的事件 ID 都不重复；
- 原子性：计划中任一事件校验失败（如依赖成环）时零写入；
- 端到端：导入后成员私聊被正常处理；PM 发起同步会给导入的任务负责人发探针；
- demo 模式：关闭时未注册者零事件；打开且项目为空时初始化；**打开且已有成员但没有任务时不初始化、不产生毒事件**（回归测试）；
- 角色：审批升级只发给 `pm`；导入 `manager` / `lead` 时有警告。

## 实施拆分

1. `fix(feishu)`：自动初始化收进 demo 模式，按“没有成员”触发并走批量校验，附毒事件回归测试（已确认该测试在旧代码上复现 `member ... already exists`）；
2. `fix(coordination)`：审批升级对象统一为 `pm`（已确认新测试在旧代码上失败）；
3. `feat(feishu)`：`batch_get_open_ids` 与 Mock 目录；
4. `feat(team)`：`team.yaml` 模型、解析、差异计划（含三方比对）与导入；
5. `fix(cli)` + `feat(cli)`：独立长连接监听器传入 demo 模式；`orgpilot team import / show`，端到端测试，`team.example.yaml`；
6. `docs`：本 ADR 转为 Accepted，更新飞书配置指南（权限说明、通讯录权限范围、个人邮箱、导入步骤）、开发记录、README、架构文档；
7. 实测修订（2026-10-14）：`test` 屏蔽开发者 `.env`；`feat(team)` 支持手机号；`fix(cli)` 接口报错不再抛出异常。

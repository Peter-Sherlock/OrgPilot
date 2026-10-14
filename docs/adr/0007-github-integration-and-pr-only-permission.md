# ADR-0007：GitHub 集成、计划即代码与仅 PR 写权限（执行大纲）

- 状态：Proposed（草案，未实现）
- 日期：2026-10-11

## 背景

当前 OrgPilot 的事实来源只有两类：飞书消息中抽取的成员声明（health claim），以及 `auto-bootstrap` 或 API 注入的任务事件。系统只能“相信人说的”，无法核对：

1. 成员说“已完成”，但代码可能尚未合并、CI 可能是红的；
2. 成员说“报错卡住”，系统拿不到任何客观证据（CI 日志、失败用例）；
3. 改期审批通过后直接调用飞书 Task API 修改截止时间，变更缺少可审阅的 diff 与可追溯的审计历史。

目标：项目在 GitHub 云端维护，GitHub 成为任务、代码与 CI 的事实来源；飞书保持为对话入口；OrgPilot 作为协调大脑，**只读 GitHub 事件，唯一的写操作是提交 PR，合并永远由人决定**。

## 决策

### 1. 角色分工

| 组件 | 职责 |
|---|---|
| GitHub | 事实来源：计划文件、代码、PR、CI |
| 飞书 | 对话入口：成员汇报、追问、卡片通知 |
| OrgPilot | 协调：事件溯源、声明与证据核验、风险推演、提出变更（PR） |

与现有领域模型的映射：

| 现有概念 | GitHub 对应 |
|---|---|
| `workflow_status`（官方状态，LLM 不可改） | 由任务关联 PR 的状态确定性推导 |
| `TaskHealthClaim`（成员声明） | 不变，仍来自飞书消息抽取 |
| 证据核验 | CI 结论、PR 合并、提交记录 |
| 审批卡片 | PR review：合并即批准，关闭即拒绝 |
| 事件溯源 | GitHub webhook → `OrgEvent`（新增 `EventSource.REPOSITORY`） |

由此 ADR-0003 的 Zero Direct Mutation 从 LLM 层延伸到整个系统：OrgPilot 对任何事实来源都没有直接写权限。

### 2. 身份与“仅 PR”权限模型

- 使用 **GitHub App**，不使用个人 token；
- 最小权限：Metadata 读、Contents 读写、Pull requests 写、Checks 读、Actions 读；**不授予 Workflows 权限**（无法修改 `.github/workflows/`，即无法篡改 CI 本身）；
- 关键约束：创建 PR 需要先推分支，而 `Contents: write` 本身允许直推默认分支，因此“仅 PR”**不能只靠 App 权限表达**，必须配合仓库规则：
  - 默认分支 ruleset：必须走 PR、至少 1 个人类 approve、必须通过状态检查、禁止 force push，App 不在 bypass 名单；
  - `CODEOWNERS` 指定 PM 为 `.orgpilot/plan.yaml` 的必需审阅者，保证“合并 = PM 批准”；
  - GitHub 不允许 PR 作者批准自己的 PR，App 无法自批自合；
  - App 只推送 `orgpilot/*` 命名空间的分支。
- CI 密钥风险：同仓分支的 PR 会携带仓库 secrets 运行 CI。若 OrgPilot 被 prompt 注入生成恶意内容，CI 会执行它。使用 secrets 的 job（部署等）必须放入需要人工审批的 environment。
- 备选（更严格）：fork 模式，App 推送到 fork 再向主仓提 PR，对主仓零写权限；配置成本更高，作为后续加固选项。

### 3. 计划即代码（Plan-as-Code）

- 任务、负责人、截止时间、依赖关系写入仓库内 `.orgpilot/plan.yaml`，由 Pydantic 模型定义 schema；
- 默认分支上 `plan.yaml` 的每次变更（webhook `push`）被解析并 diff 为 `task.created` / `task.updated` 事件，事件 ID 由 `commit SHA + task_id` 派生，保证重放幂等；
- 人和 OrgPilot 都只能通过 PR 修改计划，git 历史即审计日志；
- Web 控制台（ADR-0006）改为渲染 `plan.yaml` 投影出的状态。

### 4. 任务与代码的确定性关联

不让 LLM 猜测 PR 属于哪个任务（与意图识别分层原则一致：系统已知的事实由代码判定，不交给模型）：

- 约定：PR 描述包含 `Task: <task_id>`，或分支命名为 `<task_id>/...`；
- CI 检查：未关联任务的 PR 检查失败（`docs`、`chore` 类可豁免）；
- `workflow_status` 推导规则（待确认）：

| 条件 | workflow_status |
|---|---|
| 无关联 PR | `todo` |
| 存在 open（含 draft）PR | `doing` |
| 存在已请求 review 的 PR | `review` |
| 关联 PR 全部合并且 `plan.yaml` 未声明后续工作 | `done` |
| `plan.yaml` 显式标注 | `blocked` |

### 5. 声明与证据核验

新增 `task.evidence_recorded` 事件承载 CI 结论、PR 合并等客观证据。核验规则保持确定性：

| 成员声明 | 证据 | 处理 |
|---|---|---|
| on_track / 已完成 | 无已合并 PR，或 CI 失败 | 标记“未证实”，在简报中提示 PM |
| at_risk / 报错卡住 | CI 失败 | 将失败 job 与日志链接附到风险卡片 |
| delayed | 关联 PR 已合并且 CI 通过 | 标记冲突，追问成员确认 |

### 6. 改期执行路径变更

- 原路径（ADR-0005）：PM 点击飞书卡片批准 → `UPDATE_TASK_DEADLINE` 调用飞书 Task API；
- 新路径：OrgPilot 生成修改 `plan.yaml` 的 PR → 飞书卡片展示变更摘要与 PR 链接 → PM 在 GitHub 合并或关闭 → `pull_request` webhook（`closed` + `merged`）驱动 `ApprovalManager` 状态 → 飞书卡片原地刷新结果。

## 执行大纲

各阶段独立可交付，按顺序推进。

### G1：只读接入（证据层）

- 创建 GitHub App（此阶段仅授予只读权限）；
- 新增 webhook 路由：校验 `X-Hub-Signature-256`，按 `X-GitHub-Delivery` 去重；
- 双模客户端：`GitHubClient`（httpx 直连 REST API，App JWT 换取 installation token）与 `MockGitHubClient`（录制回放），与 ADR-0005 的 `FeishuClient` 保持一致；
- 映射 `pull_request`、`check_suite`/`workflow_run`、`push` 为 `OrgEvent`；
- 本地开发使用 smee.io 转发 webhook。
- 验收：录制的真实 webhook payload 回放测试全部通过；重复投递不产生重复事件。

### G2：计划即代码

- 定义 `plan.yaml` schema 与加载器；
- 将 `auto-bootstrap` 演示任务迁移为示例 `plan.yaml`；
- `push` 事件触发 diff → 任务事件；控制台渲染新投影。
- 验收：修改 `plan.yaml` 并合并后，控制台与 DAG 在一次 webhook 内更新；回放结果与直接加载一致。

### G3：声明与证据核验

- 实现任务关联约定与 PR 关联检查；
- 实现 `workflow_status` 推导与 `task.evidence_recorded`；
- 实现第 5 节核验规则，接入进度简报与风险卡片。
- 验收：新增场景回放（例如“声称完成但 CI 失败”“声称延期但已合并”）全部通过。

### G4：PR 写入与审批闭环

- 升级 App 权限（Contents 写、Pull requests 写），配置 ruleset 与 `CODEOWNERS`；
- 改期审批改走第 6 节新路径；飞书卡片展示 PR 链接并随 webhook 原地刷新。
- 验收：App 直推默认分支被拒绝；App 无法批准或合并自己的 PR；关闭 PR 等价于拒绝改期。

### G5：云端部署

- 容器化网关；使用 PostgreSQL（ADR-0004 已支持 `ORGPILOT_DATABASE_URL`）；
- GitHub App 私钥、webhook secret、飞书凭据进入密钥管理，不落盘到仓库；
- 公网 HTTPS 入口供 GitHub webhook 使用；飞书可继续使用长连接。
- 验收：真实仓库 smoke test（开 PR → CI → 合并 → 飞书卡片刷新）端到端通过。

## 与意图识别的关系

- 查询类意图（“支付 SDK 现在什么情况？”）可直接用 GitHub 证据回答，而不是生成声明；
- 证据核验为误识别提供第二道防线：被错误抽取的声明会与客观证据冲突并被提示，而不是静默改变任务健康度；
- 意图识别重构（见后续 ADR）应先于 G3 完成，避免把错误声明与证据做比对。

## 待决问题

1. PM 是否接受“在 GitHub 合并 PR 才算批准”？代价是比飞书卡片多一步跳转；
2. `workflow_status` 推导规则是否覆盖团队实际工作方式（一个任务多个 PR、跨仓库任务）；
3. 是否需要直接采用 fork 模式而非 ruleset 方案；
4. 非代码类任务（设计、文档、对外沟通）如何产生证据。

## 后果

正面影响：
- 成员声明可被客观证据交叉验证，抽取幻觉与误识别的影响被限制；
- 所有计划变更均有 diff、审阅与 git 审计历史；
- OrgPilot 对事实来源无直接写权限，安全边界清晰。

代价与限制：
- 引入 GitHub App 凭据管理、webhook 公网入口与云端部署；
- “仅 PR”依赖仓库 ruleset 与 `CODEOWNERS` 正确配置，需纳入上线检查清单；
- 改期审批路径变更，ADR-0005 中 `UPDATE_TASK_DEADLINE → 飞书 Task API` 的映射将被替换；
- 非代码任务缺少天然证据来源，核验能力覆盖不全。

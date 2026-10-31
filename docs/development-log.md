# 开发记录

## 2026-10-30：M1-2 修复 D4——迟到的兑现记为违约（分支 `fix/d4-late-fulfilment`）

生命周期场景里，Bob 承诺 10/27 18:00 前做完 T3，10/28 10:20 才做完，承诺却记为 `fulfilled`。
投影器结算承诺时从不比较 `due_at`，`CommitmentStatus.BROKEN` 在 `src/` 里从来没有被写入过。

### 改动

- `state/projector.py`：`_fulfill_matching_commitments` 改名 `_settle_matching_commitments`（它现在也会写
  `broken`），多收一个事件的 `occurred_at`。匹配事件晚于 `due_at` 写 `broken`；不晚于（含恰好等于）
  或承诺没有 `due_at` 写 `fulfilled`。`workflow_status`、`health_status` 两个入口都传事件时间；
  结算时 `last_update_at` 记为事件时间，以前结算不更新它；
- 用业务时间 `occurred_at`，不用墙钟，也不用 `received_at`：投影仍是事件日志的纯函数，同一份日志
  回放结果相同；消息晚到不算人晚交；
- **没有加新状态。** 「迟到但完成」就是 `broken`：ground truth 期望的正是 `broken`；`broken` 说的是承诺，
  做没做完看任务自己的状态（T3 照常是 done）。语义写进 `docs/event-semantics.md` 新增的「承诺的结局」；
- `KNOWN_DEFECTS` 删掉 D4 条目；D3 注释里「没有地方读 `due_at`」改成「只和结算事件的时间比较，
  不和流逝的时间比较」。

### 结果

- 生命周期（起点 main cf9ef14）：**38/59 → 39/59**。逐项对比改动前后的 59 项检查：只有
  `commitment:c_bob_t3_done` 由失败转为通过，检查集合不变，其余 58 项结果不变；
- 与第九节 9.4 内存补丁实验的结论一致（D4 转绿 1 项，无回归）。

### 新增测试

- `tests/test_projector.py` 新增 7 个（含参数化）：早于或恰好等于 `due_at` → `fulfilled`，晚于 → `broken`，
  两个入口各测一遍；迟到时任务仍记为完成，同一份日志重放出相同的承诺状态；同任务别的取值、别的任务
  达到同一取值、同任务别的谓词，三种无关事件都不改动承诺。测试里的 `due_at` 早于今天，误用墙钟的话
  按时用例也会变成 `broken`；
- 新测试放到未修复的投影器上跑，两个迟到用例都报 `fulfilled` 而不是 `broken`，测试抓得住这个缺陷。

### 未做（如实标注）

- **到期却一直没有匹配事件的承诺仍停在 `active`。** 发现它需要时间推进，属于 M1-8（D3）和
  M1-10（R3 承诺跟进），设计在待写的 ADR-0014；生命周期第 9 轮（T3 过期、没人说话）仍登记在 D3 名下；
- `at_risk` 仍没有写入方，结算只看 `active` 承诺。M1-10 如果开始写 `at_risk`，要让它也能被结算；
- 没有动 `agent/loop.py`、`coordination/ledger.py`（并行的 D5 PR 那条线）。两个 PR 删的是 `KNOWN_DEFECTS`
  里相邻的条目，冲突时保留两边的删除；
- `docs/full-lifecycle-scenario-design.md` 第三节、第九节是带日期的复核快照，没有改。

### 验证状态

```text
lifecycle: 39/59（main cf9ef14: 38/59）
orgpilot replay --all: 9/9 PASS
pytest: 481 passed, coverage 92.47%（fail_under=90；main: 474 passed, 92.41%）
ruff check: All checks passed
eval-intent --replay: dev 97/102, test 53/58, conversations 10/10（与冻结结果一致）
```

---

## 2026-10-30：修复 D5「升级状态一直不消失」（M1-1，分支 `fix/d5-escalation-termination`）

只要项目里有过一个升级的 Case，之后每一轮的终止原因都报 `escalated`。生命周期场景里 David 在第 14 轮失联升级，
第 15–22 轮全部报 `escalated`，「终止原因」在任何长场景里都失去了意义，后面每一步修复的度量都依赖它，所以排第一个修。

### 改动

- `agent/loop.py`：每轮开始时记下所有 Case 的状态，轮末对比，只有**本轮**变成 `ESCALATED` 的 Case 才让这一轮报
  `escalated`。进入升级有三条路：`reconcile()` 里的回复超时、PM 驳回、PR #5 加的批准后写回失败。三条都走这一个比较，
  不需要在每处分别打标记，以后新增的入口也自动覆盖；
- 其他终止原因的语义不变：还有等待中的 Case 时照旧报 `waiting_response` / `waiting_approval`（和原来一样优先于升级）；
  没有任何 Case 报 `no_action`；没有活跃 Case、本轮也没升级，报 `all_resolved`。原来「全部 resolved/cancelled」和
  兜底两个分支都返回 `all_resolved`，合并成一个，行为不变；
- `_determine_termination_reason()` 新参数带默认值，`scenarios/runner.py` 在零轮场景里的无参调用不受影响。

### 取舍：没有改 `coordination/ledger.py`

第九节 9.4 的最小修复实验改的是 ledger（任务完成时把它的 escalated Case 关掉），同样能让这 8 项转绿。没有采用：

- 它覆盖不到 PR #5 那条路：写回失败后任务并没有完成，Case 一直是 escalated，终止原因照样粘住；
- 终态 Case 要不要随任务变化而关闭或重开，是 Case 生命周期的设计问题，归 D1b（M1-6）。现在改 ledger 等于替它先做了决定。

所以升级过的 Case 在台账里仍然是 `escalated`（升级本身是发生过的事实），只是不再影响之后各轮的终止原因。

### 结果

- 生命周期 **38/59 → 46/59**，恰好是 D5 登记的 8 项（第 15、16、18–22 轮的终止原因和 `final_termination_reason`），
  其余 51 项的通过与否逐项不变；`KNOWN_DEFECTS` 删除 D5 条目；
- 第 14 轮仍报 `escalated`。第 17 轮的实际值从 `escalated` 变成 `all_resolved`，仍然失败（期望 `waiting_approval`），
  原因是 D1b 的槽位问题，登记不变。

### 新增测试（`tests/test_agent_loop.py`）

- `test_escalation_is_reported_only_in_the_turn_it_happens`：按三条入口参数化（回复超时、PM 驳回、批准后写回失败）。
  升级那一轮报 `escalated`，下一个安静轮报 `all_resolved`，Case 本身仍是 `escalated`。三条在旧代码上都失败；
- `test_a_new_escalation_is_reported_after_an_earlier_one`：防止矫枉过正。已经有一个旧的升级 Case 时，
  另一个任务新发生的升级仍然要报 `escalated`；
- PR #5 写回失败测试的准备步骤抽成共用函数，断言不变。

### 验证状态

- `uv run orgpilot replay --all`：9/9，m1_03、m1_05 仍在升级那一轮报 `escalated`；
- `uv run pytest --cov=orgpilot`：478 个测试全过（main 上 474 个，新增 4 个），覆盖率 92.43%；
- `uv run ruff check .` 通过；改动的三个代码文件在 main 上本来就格式干净，已跑 `ruff format`。

## 2026-10-29：M1 度量基线——移植六周全生命周期场景与缺陷棘轮（分支 `feat/lifecycle-baseline`）

M1 的目标是让现有能力扛住一个完整项目周期。先把尺子立起来：从 `wip/clock-and-lifecycle`（已关闭的 PR #4）
移植 `e2e_01` 生命周期场景、棘轮测试和评测引擎的逐轮检查。Clock 那一半已由 PR #5 合入，没有重复移植。
本 PR 只做度量，不修内核。

### 结果

- 59 项检查：38 通过、21 失败，和 10-08 在旧基线上的数字、失败集合完全一致。意图路由、名单导入、PR #5
  都不在这条回放路径的被测行为上；
- D1–D6 全部仍复现，没有消失的，也没有新的失败检查。逐条读代码复核，登记表里写上了 ed8bf27 的 file:line；
- **D1 拆成 D1a / D1b**：第 4 轮是「追问拿到 ETA 就结案、不比 deadline」（`ledger.py:163-173`），第 17 轮才是
  「终态 Case 占住槽位」（`:195-197`），是两处独立代码；
- PR #5 让写回失败也会升级，多了一条进入 D5「升级状态粘住」的路，Mock 适配器测不到。

### 怎么验证的

对每个根因只改引用的那几行，在内存里打补丁（不落盘），重跑生命周期、9 个回放场景和整个测试套件：
每个补丁都恰好让自己名下的检查转绿，9 个场景不回归，其余 399 个测试全过；七个叠加是 59/59。
唯一的例外是朴素版 D1b（「任务还有风险就重开」），会让 Case 抖动、打破 m1_01 和 m1_05——这条约束写进了登记注释。
补丁只是实验草图，没有进 PR。

### 其他

- `replay --all` 只扫 `evals/scenarios/`，仍是 9/9；生命周期场景放在 `evals/lifecycle/`，单独由测试跑；
- 引擎现在会检查以前声明了却从没检查过的逐轮字段；脚本里的人工审批如果没命中待审批请求，记为失败，
  不再静默跳过。新增 `test_scenario_round_checks.py` 直接锁住这些语义；
- 团队模拟器设计稿一并入库，文件开头标明「未实现，M3 用」；
- 修复顺序与改动文件见 `docs/full-lifecycle-scenario-design.md` 第九节：按 `ledger.py` / `projector.py` /
  `service.py` 分三条互不重叠的线，第一波 D5、D4、D2 可以并行。

## 2026-10-20：进行中的对话与同步会话重启后恢复（分支 `feat/conversation-persistence`，ADR-0013）

补上同日上一条记录推迟的“对话状态持久化”，按当时的结论和同步会话一起做。

### 改动

- `storage/conversation_store.py`：新增 `SqlConversationStore` 和两张表。`dialogue_snapshots` 每个项目一行，
  存待回答的追问和待确认的同步卡片；`sync_sessions` 每个会话一行，自增 `id` 保留发起顺序。一次 `save`
  在同一个事务里写两者，内容没变时不写；
- `MessageRouter`：第一次处理某个项目时加载对话状态；`handle_message`、`handle_sync_card_action`、
  `start_sync` 结束时保存（路由中途抛错也保存），保存在 `processed_messages` 记录之前；
- `GatewayService`：只加装配——`conversation_store`、`get_sync_coordinator` 构建协调器时加载会话、
  `save_conversation_state`（加锁导出后写入）；`webhook.py` 没有改动。REST `/sync` 启动会话后单独保存；
- `DialogueStateStore` / `ProgressSyncCoordinator`：只加导出与恢复方法，接口仍是同步的，路由行为不变；
- 持久化的时间必须带时区，否则 `ValueError`；加载时不判断过期、不改时间戳，过期和超时仍由原时间戳和
  `service.clock` 决定；
- 读不出来的快照（例如以后删了模型字段）记 warning 后按不存在处理，不会让该项目之后的每条消息都报错。

### 取舍（详见 ADR-0013）

- 选“每轮结束时整体保存”而不是“每次调用即写”：处理器和协调器大量原地修改条目（`reask_count += 1`、
  `probe.status = ...`），评测工具也直接改 `coordinator._sessions`，逐处改成显式更新的改动面大，漏一处
  就是只在重启后出现的缺陷；
- 没有用事件溯源：这些是会过期的对话层临时状态，不是组织事实。

### 新增测试

- `tests/test_conversation_restart.py`（16 个，文件型 SQLite + `FrozenClock`）：追问、确认卡片（点按钮 /
  回复“好的”）、`/同步` 中途重启后收敛出简报，每个流程都与不重启的结果逐项比对；过期的追问、卡片和
  空闲会话在重启后仍按原时间过期；经 `build_gateway_service` + 飞书 webhook 的同一场景；时间往返、
  拒绝不带时区的时间、会话顺序、内存条目优先、读不出来的快照；
- 用两个故意改坏的版本核对区分度：完全不恢复时 10 个测试失败；恢复时把时间戳重置为当前时刻时，
  4 个过期 / 超时测试失败。

### 仍未覆盖（如实标注）

- **至少一次**：副作用先于保存。在“简报已发、快照未写”之间崩溃，重启后会再发一次简报；探针已发出、
  会话未保存时崩溃，需要重新 `/同步`，成员收到两遍探针；
- **仍只在内存**：`LLMIntentClient.spent_usd`（`--max-spend-usd` 按进程计，重启清零）和调用指标、飞书
  tenant token 缓存、适配器 `audit_log`。意图层没有 LLM 响应缓存；
- 单写者假设：多实例同时写同一个库会互相覆盖对话快照；已完成的会话不清理；
- 真实租户里没有做过“演示中途重启”的验证，只有自动化测试。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 412 passed, coverage 92.34%（fail_under=90）
ruff check: All checks passed
意图评测离线回放（c9850dec6655）：dev 97/102、test 53/58、对话 10/10；与 origin/main 的报告逐样本一致（只差延迟）
tests/test_feishu_demo_script.py: 2 passed
git diff --check: PASS
```

---

## 2026-10-20：修复控制台 DAG 不渲染、网页审批必然 403（分支 `fix/console-dag-and-approvals`）

即下一条记录"发现但未修"的前两项，两个问题在 main 上本来就存在。本分支基于 `fix/console-xss-escaping`
（PR #7），保留它的全部转义。

### 修复

- **DAG 面板**：只改控制台，`/dag` 不动。响应本来就给每个节点带了 `layer`，布局不需要新字段。
  `loadDag` 改读 `summary.total_tasks/completed_count/on_track_count/at_risk_count/delayed_count`；
  `renderDagSvg` 改用 `nodes[].task_id/layer/health_status/is_critical_path` 和
  `edges[].from_task/to_task`；状态按小写枚举值比较（`delayed`、`done`）。原代码比较的
  `'DELAYED'`、`'COMPLETED'` 一个大小写不对，一个在 `WorkflowStatus` 里根本不存在；
- 节点配色：已完成紫、延期红、风险或被上游延期波及（`is_at_risk`）或 `blocked` 琥珀、正常绿，新增
  "未上报"灰。新建任务的健康度是 `unknown`，原来默认画成绿色，看起来像"正常"。健康度卡片副标题
  显示未上报的任务数；
- 节点抽屉：状态显示中文标签，阻塞原因读 `blockers` 列表，截止时间后附预计完成时间；
- **审批 403**：新增 `GET /api/v1/projects/{pid}/console-context`，返回控制台的操作人，即项目 PM
  （role 为 `pm` 的成员按 id 排序取第一个）。选取逻辑从 `CoordinationService._find_pm_targets`
  抽成 `pm_member_ids`，两处共用，保证操作人就是协调器给改期请求指定的审批人。控制台拿它作
  `approver_id`，浏览器不再自己决定审批人；项目没有 PM、或请求的审批人不是当前操作人时按钮置灰；
- 服务端校验不变：审批人必须等于请求指定的审批人，否则仍是 403。控制台原来忽略响应、静默刷新，
  现在把失败原因显示在审批面板上。

### 新增测试

- `tests/test_console_api_contract.py`（5 个）：`/dag` 真实响应必须带齐控制台读取的字段，字段清单
  同时反查 `index.html`，防止清单和控制台脱节；控制台比较的状态字符串必须是 `HealthStatus` /
  `WorkflowStatus` 的合法值；`/console-context` 返回 PM，用它提交审批得到 200；没有 PM 时操作人
  为空；
- 其中的 Node 行为测试用真实 API 响应驱动真实控制台脚本：`loadDag`、`loadApprovals`、点击批准，
  断言没有异常、统计卡片数字、SVG 节点和边的数量、发出的 `approver_id`，再把控制台实际发出的
  请求体回放到真实接口，得到 200。对修复前的 `index.html` 运行，5 个里 3 个失败；
- `test_console_escaping.py` 的 DAG 夹具改成真实 `/dag` 的形状。原夹具用的恰好是错误字段名，所以
  XSS 测试没能暴露这个问题。

### 手动验证

mock 适配器、端口 8011、临时 SQLite：重置沙箱，经 API 让 Alice 报延期。真实浏览器里 DAG 画出 3 个
节点、2 条边，支付 SDK 红色、下游两个琥珀色、被波及的边为红色；统计卡片为总数 3、延期 1、未上报 2；
连接状态"已连接"，控制台没有 JS 报错。点击"批准改期"：`POST .../decision` 返回 200，待审批归零。
在页面里以 `mallory` 调用审批，面板显示 403 原因。

### 未做（如实标注）

- 控制台没有登录，操作人就是项目 PM：能打开控制台的人就能以 PM 身份审批。唯一的边界是
  `ORGPILOT_API_TOKEN`，但控制台的 `fetch` 不带 `Authorization` 头，一旦设置 token，控制台全部
  401。这是原有问题，本次未改；
- PR #7 合入 main 之前，本分支的 PR diff 会包含它的提交。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 420 passed（新增 5 个）, coverage 91.96%（fail_under=90）
ruff check: All checks passed
```

---

## 2026-10-20：飞书卡片 `lark_md` 转义（分支 `fix/feishu-lark-md-escaping`）

修控制台存储型 XSS（PR #7）时顺带审计发现，`feishu/cards.py` 把用户可控的文本原样拼进 `lark_md`：
任务标题、探针原因、审批人 / 负责人名字、成员 ID，以及同步协调器生成的、内嵌成员名字的简报建议。
飞书不执行脚本，所以这不是 XSS，而是 Markdown 注入：名字或标题里带 `[批准](http://evil)`、
`**...**`、`<font>`、`<at id=all></at>` 的成员，可以在发给 PM 或群里的卡片上伪造链接和格式，或者触发 @。

### 改动

- 新增 `escape_lark_md`：依据飞书富文本组件文档的"特殊字符转义说明"，把 `& < > * ~ [ ] \` 和反引号
  转成 HTML 实体，换行换成空格（防止用户值另起一行伪造卡片行）。`_ - : /` 故意不转：成员 ID、任务 ID
  和 ISO 时间里都有，而 `lark_md` 文档只把 `*` 列为强调语法；
- 7 个卡片生成函数里所有用户来源的 `lark_md` 值都经过它；卡片自己的 `**`、反引号、换行不变；
  `plain_text` 标题和按钮 `value`（回调要用原值）不转义；
- `build_sync_closed_card` 的 `note` 整体转义：调用方用 operator id 和会话摘要拼出来，现有模板里没有
  Markdown；
- 简报建议在卡片层整体转义，而不是在 `sync_coordinator.py` 里转：`ExecutiveBriefing` 也会存库、
  给控制台用，转义应该发生在具体的渲染端。

### 新增测试

- `tests/test_feishu_cards.py` 新增 30 个（含参数化）：恶意标题和名字在每种卡片里都被转义、按钮
  `value` 保持原值；普通中文标题、全角标点、带 `_` 的 ID、ISO 时间原样不变；正常卡片的字段内容与
  改动前逐字一致。

### 未做（如实标注）

- **没有在真实租户上验证渲染效果。** 转义表出自 `markdown`（富文本）组件的文档，`lark_md`（普通文本
  组件）的文档没有单独写转义规则；反引号里的实体会不会被解码、`&sim;` 显示成什么、`_斜体_` 是否
  生效，都只能在租户里看。核对步骤写在 `docs/feishu-demo-runbook.md` 第 8 节；
- 本次没有向飞书发送任何消息，也没有动演示目录、`.env`、`team.yaml`。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 426 passed, coverage 91.95%（fail_under=90）
ruff check: All checks passed
```

---

## 2026-10-20：修复 Web 控制台存储型 XSS（分支 `fix/console-xss-escaping`）

控制台用 `innerHTML` 模板拼 DOM，任务标题、成员名、消息原文都未转义直接插值。这些数据来自飞书用户
和 `team.yaml`：成员私聊机器人发一句 `<img src=x onerror=...>`，脚本就会在 PM 打开的控制台里执行。

### 修复

- 新增唯一的 `escapeHtml`（转义 `& < > " '`），套在全部 20 处数据插值上：聊天气泡（发送人、消息
  原文）、简报卡片（标题、三个计数、关键路径行、每条建议）、DAG 节点（标题、负责人、工作流状态）、
  时间线（类别、标题、时间、描述）、审批面板（任务、审批人、新截止时间）；
- DAG 标题先截断再转义，避免把 `&amp;` 这类实体截成半个；
- 审批按钮原来是 `onclick="decideApproval('${id}', ...)"`。属性值里的实体会先被浏览器解码再当 JS
  执行，`escapeHtml` 挡不住这个上下文，改为 `data-decision` 加 JS 绑定；
- 页面内部算出的常量（颜色、Tailwind 类、静态 `<circle>`）不转义，列在测试的白名单里。正常数据下
  渲染结果不变。

### 新增测试

- `tests/test_console_escaping.py`：解析 `index.html` 内联脚本，凡是赋给 `innerHTML`/`outerHTML`、
  传给 `insertAdjacentHTML`、或本身含 HTML 标签的模板，每个 `${...}` 都必须是 `escapeHtml(...)`
  或白名单常量；事件处理器和 URL 属性里禁止插值；sink 只能接字面量，不能再拼接。另有 17 个用例
  检验检查器本身能抓出违规写法。对 main 上的旧文件运行，报出 24 处；
- 行为测试：用 Node 跑真实渲染函数（桩 DOM），六个面板灌入恶意数据，断言没有 `img`/`script`
  元素、没有 `on*` 属性，原文作为文本显示。本机没有 Node 时跳过。

### 手动验证

mock 适配器、端口 8010、临时 SQLite：经 API 写入标题含 `<img onerror>` 的任务、经沙箱输入框发送
含 HTML 的消息。聊天、时间线、审批、简报卡片都按原文显示，`onerror` 计数器始终为 0；DAG 节点在
真实浏览器的 SVG 里同样按文本显示。

### 发现但未修（如实标注）

- **DAG 面板在 main 上本来就渲染不出来**：控制台读 `health_distribution`、`completed_tasks`、
  `nodes[].id/health/level`、`edges[].source/target`，`/dag` 实际返回 `summary`、
  `nodes[].task_id/health_status`、`edges[].from_task/to_task`。`loadDag` 每 5 秒抛一次异常，
  DAG 和三个统计卡片一直是空的。与本次修复无关，未改；
- **控制台审批必然 403**：`decideApproval` 写死 `approver_id: 'pm_web_operator'`，不是任何审批人；
- **飞书卡片 `lark_md` 注入**：`feishu/cards.py` 把任务标题、阻塞原因、成员名、建议原文直接拼进
  `lark_md`。飞书客户端不执行脚本，所以不是 XSS，但可以伪造链接、加粗、`<at>` 提醒。转义规则需要
  在真实租户上核对，本次未改。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 415 passed（新增 19 个，含 Node 行为测试）, coverage 91.94%（fail_under=90）
ruff check: All checks passed
git diff --check: PASS
```

---

## 2026-10-20：接入 GitHub Actions CI（分支 `claude/modest-dubinsky-3c1c14`）

此前仓库没有 `.github/`，PR 上看不到任何检查，测试也只在 Windows 上跑过。新增
`.github/workflows/ci.yml`：push 到 `main` 和每个 PR 触发，ubuntu-latest + Python 3.12，
`astral-sh/setup-uv@v10.3.0`，`uv sync --locked`。

### 检查步骤

1. `uv run ruff check .`（不含 `ruff format --check`：main 上有已知格式欠账，如 `intent/router.py`，不在本次范围）；
2. `uv run pytest --cov=orgpilot --cov-report=term-missing`，覆盖率门槛沿用 pyproject 的 `fail_under = 90`；
3. `uv run orgpilot replay --all`；
4. 意图评测离线回放（冻结提示词 `c9850dec6655`）：dev、test、多轮对话三条 `eval-intent --client llm --replay`。

### 实施中的决策

- **评测命令不会因分数变化而失败**：`--client llm --replay` 只在缺录制时返回 1，分数怎么变都返回 0
  （单条样本评测本来就是 "no pass gate"，对话评测的门槛只作用于 oracle 和 heuristic）。工作流里加了
  显式核对：从输出中匹配 `Samples` / `Failures` / `Conversations` 三行，与 `BASELINE.md` 的冻结结果
  （dev 97/102、test 53/58、对话 10/10）逐项比较，不一致即失败；输出格式变了也会失败，不会静默通过。
  只比较冻结的通过数，不另设阈值；以后改提示词或数据集时，`BASELINE.md` 与工作流里的数字要一起改；
- **核对本身验证过会失败**：本地把一条录制的意图从 `request_sync` 改成 `chitchat`，dev 变成 96/102，
  步骤报错退出；挪走一条录制，CLI 报"回放失败"返回 1，经 `pipefail` 让步骤失败；
- **回放不联网（已验证）**：`_exchange` 在 replay 模式下读完录制就返回，不会走到 HTTP；单元测试
  `test_replay_mode_reads_recordings_and_fails_on_miss` 用一个一调用就报错的 transport 覆盖了这一点。
  另外在本地用 socket 守卫（拦截所有非回环地址的 connect 与 DNS 解析）跑完三条命令，记录到的外连
  尝试为 0；对照组里同一个守卫能拦下一次 httpx 请求。CI 不用任何 secret，`DEEPSEEK_API_KEY=dummy`
  只为通过配置校验。

### Linux 上的结果

测试第一次在 Linux 上跑，396 个全部通过，没有出现需要修的平台问题，
所以本分支没有改任何源码或测试。CI 上覆盖率 92.33%，比 Windows 本地的 91.94% 略高；pytest 只剩
2 条 warning，都是 `lark_oapi` 自身的 DeprecationWarning。

### 验证状态

```text
本地（Windows，Python 3.14）：ruff 通过；replay 9/9 PASS；pytest 396 passed，coverage 91.94%；
意图评测离线回放 dev 97/102、test 53/58、对话 10/10，与冻结结果一致
CI（ubuntu-24.04，CPython 3.12.3）：ruff 通过；pytest 396 passed，coverage 92.33%；replay 9/9 PASS；
意图评测离线回放 dev 97/102、test 53/58、对话 10/10，与冻结结果一致；job 耗时 1 分 13 秒
```

### 已知事项

- GitHub 提示 `ubuntu-latest` 自 2026-10-19 起逐步迁到 Ubuntu 26，本次仍跑在 24.04 上；
  工作流用的是 `ubuntu-latest`，迁移后第一次跑如果出问题，再考虑固定版本；
- 只固定了 action 的版本（`actions/checkout@v7.0.1`、`astral-sh/setup-uv@v10.3.0`），uv 本身取最新版。

---

## 2026-10-20：演示前收尾——修复审批改期链路、注入 Clock（分支 `feat/demo-readiness`）

准备在真实飞书租户录演示前，逐段核对"报延期 → 审批卡片 → 批准改期"的代码路径。这条链路在飞书
适配器下从没有端到端测过，核对中发现三个会让演示当场失败的缺陷。

### 修复

- **改期载荷键名不一致**：协调服务的改期提议用 `new_deadline`，飞书侧却读 `proposed_deadline` 和
  `deadline`。后果：审批卡片上的新截止时间显示"未指定"；PM 批准后适配器读不到截止时间，退回用
  当前时间，任务被改期到"批准的那一刻"。现在 `new_deadline` 是唯一的截止时间键，载荷另带
  `task_title` 和 `impacted_tasks`（下游任务标题）；缺少 `new_deadline` 时返回失败。Mock 适配器
  一直读的是 `new_deadline`，所以 9 个回放场景没有暴露这个问题；
- **飞书任务写回**：批准后适配器把 OrgPilot 任务 ID（如 `task-payment`）当成飞书任务 guid 调 Task v2
  接口。名单导入的任务不在飞书任务里，接口必然报错，而异常没有被捕获，整轮协调中断。现在写回
  默认关闭（`ORGPILOT_FEISHU_TASK_WRITEBACK`），改期只更新 OrgPilot；开启后接口失败返回 FAILED、
  不写本地事件，Agent Loop 据此把协调事项升级，而不是标为已解决；
- **独立长连接监听器**：`orgpilot start-feishu-ws` 用默认 Mock 适配器和默认路由构造服务，审批卡片
  和探针不会发到飞书，LLM 意图客户端和业务时区配置也不生效。网关与监听器改为共用
  `build_gateway_service`；监听器在 Mock 适配器下拒绝启动；
- **审批结果卡片**：审批人显示成员名字而不是 open_id，审批时间按业务时区显示（原来标的是 UTC）。

### Clock 注入（移植自 `wip/clock-and-lifecycle`）

- 新增 `domain/clock.py`（`Clock` / `SystemClock` / `FrozenClock`）。`GatewayService` 持有唯一的时钟，
  路由、飞书 webhook、适配器、同步协调器、对话状态存储都从它读"现在"；
- 网关测试的 fixture 改回固定时刻并注入 `FrozenClock`，取代 65c70e0 把 fixture 锚定到真实时钟的做法；
- 改回固定时刻后，立刻暴露出 `DialogueStateStore` 判断追问有效期时仍读系统时钟，一并注入——这正是
  `test_clock_determinism.py` 要防的那一类缺陷；
- 只移植了 Clock。同一份 WIP 里的生命周期场景（`e2e_01`、团队模拟器设计）留给阶段 3 的对比实验；
  PR #4（WIP 备份分支）由本分支取代。

### 新增测试

- `tests/test_feishu_demo_script.py`：按演示手册的步骤，经 `build_gateway_service` 的真实装配
  （只替换飞书客户端）跑完名单导入 → 私聊报延期 → 审批卡片只发给 PM，且含新截止时间和两个下游任务
  → 非审批人点击被拒 → PM 批准 → 截止时间更新、飞书任务接口未被调用 → 重新导入不回滚；
- `test_clock_determinism.py`（5 个）；适配器写回成功 / 失败 / 缺少截止时间（4 个）；Agent Loop
  写回失败时升级（1 个）；共用装配与监听器拒绝 Mock 适配器（2 个）。

### 未做（如实标注）

- **对话状态持久化**原计划今天做，推迟：同步会话（`ProgressSyncCoordinator._sessions`）同样只在
  内存里，只持久化对话状态，重启后待追问的状态会指向已经不存在的会话。两者需要一起做；
- 真实租户未跑：演示步骤见 `docs/feishu-demo-runbook.md`，待执行；
- `ORGPILOT_FEISHU_TASK_WRITEBACK=true` 的路径只在 Mock 上测过。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 396 passed, coverage 91.94%（fail_under=90）
ruff check: All checks passed
意图评测离线回放（c9850dec6655）：dev 97/102、test 53/58、对话 10/10，与冻结结果一致；oracle dev 0 失败
git diff --check: PASS
```

---

## 2026-10-14：P-1 实测修订——手机号标识、测试隔离 `.env`、接口报错提示（分支 `feat/team-onboarding`）

在飞书「测试企业」中用单账号做实测，在导入第一步就发现问题。

### 实测发现

- **查不到时的返回格式**：接口返回 `code: 0`，对应条目只有 `{"email": "..."}`、没有 `user_id`，而不是省略这一条。ADR 初稿里“文档未说明、待确认”的问题至此确认；代码原本对两种情况都按未解析处理，行为正确；
- **只靠邮箱在手机号注册的租户里不可用**：测试账号是手机号注册的，资料里没有账号邮箱。在管理后台把同一个 QQ 邮箱填进“工作邮箱（Work email）”后，按邮箱仍查不到；按手机号能直接查到 open_id。“工作邮箱不算账号邮箱”是根据现象推断的，没有拿到字段级的直接证据；
- **开发者的 `.env` 会泄漏进测试**：`OrgPilotSettings.from_env` 把 `./.env` 写进 `os.environ`，并且在整个进程里一直生效。worktree 里放了实测凭据后，`test_import_requires_feishu_credentials` 先调用了 `monkeypatch.undo()`，把防护也一起撤掉了，结果**真实调用了一次 `batch_get_id`**（只读，查询的是 example.com 示例邮箱，没有写入数据，也没有发消息）。

### 修复与改动

- `test`：`tests/conftest.py` 增加自动生效的 fixture，屏蔽 `.env` 加载，只有 `.env` 解析器自己的测试通过 `real_dotenv` 标记豁免；凭据测试改为显式恢复真实的 `_team_directory`，不再用 `undo()`；
- `feat(team)`：成员可以填 `email`、`mobile` 或两个都填；先按邮箱查，查不到再按手机号查，两者指向不同用户时报错。手机号去掉空格、横线和 `+86` 后保存并检查重复，和邮箱放在同一个 `batch_get_id` 请求里；不写入事件，错误信息里中间四位打码；
- `fix(cli)`：接口报错（如 `Access denied`）时输出接口信息和要检查的权限，不再抛出 Python 异常；httpx 超时异常的消息是空字符串，这时改为显示异常类型（实测中遇到过一次 10 秒超时，重试即成功）；
- 文档：ADR-0009 增加“实测修订”一节；配置指南说明邮箱字段的坑和 `mobile` 的写法；`team.example.yaml` 增加手机号示例。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 380 passed（worktree 中存在真实 .env 时运行，测试未读取它）
coverage: 91.71% branch-aware total coverage (fail_under=90%)
ruff check: All checks passed
git diff --check: PASS
```

### 仍未验证

- 按手机号重跑预览已通过：真实租户里邮箱查不到后回退到手机号，计划为 1 个成员、2 个任务、3 个事件。之后的步骤（正式导入、私聊、`/同步`、审批、三方比对）待执行；
- 名单内第二个真实账号的收发未在真实租户中验证（单账号实测），该逻辑由自动化端到端测试覆盖。

---

## 2026-10-13：P-1 团队接入——team.yaml 名单导入（分支 `feat/team-onboarding`，ADR-0009）

### 已实现

- **team.yaml 与 CLI**（`src/orgpilot/team/`、`cli.py`）：成员写邮箱、名字、角色，任务的负责人和依赖用成员别名引用；`orgpilot team import <file> [--dry-run]` 输出变更计划后整批写入，`orgpilot team show` 核对结果。截止时间必须写到分钟，YAML 重复键、未知字段、重复邮箱 / 任务 ID、未知负责人 / 依赖、依赖成环、没有 pm 均在写入前报错；
- **邮箱 → open_id**（`feishu/client.py`）：`batch_get_open_ids` 调用 `contact/v3/users/batch_get_id`，每批 50 个、排除离职；任一邮箱未解析则整体中止并提示常见原因（权限、通讯录范围、离职、误填飞书企业邮箱）。邮箱不写入事件；
- **差异导入与三方比对**（`team/planner.py`）：新增成员 / 任务生成创建事件，任务标题 / 负责人 / 截止时间 / 状态变化生成 `task.updated` / `task.workflow_changed`；成员改名改角色、改依赖、清除截止时间、删除只报告不执行。任务字段以“上次导入写入的值”为基准做三方比对：`team.yaml` 未改而运行时改过（如已批准的改期）时保留运行时的值，两边都改时报告冲突且不写入。这是实现时对 ADR 初稿“只与当前状态比对”的修正——初稿会让旧名单的重新导入悄悄回滚已批准的改期；
- **事件 ID**：创建类确定（`team-import:<project>:member:<open_id>` / `task:<id>`），更新类带导入批次。验证过内容哈希方案的反例：导入 A→B→A→B 往返时第四次会与第二次撞 ID 被静默丢弃；
- **写入安全**：导入、消息抽取、演示初始化统一走 `commit_extracted_events` 的“投影副本整批校验 → 再写库”；
- **demo 模式与毒事件修复**（`feishu/webhook.py`）：自动初始化默认关闭（`ORGPILOT_DEMO_MODE`），触发条件从“没有任务”改为“没有成员”，并走整批校验。修复前，已有成员但没有任务的项目收到第一条消息会写入重复的 `member.registered`，之后每次重放都失败；回归测试已在旧代码上复现该错误；
- **角色口径**（`coordination/service.py`）：改期审批只发给 `pm`（原为 pm / manager / lead，而发起同步只认 pm）；导入 manager / lead 时给出警告。新测试已在旧代码上确认失败；
- `start-feishu-ws` 独立监听器同样传入 demo 模式；`team.yaml` / `team.*.yaml` 加入 `.gitignore`（含同事邮箱，仓库是公开的），只跟踪 `team.example.yaml`；飞书配置指南新增权限说明、通讯录权限范围与导入步骤。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 370 passed（合并 intent-router 后的 main 为 328；新增 42 个）
coverage: 91.66% branch-aware total coverage (fail_under=90%)
ruff check: All checks passed
git diff --check origin/main...HEAD: PASS
```

### 未覆盖与已知遗留（如实标注）

- **真实飞书未联调**：邮箱解析只在 Mock 目录和 httpx MockTransport 上测试。官方文档没有说明未匹配邮箱在 `user_list` 中是省略还是返回空 `user_id`，实现对两种情况都按未解析处理，需真实租户 smoke test 确认；
- 成员改名改角色、改依赖、删除成员 / 任务仍无对应事件类型，只能报告；角色写错后无法通过重新导入修正；
- 三方比对以 `source_ref` 的 `team-import@` 前缀识别导入写入的值，手工写入事件伪造该前缀会干扰判断；
- `ruff format --check` 在 main 上已有 31 个文件未格式化（本分支删掉 `gateway/service.py` 末尾空行后为 30 个）；本次只保证新增和改动的代码已格式化，未处理存量；
- pytest 的约 1200 条 Python 3.14 asyncio 弃用警告为存量问题，未处理。

---

## 2026-10-13：第 3b 步结项——记录正式结果与已知问题（提示词保持冻结，分支 `feat/intent-router`）

冻结提示词 `c9850dec6655` 的正式评测结果（本步零代码、零提示词改动）：

| 指标 | dev | test | 对话 |
|---|---|---|---|
| 行为通过率 | 95.1%（97/102） | 91.4%（53/58） | 10/10 |
| 错误 claim 率 | 3.08%（2/65） | 0/39 | — |
| claim 精确率 | 93.2% | 89.5% | — |
| 意图 macro-F1 | 95.8% | 71.8%⁽¹⁾ | — |
| p95 延迟 / 费用 | 1962.7ms，$0.00035/条 | — | 1638.0ms，$0.000374/条 |

⁽¹⁾ test 中 P1/P2 意图各仅 1 条样本，该指标不稳定。

- **留出集纪律**：c9850dec6655 是第一个查看 test 结果的版本；此前的提示词改动只在 dev 与
  对话评测上验证，此后的提示词改动也只在 dev 与对话评测上验证，test 数字仅作参考。下一个
  正式留出集来自内测真实消息（经同意、脱敏后标注）。
- **流程偏差**：运行 test 的预设条件"dev 错误 claim ≤ 1"未满足（实际 2 条，差异在噪声范围
  内），test 数字照常记录并注明。
- **ADR-0008 状态改为 Accepted**：P0 意图层实现完成并通过冻结门槛；"待决问题"下新增
  「已知问题」清单 7 项（依赖等待健康度、汇报/承诺边界、非负责人否定陈述、询问式等待口径
  冲突、假设句空条目、能力询问归类、错误 claim 指标口径），本步不修，按优先级排入后续迭代。
- `c9850dec6655` 录制 188 文件入库；BASELINE.md LLM 评测表扩列（test 通过率/错误 claim、
  claim 精确率、macro-F1 dev/test、p95、费用）并补冻结行。

```text
replay --all: 9/9 PASS；pytest 全部通过（coverage ≥90%）；ruff clean；git diff --check PASS
test.yaml 未再修改（哈希 1da96ed7… 不变）；测试全部离线；未 push
```

---

## 2026-10-13：第 3b 步（下半·收尾）——最后一轮修正，之后冻结提示词（分支 `feat/intent-router`）

第二轮在线评测（prompt `ef5a12682a05`）：dev 行为 96.1%（98/102，+14.7pp）、对话 8/10 → 10/10、
意图 macro-F1 89.12% → 93.59%；首轮 19 条失败修复 16 条，剩余 3 条中 2 条由本轮标注修正覆盖、
1 条（dev_sync_near_negative_002）按产品决定记为已知分歧。**本轮后提示词冻结：`c9850dec6655`。**

### 已实现（按部分提交）

- **dev 修正 + recent_messages 接线**：dev_answer_pending_002 health on_track → at_risk（R1：根因找到 ≠ 已解决，问题仍在处理中）；dev_hypothetical_004 增加 `context.recent_messages`（成员前句"支付SDK的报错还在排查"），保留测试目的（含糊语气 + 假设从句 → at_risk + 明天 18:00）且任务在 R7 下唯一可定——layered SUT 新增 `_seed_recent_messages`：把样本提供的近期对话播种进消息表，路由器的"最近对话"上下文（进而 LLM 提示词）即可见到；bot 侧不播种（真实回复经实际发送进入历史）；baseline SUT 不具备该机制、行为不受影响（有测试固化接线）。
- **test 集口径（用户 2026-10-13 批准）**：test_multi_task_002 删 pay_sdk hypothetical 条目、test_multi_task_003 删 merchant_admin negative 条目（R12 顺带提及不输出条目，行为期望不变）；test_request_sync_003 保持 request_sync；冻结 sha256 `b023ded8…` → `1da96ed7…`（BASELINE.md 与 `tests/test_intent_dataset.py` 同步）。
- **提示词措辞（最后一改，同步 LABELING R3/R11）**：已经发生的完成（"已经修好了""已经上线"）不填、将来的完成（"今天能提测""明天修好"）必须填；状态汇报中的等待依赖写入该条目 blocker，不另外输出 dependency_wait 条目（dependency_wait 只用于单纯询问或催促依赖方）。
- **不处理**：dev_sync_near_negative_002 在 BASELINE.md 记为已知分歧（两轮均未按 R8 negative 判定，产品决定不加特例规则）。
- **记录**：`ef5a12682a05` 录制 132 文件入库（`--replay` 可离线复跑第二轮）；BASELINE.md LLM 评测记录表补第二轮行；REVISION-2026-10.md 登记全部修订（样本/旧值/新值/规则/批准）。

### 自检与验收

```text
oracle: dev 0 失败 + 意图 100%（99/99）；test 0 失败 + 意图 100%（56/56）；对话 10/10
orgpilot replay --all: 9/9 PASS
pytest: 全部通过, coverage 90.86%（fail_under=90）
ruff: All checks passed
git diff --check: PASS
测试全部离线；未 push；提示词自此冻结（c9850dec6655）
```

---

## 2026-10-13：第 3b 步（下半·第一轮）——按 dev 错误分析修正：确定性护栏 + 提示词规则 + 标注口径（分支 `feat/intent-router`）

首轮在线评测（prompt `de8ea8d71f69`，录制已入库）：dev 行为 81.4%（83/102）、对话 8/10、
错误 claim 1.54%、意图 macro-F1 89.12%、$0.00037/条；19 条失败逐条归因。

### 已实现（按部分提交）

- **确定性护栏（代码，LABELING S4/S5）**：截止日判定移入代码——expected_completion 晚于任务截止日 → health 取 max(模型判断, delayed)，早于不下调（直接 claim 路径 + 探针合并按目标任务逐个判定）；unknown/空 health 的 report_status/answer_pending 条目不写 claim，探针中不算"已回答"，按既有追问规则驱动到 3a.2 总上限后标记「信息不全」；启发式兜底回答同源修 principle（健康词→on_track、否则 at_risk）。连带更新：mini 陷阱样本补 health、sandbox 测试的"进度正常"消息改为"还有点问题"（on_track 现在即完整回答）、conv_06/conv_10 的 oracle 失败通过移除自加的"无变化跳过事件"去重解决（重复回答照常重写 claim，supersession 语义）。
- **提示词规则修订（de8ea8d71f69 → ef5a12682a05，逐条同步 LABELING.md）**：健康度以"未解决问题"为主判据（正在修/排查/卡住/能修好→至少 at_risk；on_track 仅限无问题推进/完成/解除；"说不好"→at_risk）；删除提示词的截止日比较要求（系统负责）；hedged 限定不确定词表（应该/估计/可能/大概/也许/说不准/理论上/"差不多……吧"），"快完工了/还行/有点慢/预计"=certain；句末"吧"自任务陈述=statement；完成类事件清单（修好/修完/恢复/解决/完成/提测/上线/交付/合并）；任务归属先推断（发送者任务+健康度）后追问；顺带提及（可以开始=依赖解除、我没管）不输出条目；P1 不吞 P0；request_sync 仅限向全员收集、催人不是、明确拒绝→negative；out_of_scope（有请求）vs chitchat（无请求）；待确认卡片"发吧/确认/可以"→acknowledge。提示词例句与三个数据集零重句（程序化核对）。
- **标注口径（只动 dev，登记 REVISION-2026-10.md）**：dev_request_sync_003"帮我看看现在的项目进度"→ query_status/query_answer（R8/S6）；handlers 新增 S6：PM 状态查询回答后附一句"需要我向大家发起同步吗？"（只提示不发卡）；confirmation_flow 核对已是 acknowledge（无需改）。
- **评测口径**：第 0 层样本（显式命令/卡片回调）不计入意图层指标、单列数量——oracle dev 意图集合匹配从 97.06% 修正为 100%（99/99）、test 100%（56/56）；按类别"同步误触发"列改为只统计"不该触发却触发"，与总体一致。
- **test 一致性检查（只读，不改 test.yaml）**：S4 无冲突（无 completion 过截止日且 health≠delayed 的标注）；S5 无行为级冲突（health=None 条目均为 hypothetical 或 negative-no-health，本就不产生 claim）；建议项 2 条——test_multi_task_002 删除 pay_sdk hypothetical 条目（"一恢复我就开跑"属 R12 顺带提及）、test_multi_task_003 删除 merchant_admin negative 条目（"还没碰"非本人任务，R12 不输出条目）——均不影响行为期望，待审批后改并更新冻结哈希；另 test_request_sync_003"我想知道每个人手上的任务都推到哪了"建议保留 request_sync（明确指向全员收集，非模糊"看看进度"）。
- **记录**：de8ea8d71f69 录制 132 个文件入库（--replay 可离线复跑首轮）；BASELINE.md 新增"LLM 评测记录"表（prompt 版本维度，首行填首轮数据）。

### 自检与验收

```text
oracle: dev 0 失败 + 意图 100%（99/99）；test 0 失败 + 意图 100%（56/56）；对话 10/10
orgpilot replay --all: 9/9 PASS
pytest: 327 passed, coverage ≥90%
ruff: All checks passed
git diff --check: PASS
test.yaml 未修改（frozen-hash 不变）；测试全部离线；未 push
```

### 下一轮在线评测命令（新 prompt 版本 ef5a12682a05）

```powershell
$env:ORGPILOT_LLM_PROVIDER = "deepseek"
$env:DEEPSEEK_API_KEY = "<你的 key>"
uv run orgpilot eval-intent --split dev --client llm --live --confirm-spend --max-spend-usd 2
uv run orgpilot eval-intent --conversations --client llm --live --confirm-spend --max-spend-usd 2
```

---

## 2026-10-13：第 3b.1 步——修复预算闸门、运行时录制、计费与重试 + §8 意图层指标（分支 `feat/intent-router`）

### 已实现（按部分提交）

- **commit A（bdc4218）预算闸门 / 录制边界 / 计费 / 提示词**：
  - 预算闸门前置：发请求前累计估算 ≥ `--max-spend-usd` 直接抛 `SpendLimitExceeded` 不再发出请求；调用后超限立即中止。回归：预算 $0.01 + 单次 $0.016 跑 3 段对话 → 旧实现 12 次调用 / $5.04 / "正常结束" 0/3，现在 1 次调用 + CLI 返回非 0 打印「超出预算中止」；
  - 新增 `IntentInfrastructureError` 基类，`ReplayMissError`（兼作 FileNotFoundError）与 `SpendLimitExceeded` 继承；router 对该基类一律上抛（不重试、不降级）；
  - router 只对瞬时错误（超时 / 连接错误 / HTTP 429 与 5xx）重试一次；`LLMIntentError`（客户端已重试过校验）直接降级——连续失败时每条消息最多 3 次 HTTP 调用，全部来自客户端内部；
  - `LLMIntentClient` 新增 `record` 开关（默认关闭）：仅评测 CLI `--live` 显式开启录制；运行时路径（`from_settings` / `default_intent_client`）永不写录制，用户原话不进仓库；冒烟遗留的旧版本录制目录已删除；
  - 计费表更新为官方高峰价（deepseek-flash：input 0.30 / cache_read 0.006 / output 1.20 USD 每百万 token，2026-10 核对）；未知模型按"价格未知"处理，设 `--max-spend-usd` 且价格未知时拒绝运行；
  - `PROMPT_VERSION` 改为哈希（固定前缀 + 工具 schema + 动态模板版本常量 + 降级说明），改任何字段 description 都会换版本；提示词小修：blocker 不推测（提到原因才填、尽量原文）、最近对话轮数用 `ORGPILOT_INTENT_RECENT_TURNS`、承诺示例换句式避免与 dev 集雷同。
- **commit B（本次）§8 意图层指标 + §9 报告显示**：
  - `RecordingIntentClient` 透明包装 oracle / llm 客户端，记录每条样本的 `IntentResult`（预护栏，不改路由逻辑）；新增 `intent/metrics.py`：意图集合完全匹配率、各意图 P/R/F1 + macro-F1（仅出现的意图）、answers_pending 准确率、匹配条目四项属性准确率、期望→实际混淆表（task_id 对齐、位置兜底）；失败样本附模型实际输出；
  - oracle 自检：mini 集（6 条）全部意图指标 100%（测试固化）；单条与 CLI `--intent oracle` / `--client llm` 均已接线，报告新增「意图层指标」章节与混淆表；
  - §9：分母为 0 显示"—"不显示 0%；「baseline 无意图层」说明只在 baseline 报告出现。

### 自检与验收

```text
orgpilot replay --all: 9/9 PASS
pytest: 321 passed, coverage 90.76% (fail_under=90)
ruff: All checks passed
git diff --check: PASS
test.yaml 未修改（frozen-hash 测试通过）；测试全部离线（MockTransport / replay / mock）；未 push
```

### 在线评测运行命令（留给用户；价格按新计费表估算）

```powershell
$env:ORGPILOT_LLM_PROVIDER = "deepseek"
$env:DEEPSEEK_API_KEY = "<你的 key>"
# ① 冒烟（10 条，schema 一次通过率 < 90% 停下汇报）
uv run orgpilot eval-intent --split dev --client llm --live --confirm-spend --limit 10 --max-spend-usd 0.5
# ② 完整 dev 集（报告含意图层指标 + 混淆表 + 失败样本模型输出）
uv run orgpilot eval-intent --split dev --client llm --live --confirm-spend --max-spend-usd 2
# ③ 全部对话评测
uv run orgpilot eval-intent --conversations --client llm --live --confirm-spend --max-spend-usd 2
```

---

## 2026-10-12：第 3b 步（上半）——LLMIntentClient 接入 DeepSeek，跑第一次 dev 评测（分支 `feat/intent-router`）

### 已实现（按部分提交）

- **commit A：LLM 侧 schema 加固**
  - `IntentItem.health_status` 改为 `HealthStatus` StrEnum：JSON schema 出现 enum；原 field_validator 允许的恰是这 4 个值，**校验语义不变**（复跑 layered+oracle dev/test 均 0 失败确认）；handlers 中两处把枚举当裸字符串用的地方改为显式 `.value`；
  - IntentItem/IntentResult 全字段中文 description（expected_completion=带时区偏移 ISO 8601、source_quote=原文逐字子串、R11 只填完成/解除时间等），随 `model_json_schema()` 直接成为给模型的工具说明；`reasoning` 移到首字段并限定两句话；
  - 新增 `llm_client.normalize_intent_payload`：LLM 边界只修两类良性问题并分类计数——丢弃幻觉字段、naive expected_completion 按业务时区补齐；其余错误不修补，交给 Pydantic 报错驱动重试。
- **commit B：LLMIntentClient + 配置**
  - 强制工具调用 `record_intent`（schema 即 IntentResult）、tool_choice=tool、temperature=0、thinking=disabled（按兼容性结论，不采用思考模式）；异步 httpx；
  - 提示词：固定前缀（角色/意图表/正交属性/R1–R11 判定规则/输出要求）进 system，动态内容（时间、发送者、任务含截止日、待回答、最近 N 轮、消息）进 user——固定前缀哈希 = prompt 版本，顺序设计为命中 DeepSeek 自动缓存；示例全部新写，与三个评测数据集零重句；
  - 校验失败 → 错误摘要（loc+type）附在重试消息中再试一次 → 仍失败抛 LLMIntentError（router 走既有 fallback）；无 tool_use → 降级一次（schema 写入提示词解析文本块）；
  - 录制/回放：`evals/intent/recordings/<prompt版本>/<请求哈希>.json`；replay 只读，缺失抛 ReplayMissError，router 对它直接上抛（不静默降级）；结构化日志每次调用一条（token/延迟/缓存/stop_reason/重试/降级/校验错误/归一化/费用），无 key；
  - `ORGPILOT_INTENT_CLIENT=heuristic|llm`（默认 heuristic，mock 演示不变；llm 需 provider=deepseek/aihubmix）。
- **commit C：评测运行器**
  - `--client llm` + `--live --confirm-spend`（需 key）或 `--replay`（只读）；`--limit N`；`--max-spend-usd`（官方高峰价常数表估算，超限立即中止 SpendLimitExceeded）；
  - 报告新增 llm_stats：一次通过率/重试率/降级率、校验错误类型分布、延迟 p50/p95、每消息 token（含 cache read）与费用、缓存命中占比；JSON+markdown 双输出；mini 数据集上验证 live 录制 → replay 离线复现相同指标。
- **commit D：兼容性脚本顺手修复**：token 统计加入 cache_read_input_tokens；两个结构化探针记录校验失败 loc+type（上限 50）。

### 在线运行停止点（本步约定）

- 在线调用只在用户授权后执行（用户自己运行命令，或在 agent 环境设置 key 并给出预算）；
- 先 `--limit 10` 冒烟：schema 一次通过率 < 90% 即停，汇报校验错误分布；通过后跑完整 dev 集与全部对话评测，然后停下——**本步不迭代提示词、不运行 test 集**。
- 本次开发环境无 key，上述在线步骤由用户执行（命令见汇报）；离线验收全部通过。

---

## 2026-10-12：第 3b-0 步——接入 DeepSeek V4.1 Flash，修正兼容性脚本（分支 `feat/intent-router`）

本步**不接入意图层、不发起任何真实调用**：只补齐 provider 配置面与探针脚本，为 3b（真实 LLM 意图客户端）铺路。

### 已实现（按部分提交）

- **commit A（8d39935）deepseek provider + thinking 开关**：
  - `ORGPILOT_LLM_PROVIDER` 扩展为 mock / aihubmix / **deepseek**；新增 `DEEPSEEK_API_KEY`（repr=False）、`DEEPSEEK_BASE_URL`（默认 `https://api.deepseek.com/anthropic`）、`DEEPSEEK_MODEL`（默认 `deepseek-flash`，即 DeepSeek-V4.1-Flash）；deepseek 缺 key 时 validate 报错；
  - 新增 `ORGPILOT_LLM_THINKING`（omit 不发送字段 / disabled / enabled），**provider 决定默认值**：aihubmix → omit（字段不发，原有行为不变），deepseek → disabled（服务端默认开启思考）；from_env 支持 env 显式覆盖；
  - 两个在线 provider 复用 `AnthropicCompatibleLLMClient`：构造参数新增 `thinking`，非 omit 时在请求体注入 `"thinking": {"type": ...}`；响应中只取 text 块、忽略 thinking 块（原有行为保持）；gateway/app 按 provider 构造；
  - .env.example 与 README 增补 deepseek 配置段与 thinking 说明。
- **commit B（fd4a2e0）兼容性脚本扩展**（`scripts/llm_compat_check.py`）：
  - 运行门槛按 provider 校验：provider ∈ {aihubmix, deepseek}、对应 key（`AIHUBMIX_API_KEY` / `DEEPSEEK_API_KEY`）已设置、`--confirm-spend`；base URL / 模型默认值按 provider 取（env 可覆盖，CLI `--model/--base-url` 最高优先）；
  - 新增 `--thinking omit|disabled|enabled`（默认随 provider）：enabled 时全部探针 max_tokens 提到 4096；
  - 每次调用记录 `stop_reason` 与响应 `model`（逐调用清单入 JSON）；汇总新增 `max_tokens_truncations`（stop_reason=max_tokens 次数）与 `model_mismatches`（响应模型 ≠ 请求模型次数——DeepSeek 会把未知模型名静默映射到 deepseek-flash，不报错，所以这个计数很关键）；
  - 新增探针 (e) 强制工具调用：工具 `record_intent`，input_schema = `IntentResult.model_json_schema()`，`tool_choice={"type":"tool","name":"record_intent"}`，调用 N 次，统计 tool_use 出现率与 Pydantic 校验通过率；端点因 `$defs/$ref` 拒收（HTTP 400 且响应含 $defs/$ref）时记录错误、改用展开后的 schema（`expand_schema_refs` 全量解析引用）重试整轮，汇总注明 `schema_expanded`；
  - 单个探针 HTTP 错误：记录状态码 + 响应体前 300 字符（API key 强制替换为 `***`），其余探针继续执行，不整体崩溃；
  - 结果写入 `reports/compat/<provider>-<model>-<thinking>-<时间戳>.json`（已加入 .gitignore），同时打印；
  - 测试全部用 httpx MockTransport（12 个用例）：provider 门槛、thinking 注入/省略与 max_tokens、强制工具探针解析、$defs 拒收回退（断言重试请求的 schema 不再含 $defs/$ref）、HTTP 错误隔离与 key 脱敏、模型不一致与截断计数、报告落盘、思考参数透传。

### 文档

- ADR-0008 §7 改写：主候选 DeepSeek V4.1 Flash（官方 API、Anthropic 兼容端点），AIHubMix gpt-5.6-luna 为可选对照；结构化输出策略取决于兼容性测试（无原生 schema 输出，优先强制工具调用，$defs/$ref 被拒则展开）；选型依据 = 兼容性测试 + dev 集结果，test 集只在选定模型与提示词后运行一次；数据流向：官方 API 只经过 DeepSeek 一方，进入企业使用前仍需合规评估。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 全部通过（含 compat 脚本 12 个 MockTransport 用例），覆盖率 ≥ 90%
ruff / git diff --check: PASS
真实调用：0 次（全测试走 MockTransport）
```

---

## 2026-10-12：第 3a.2 步——追问次数封顶，mock 模式下探针能收敛（分支 `feat/intent-router`）

### 已实现（按部分提交）

- **commit A（0a592f0）追问次数封顶**：
  - `PendingClarification.reask_count`：因换话题而消耗的重问预算，与 `asked_rounds` 分开计数；`DialogueStateStore.max_topic_reasks`（默认 1，`ORGPILOT_MAX_TOPIC_REASKS`，负数校验）；
  - 换话题轮：`reask_count += 1`；未超重问预算 → 回复末尾附带重问；`asked_rounds + reask_count` 达到 `max_clarification_rounds + max_topic_reasks`（默认 3+1=4）→ 停止追问——probe 来源经 `notify_probe_finished(complete=False, note="信息不全")` 通知协调器并清除（与"多轮追问后仍不完整"同一处理），clarify 来源直接清除；
  - answer 批处理的放弃条件从 `asked ≥ 3` 改为 `asked + reasked ≥ 4`：换话题轮与不完整回答轮共用同一总预算，任何混合序列（闲聊与部分回答交替）都会收敛；探针首次提问不计入（`asked_rounds` 从 0 起）；
  - LABELING.md R9 补充该规则（重问预算、总上限、两种来源的收敛方式）。
- **commit B（62bb23b）HeuristicIntentClient 最小兜底**（只为 mock 演示收敛，不追求准确率）：有待回答问题时，非命令、非明显闲聊（关键词表）、非单纯确认语（精确匹配表）的消息按 `answer_pending` 处理；任务默认取待答任务，多目标任务按消息提及优先——任务 id/标题完整包含直接命中，否则按字符 bigram 重叠 ≥2 且严格领先判定"提到的那个"；无明确提及 → 任务置空，由合并层作用于全部目标任务（安全失败：健康度兜底 at_risk、只追问时间，不虚构完成信息）；时间仍由 TemporalResolver 解析，未扩展解析能力。
- **commit C（91958de）评测扩充与门槛**：
  - `conversations.yaml` 8 段 22 轮 → **10 段 36 轮**：conv_09 连续换话题（第 2 次换话题不再重问；第 4 次达到总上限 → 标记「信息不全」收敛出简报）、conv_10 成员始终不正面回答（第 5 轮含糊回复达到总上限后收敛）；
  - conv_06 适配新总上限：放弃点从第 4 轮顺延到第 5 轮（t6 的 asks_clarification 标注翻转为 true，新增 t7 放弃轮）——总上限 3→4 的直接推论，属标注随行为契约修订；
  - CLI `--conversations` heuristic 门槛：**conv_01 必须收敛且完成时间正确**（`HEURISTIC_GATE_CONVERSATIONS`），其余对话只记录不设门槛；oracle 门槛不变（全部对话通过）；
  - BASELINE.md 按 3a.2 口径刷新：多轮对话 oracle **10/10 段（36/36 轮）**、heuristic 2/10（conv_01 + conv_08）；单条样本三组数字同步更新（heuristic 多轮补全 dev 37.5%→62.5%、test 0%→50%，错误 claim 4/64→6/65 的刻意取舍已注明）。
- **测试对齐**：`test_probe_reply_with_sync_keyword_is_not_treated_as_new_sync` 的成员消息从"我这边当前进度正常"改为纯闲聊——3a.2 语义下前者是含糊回答尝试（正确地消耗一轮追问），原断言"探针期间消息绝不二次发起同步"用闲聊消息保持不变。

### 实施中的决策

- **换话题轮无论是否实际附带重问文本都消耗预算**：若只按"实际发出了重问"计数，只闲聊不回答的成员永远不会触发总上限，与本次目标（mock 模式下探针必能收敛）相悖；`reask_count` 的语义因此是"因换话题消耗的重问预算"，LABELING.md R9 按此描述。
- **任务提及检测用 bigram 重叠而非完整标题包含**："支付SDK明天下午5点能修好"提到的是任务"支付SDK接入"的标题前缀，完整包含判定会漏配、把答案错误作用于全部目标任务（conv_02 t1 首跑即暴露，随后修复）。
- **fallback 条目不猜健康度**：健康度留空由合并层兜底（有原 draft 沿用、否则 at_risk），缺失槽位继续追问——宁可多问一句，不虚构"正常/已完成"。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 273 passed
coverage: 91% branch-aware (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
eval-intent 单条样本: oracle dev 102/102、test 58/58（100%），未经确认群发 0、误触发 0、错误 claim 0
eval-intent --conversations: oracle 10/10 段（36/36 轮）通过；heuristic 2/10（conv_01 门槛通过，exit 0）
```

### mock 模式完整演示（/同步 → 成员回复 → PM 收到简报）

```text
[09:00] 赵敏: /同步
  机器人 -> 已发起全员进度同步：已向 3 位负责人发送进度探针。(sync_started)
[09:12] 王伟: 我负责的两个都搞定了
  机器人 -> 已记录：支付SDK接入 → 有风险；已记录：商户后台管理端 → 有风险。
            还需要补充：【支付SDK接入】还差：预计恢复或完成时间点；【商户后台管理端】还差：预计恢复或完成时间点
  (clarification_sent —— fallback 把含糊回答按 at_risk 兜底并追问时间)
[09:22] 王伟: 最晚明天下午5点，都搞定
  机器人 -> 已记录：支付SDK接入 → 有风险，预计 10/17 17:00 完成；已记录：商户后台管理端 → 有风险，预计 10/17 17:00 完成，信息完整，谢谢！
[09:30] 刘芳: 都搞定了，最晚明天下午6点
  机器人 -> 已记录：收银台页面改版 → 有风险，预计 10/17 18:00 完成；已记录：对账模块联调 → 有风险，预计 10/17 18:00 完成，信息完整，谢谢！
[09:45] 陈磊: 压测已经全部搞定了
  机器人 -> 已记录：支付全链路压测 → 正常，信息完整，谢谢！(关键词恢复路径)
会话状态：发起于 10/16 09:00，已回复 3/3 人
简报已生成：True
简报摘要：项目全景进度汇总：共 5 项活跃任务，0 项正常推进，0 项延误，4 项存在潜在风险。
  - 王伟/刘芳/陈磊：collected
  · 建议：有 4 项任务处于不确定风险状态，建议关注后续技术排查进展。
```

### 已知限制

- 对话状态仍仅在内存（重启丢失）；probe 来源 claim 的合并加权规则仍属第 4 步；
- heuristic 错误 claim 率 6/65 是"准确率换演示收敛"的刻意取舍（BASELINE.md 已注明）；"正常""跑通"等恢复表达仍在 MockLLMClient 关键词之外，heuristic 多轮对话 2/10 的其余失败均属 3b 的目标空间。

---

## 2026-10-12：第 3a.1 步——探针回复统一接入新流水线，多轮对话评测（分支 `feat/intent-router`）

### 已实现（按部分提交）

- **commit A（8740bc4）路由顺序修正**：显式命令前移到探针捕获之前且不消耗轮数；会话进行中 PM 重复 `/同步`、确认卡片或 acknowledge 确认 → `sync_already_active`，回复会话状态（发起时间、已回复/总人数，`SyncSession.progress_summary()` 新增）；
- **commit B（8fc1df7）探针回复统一接入新流水线**：
  - `PendingClarification` 目标任务扩展为列表（`task_ids`），待补全 claim 与缺失槽位按任务组织（`claims`/`missing` 字典）；
  - 发出探针时为每个被询问成员登记 probe 来源的追问（`router.start_sync` + `register_probe_clarifications`，公开 API；确认卡片确认与 acknowledge 确认同路径）；
  - 删除旧路径：`ProgressSyncCoordinator.handle_member_reply`（内含 ClaimExtractor 抽取与 SlotCompletenessEvaluator 判断）与 `GatewayService.handle_sync_member_reply` 整体移除，协调器不再持有 extractor；
  - 新增协调器公开 API `notify_probe_finished(project_id, member_id, complete=…)`：收集完成/信息不全经它通知，协调器负责收敛与简报；超时路径不变（`get_active_session` 触发，每条入站消息都会"对表"并刷新协调器 agent，保证超时简报反映最新已持久化 claim）；
  - 收集完成判定按任务：on_track 免槽位、at_risk 需时间、delayed 需时间+阻塞（与 SlotCompletenessEvaluator 同源的 `_task_missing_slots`，不留两套逻辑）；无任务整句回答（"都正常"）作用于全部目标任务；追问只列缺信息任务与槽位；最大轮数（3）仍不完整 → `MemberProbeState.incomplete` 标记，简报 recommendations 首行标注"信息不全"；
  - **replay 场景说明**：9 个 replay 场景直接驱动 `CoordinationAgent.run_turn` 内核、不经过 ProgressSyncCoordinator，行为零变化，无需更新期望（逐条核对过 runner 源码）；
- **commit C（本次）评测补充**：
  - 样本上下文新增 `pending_probe`（task_ids + asked_rounds）；`LayeredSystemUnderTest` 遇到它时通过公开 API `router.start_sync` 建立**真实同步会话**并按样本登记探针追问；新增 dev 样本 `dev_probe_reply_001/002`（probe_reply 类别，下限 2），dev 102 条；
  - `evals/intent/conversations.yaml`：8 段 22 轮，覆盖任务书 8 个场景（时间澄清补全、双任务部分回答、换话题重追问、探针期间 /帮助 不耗轮数、PM 被探针时重复 /同步、最大轮数信息不全、NL 同步→卡片→确认→全员回复→简报、重复投递幂等），每轮断言行为、每段断言最终状态（claims/collected/简报标注）；
  - `intent/conversation_eval.py` + CLI `eval-intent --conversations`；oracle 以 (sender, message) 为键、按对话注入（`allow_duplicate_keys` 供重复投递轮）；**oracle 门槛：全部对话通过——实测 8/8 段（22/22 轮）通过**；
  - BASELINE.md 记录 baseline 数字对应提交 `4ee48bf`（可检出复跑）及多轮对话结果。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 265 passed（新增 coordinator notify 3、会话评测 5、router 探针/命令/护栏 6、CLI --conversations 2 等）
coverage: 90.46% branch-aware (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
eval-intent: 单条样本 dev/test oracle 0 失败（100%），--conversations oracle 8/8 通过
```

### 探针路径前后对比（场景 1：探针 → 风险无时间 → 追问 → 补时间 → 收集完成）

| 轮次 | 旧路径（3a 及之前） | 新路径（3a.1） |
|---|---|---|
| 发起 | `/同步` → 协调器建会话并群发探针 | 同左，但 router 另为每个成员登记 probe 追问（目标=其全部任务） |
| 回答1 "压测这边卡住了" | `handle_sync_member_reply` → ClaimExtractor 抽取 → SlotCompletenessEvaluator 判缺时间 → 协调器自己追问（turns_count+1） | 意图分类（answer_pending）→ 护栏 → answer 批量合并 → 按任务判完整性（at_risk 缺时间）→ 追问只提该任务该槽位（rounds+1） |
| 回答2 "预计明天下午5点能恢复" | 再抽取 → 完整 → 协调器标记 COLLECTED，全员收敛才出简报 | 合并补上时间 → 按任务完整 → `notify_probe_finished(complete=True)` → 协调器标记 COLLECTED，全员终态才出简报 |
| 差异要点 | 抽取/完整性/追问文案都在协调器内，意图层无法介入；成员多任务时追问文案只提第一个任务 | 抽取与完整性在意图层（可被 oracle/模型替换），按任务逐个追问；协调器只管收敛与简报 |

### 已知限制

- 对话状态仍仅在内存（重启丢失）；probe 来源 claim 的"合并规则"（同人多任务加权）仍属第 4 步；
- `--system baseline` 与 layered+heuristic 同链路（同 3a 说明）；heuristic 在多轮对话上大量失败属预期（探针回答意图识别是 3b 目标）。

---

## 2026-10-12：意图识别第 3a 步——三层路由、对话状态、消息幂等与 oracle 评测（分支 `feat/intent-router`）

### 已实现

- **意图客户端接口**（`intent/client.py`）：`IntentClient` 协议 + `IntentContext`（消息、业务时区 occurred_at、发送者 id/角色/显示名、负责的任务、全部任务快照、待回答/待确认上下文、最近 N 轮对话含机器人发言，N 默认 6 可配 `ORGPILOT_INTENT_RECENT_TURNS`）；`HeuristicIntentClient`（包装 MockLLMClient，mock provider 默认，效果不作考核）；`ScriptedIntentClient`（预设结果 + 可模拟超时/异常/非法输出）；所有调用经 `classify_with_timeout`（`asyncio.wait_for`，默认 20 秒 = `ORGPILOT_INTENT_TIMEOUT_SECONDS`），同步实现 `asyncio.to_thread` 包装；
- **MessageRouter**（`intent/router.py`）：webhook（含 WS）与 sandbox-chat 全部改走 router，两处的重复路由逻辑（同步关键词列表、"不可处理回欢迎语"）已删除；`/messages` 保留为 legacy 低层抽取接口（docstring 标注）。消息处理顺序：**幂等 → 群聊拒收 → 未注册拒收 → 探针回复 → 显式命令 → 对话状态+意图分类 → 护栏 → 处理器**；意图分类失败重试一次，仍失败回复"没理解，可以换个说法吗？也可以发送 /帮助 查看用法"，回复类别 `fallback`，不改状态；
- **消息级幂等**（`storage/message_store.py` 新增 `processed_messages` 持久化表）：project_id + message_id 已存在时直接返回上次结果，不重新识别、不重复发送；同一张表提供"最近 N 轮对话"上下文；
- **DialogueStateStore**（`intent/dialogue.py`）：按（项目+发送者）存放 PendingClarification（问题/目标任务/待补全 claim/缺失槽位/轮数/来源 probe|clarify）与 PendingSyncConfirmation（发起人/成员/时间）；过期 24 小时/30 分钟可配置；探针追问登记于此，探针回复仍委托协调器收敛；**本轮仅内存，重启丢失**（已知限制）；
- **护栏**（`intent/guards.py`，纯函数 + 单测）：grounding（复用 verify_quote）→ 任务（R7 追问列出发送者任务）→ 语气（question+report → 查询；hypothetical → noted）→ 含糊无时间追问（R6）→ 置信度阈值（默认 0.6，`ORGPILOT_INTENT_CONFIDENCE_THRESHOLD`）→ 时间原文子串 + **只比对日期** → request_sync 仅 PM → P1/P2 → unsupported；
- **处理器**（`intent/handlers.py`）：report_status/make_commitment 走可复用事件构造（`extraction/event_builder.py`）+ `commit_extracted_events`（内存校验→持久化→跑一轮），回复"已记录：支付SDK接入 → 有风险，预计 10/14 17:00 完成"式摘要；answer_pending 把答案合并进待补全 claim（字段覆盖、健康度缺省链、无原 claim 则新建），SlotCompletenessEvaluator 判完整性，不完整且未达轮数只追问剩余槽位；换话题（answers_pending=false）正常处理并末尾重新追问、轮数不增（R9）；query_status 直接从投影回答；request_sync PM 发 `kind=sync_confirmation` 确认卡片（按钮 sync_confirm/sync_cancel，仅发起人且 PM 可操作，过期原地更新"已过期"），否定句 → noted；acknowledge 有待确认卡片时肯定 → 发起（sync_started）/否定 → 取消（noted），无卡片 → smalltalk；一条消息多条目分别处理合并回复，类别按优先级 `no_permission > sync_started > sync_confirm > clarify > claim_recorded > query_answer > noted > unsupported > help > smalltalk`（已写入 ADR）；
- **oracle 评测**（`intent/oracle.py` 仅评测路径 + `evaluator.LayeredSystemUnderTest` + CLI `--system baseline|layered --intent heuristic|oracle`，oracle 仅限 layered，报告标题标注上限口径）；oracle 达标：**dev 100/100、test 58/58 全部通过（100% ≥ 95%），未经确认群发 0、同步误触发 0、错误 claim 0**，0 条失败即无需修流水线 bug 也无需上报标注问题。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 251 passed（新增 router 集成 19、guards 15、client 6、dialogue/message_store 6、oracle 流水线 1 等）
coverage: 90.07% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
eval-intent 四种组合可运行；oracle+baseline 组合按设计拒绝（SystemExit 提示只能与 layered 组合）
```

### 已知限制（如实标注）

- **DialogueStateStore 仅在内存**：进程重启后待回答追问与待确认卡片全部丢失（探针自身的会话状态仍由协调器管理）；持久化留给后续步骤；
- probe 来源的 claim 合并仍由协调器内旧抽取路径完成（router 登记其追问状态但处理委托给协调器），统一的"按探针合并"留到第 4 步（claim 合并规则）；
- `--system baseline` 在运行时与 layered+heuristic 同链路（旧 webhook 路由已删除），仅观测口径不同；BASELINE.md 中 baseline 行为第 3a 步之前的历史数字；
- oracle 每个样本需要唯一 (sender, message) 键，同一 split 内消息重复会构造失败（当前数据集满足）。

---

## 2026-10-12：评测集小修订（R11）并冻结 test 集（分支 `feat/intent-eval-baseline`）

### 已实现

- **规则 R11**（`LABELING.md`）：`expected_completion` 只填"任务完成或阻塞解除"的时间；开始时间、演示时间、定位原因的时间、下次同步的时间不填，任务已完成的样本也不填；
- **样本修订**（登记于 `REVISION-2026-10.md` 第五节）：
  - 消息改写使时间成为完成时间、保留解析难点：dev_time_chinese_001（"三点能定位"→"三点能修好"）、dev_time_chinese_002（"5点半演示"→"5点半能全部搞定"）、dev_time_chinese_003（"下周三才能开始"→"下周三才能跑完"）、test_answer_pending_001（"明天上午十点开始全链路跑"→"明天上午十点能恢复"，健康度 on_track→at_risk）、test_time_chinese_001（改为明确的"拖到月底才能做完"，保留"月底"）、test_time_chinese_002（"晚上八点开跑"→"晚上八点能全部跑完"）；
  - **dev_time_chinese_004 补上 delayed**（月底 10-31 晚于截止日 10-22，R1）。更正：上一轮修订清单误记此样本已改为 delayed，实际未落地，本轮修正；
- **回复类别拆分**：`sync_confirm`（只发确认卡片）与 `sync_started`（已发起同步：命令直达或卡片确认后）分离；7 条已发起样本（sync_command PM×3、confirmation_flow 确认×4）改为 sync_started，request_sync 8 条维持 sync_confirm；评测器观测逻辑同步（observed：发起了→sync_started，仅发卡片→sync_confirm），LABELING.md 的 R5/R8 表已更新；
- **CLI**：`eval-intent` 汇总输出新增"Unconfirmed mass sync sends: x / y samples requiring a card (target 0)"；
- **test 集冻结**：BASELINE.md 记录 `test.yaml` 的 sha256（82f55b9e…）并注明"之后修改 test 集需要先经过审批"；新增 `test_frozen_test_split_unchanged_without_approval`，哈希不一致即失败并在失败信息中提示需审批、同步更新 BASELINE.md。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 204 passed（新增冻结哈希测试 1 条；评测器/评测集用例按 sync_started 口径更新）
coverage: 91.15% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
eval-intent dev/test --mode mock: 正常运行，CLI 输出含未经确认群发次数
```

### 基线 (b) 更新（口径：R1–R11 + sync_started 拆分 + 时间修复）

- dev：claim P 41.18% / R 17.50%；时间 75.00%；错误 claim 率 6.25%；同步误触发 0%；未经确认群发 1/5；回复类别 29.00% → **28.00%**（唯一变化：dev_request_sync_003 的回复在新口径下类别不符）；失败 85；
- test：claim P 20.00% → **33.33%** / R 5.00% → **10.00%**；task 40.00% → **50.00%**；健康度 50.00% → **66.67%**；时间 0.00%；未经确认群发 0/3；回复类别 17.24% → **18.97%**；失败 51。test 的提升来自 R11 改写让消息含义更明确（mock 抽取多命中 1 条正确 claim），并非运行时变化；
- 完整数字见 `evals/intent/BASELINE.md`（含冻结哈希）。

---

## 2026-10-11：评测集修订（R1–R10 标注口径）与两个时间 bug（分支 `feat/intent-eval-baseline`）

### 已实现

- **标注规范 `evals/intent/LABELING.md`**（产品确定的唯一口径，R1–R10）：R1 健康度按"预计完成时间 vs 任务截止日"三档判定；R2 否定句按实际含义（负责人否定风险 → on_track claim；字面否定、问题仍在 → at_risk）；R3 带疑问的等待归 query_status；R4 非负责人汇报照常生成 claim；R5 回复类别新增 `noted`（与项目相关但不改变状态：假设句、拒同步、拒绝确认）并把"无待确认事项的单纯确认语"归 smalltalk；R6 含糊语气无时间→追问、有时间→生成 claim；R7 任务歧义（未提任务名且负责人多任务）→追问不猜测；R8 全员同步矩阵（PM 自然语言=只发确认卡片；`/同步`=直接发起；卡片确认/拒绝；非 PM=no_permission）；R9 换话题按新话题回复并重新追问；R10 时间约定补充"只有日期没有时刻 = 当天 18:00"。dev/test 文件头指向该文件；
- **评测集修订**：145 条旧样本逐条复查，33 条修订（R1×4、R2×7、R3×1、R5×11、R6×1、R7×1、R8×8，其中 dev_p1_p2_003 移入 query_status 类别）；新增 13 条：sync_command 5（PM `/同步`×3、非 PM `/同步`×1、`/帮助`×1）、confirmation_flow 7（PM 确认×4、PM 拒绝×2、非 PM 误回复×1，context 新增 `pending_confirmation{kind=sync, initiated_by}`）、dev_p1_p2_007（补足 p1_p2 覆盖）。修订后 dev 100 / test 58 共 158 条（63:37）；逐条修订登记于 `evals/intent/REVISION-2026-10.md`；
- **评测器修订**（`src/orgpilot/intent/evaluator.py`，非运行时业务代码）：`ReplyCategory` 新增 `NOTED`；样本上下文支持 `pending_confirmation`（baseline 无确认卡片机制，构造不了状态，预期失败）；确认卡片判定改为显式约定——只有带 `kind=sync_confirmation` 标记、副作用发生前的消息才算卡片，同步之后发出的通知不再算；同步指标拆分为"同步误触发率"（不该发起且不该发卡片的样本上出现任何同步副作用）与"未经确认群发次数"（应先发卡片的样本上直接发起，计数、目标 0）；Markdown 报告对样本数 <5 的类别标注"样本不足，仅供参考"；`evals/intent/reports/` 移出版本控制（.gitignore + git rm --cached），基线数字改记 `evals/intent/BASELINE.md`（不带生成时间）；
- **B5 业务时区**（运行时修复）：Feishu create_time 是 UTC 毫秒，此前相对时间按 UTC 锚定，北京用户说"明天下午5点"会被记成 17:00 UTC。新增 `ORGPILOT_TIMEZONE`（默认 Asia/Shanghai，`validate()` 校验 IANA 时区名）；`GatewayService.to_business_timezone()` 在消息进入抽取/同步路径前统一换算（webhook、WS（经 webhook 委托）、sandbox-chat、/messages 四入口一致；事件存储仍为带时区同一时刻）；依赖加入 `tzdata`（Windows zoneinfo 需要）并更新 uv.lock；
- **B6 "下周X" 自然周**（运行时修复）：此前周二说"下周三"解析为本周三；现改为"下一个自然周（周一为一周起点）的周X"，"周X""这周X""本周X"保持原行为。未顺带扩展中文数字、"半"、"下班前"等解析能力。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS
pytest: 203 passed（新增 tests/test_business_timezone.py 5 条、resolver 下周X 2 条、评测器/评测集用例重写）
coverage: 91.19% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
orgpilot eval-intent --split dev/test --mode mock: 均运行并生成报告（报告已不入库）
```

### 基线重跑（顺序：先评测器修订、后时间修复）

- 基线 (a)＝时间修复之前代码：dev 时间准确率 0.00%、失败 87；test 时间准确率 0.00%、失败 51；
- 基线 (b)＝时间修复之后代码：dev 时间准确率 75.00%、失败 85（dev_answer_pending_003 与 dev_time_chinese_003 的"下周三"经 B6 转为通过）；test 数字不变；
- 其余指标两组完全一致（claim P/R、错误 claim 率、同步误触发 0%、未经确认群发 dev 1 次、回复类别、多轮），完整数字见 `evals/intent/BASELINE.md`。

### 与任务说明的差异（如实标注）

- 上一轮提交的评测报告文件按本轮要求从索引移除并忽略，上轮开发日志中"报告随分支提交留档"的做法自此作废；
- dev_p1_p2_003 修订后类别从 p1_p2 移至 query_status，为保持 p1_p2 ≥10 的类别下限新增 dev_p1_p2_007（P2 计划变更）；
- test 集时间准确率仍为 0%：可触发 claim 的 test 样本时间表达（月底前、晚上八点、中午、明早）均在现有 TemporalResolver 能力之外，或消息不触发 claim，属真实基线而非回归。

---

## 2026-10-11：意图识别第 1 步——schema、对抗评测集与现有系统基线（分支 `feat/intent-eval-baseline`）

### 已实现（按 ADR-0008 实施顺序第 1 步，不改任何运行时行为）

- **ADR-0008 落库**：从设计工作区复制 `docs/adr/0008-layered-intent-recognition.md`（意图分类、正交属性、三层路由、评测先行原则），作为意图层工作的依据；
- **意图输出 schema**（`src/orgpilot/intent/models.py`，仅定义未接入运行时）：`IntentType` 覆盖 ADR-0008 第 2 节全部 15 个意图（P0 九个 / P1 五个 / P2 一个）；`IntentItem` 含正交属性 `speech_act`/`polarity`/`certainty` 与槽位（task_id、health_status、原文 time_expression、带时区 expected_completion、blocker、必填 source_quote、confidence）；`IntentResult` 支持一条消息多个条目、answers_pending 与 reasoning；
- **对抗评测集**（`evals/intent/`）：固定项目夹具（1 PM + 3 工程师，5 个任务，含"支付SDK接入/支付全链路压测""收银台页面改版/商户后台管理端"两组易混淆名称、负责人与依赖）；dev 92 条 / test 53 条共 **145 条**，按类别分层（见下），每条含 id、category、message、sender、occurred_at(+08:00)、可选 pending_question/最近对话上下文，expected 分 behavior（claims/commitments/starts_sync/确认卡片/追问/回复类别）与 intents（意图 + 正交属性）两部分。样本按真实团队中文口语编写，未参考 `MockLLMClient` 关键词；
- **评测运行器与基线**（`src/orgpilot/intent/evaluator.py` + CLI `orgpilot eval-intent --split dev|test --mode mock|replay|live`）：被测系统协议为"一条样本 → 观测行为"；baseline 通过真实入口 `FeishuWebhookHandler` 驱动现有系统（内存 SQLite + mock adapter + 记录用 Feishu client），从事件库、同步会话与外发消息采集行为；带 pending_question 的样本先构造探针为 CLARIFYING 的活跃会话（updated_at 锚定样本时间，避开 F1 空闲超时）；指标含错误 claim 率、同步误触发率、claim P/R 与 task/健康度/时间准确率、回复类别准确率、多轮补全成功率及按类别统计；意图层指标对 baseline 记 N/A。报告输出 JSON + Markdown（含按类别表与期望/实际失败清单）至 `evals/intent/reports/`。本步不设通过门槛、不接 CI 门禁；replay/live 模式明确报"需意图层/在线模型，当前步骤仅支持 mock"；
- **LLM 兼容性脚本**（`scripts/llm_compat_check.py`，只写未运行、不入 CI）：三重门槛（provider=aihubmix、AIHUBMIX_API_KEY、--confirm-spend）不满足即退出；探测 (a) 工具调用 tool_use 块、(b) IntentResult schema 写入 prompt 的 N 次 JSON 解析/校验通过率、(c) 延迟 p50/p95、(d) token 用量；单元测试全部经 httpx MockTransport，未发生任何真实外部调用。

### 评测集类别分布（dev + test）

report_status 8、make_commitment 8、answer_pending 8（多轮回答）、query_status 8（疑问）、request_sync 8（同步正例）、acknowledge 8、chitchat 8、help 8、out_of_scope 8、negation 8、hypothetical 6、vague 6、multi_task 8、disambiguation 6、time_chinese 10、substring_trap 6、p1_p2 10、sync_near_negative 6、sync_non_pm 3、multi_turn_topic_shift 4；合计 145（dev:test ≈ 6:4）。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
pytest: 192 passed
coverage: 90.97% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
orgpilot eval-intent --split dev --mode mock / --split test --mode mock: 均运行并生成报告
```

### baseline 核心指标（无门槛，仅记录现状）

```text
dev (92 条):  claim P 47.06% / R 23.53%；task_id 58.82%；健康度 80.00%；时间 0.00%
              错误 claim 率 11.29%；同步误触发 0%；回复类别 34.78%；多轮补全 12.50%（8 条多轮）
test (53 条): claim P 0.00% / R 0.00%；task_id 20.00%；健康度 0.00%；时间 0.00%
              错误 claim 率 11.11%；同步误触发 0%；回复类别 26.42%；多轮补全 0.00%（8 条多轮）
失败最集中类别：时间表达解析（time_chinese 10/10）、P1/P2 误处理（p1_p2 10/10 产生副作用或答非所问）、
report_status/make_commitment/query_status 各 split 的措辞变体、多轮回答未合并进待补全 claim。
```

### 与任务说明的差异（如实标注）

- 分支基点：任务书写"从最新 origin/main 新建"，但上一轮受"不要 push"约束，F1/F2 合并只存在于本地 main（6bf22a2），origin/main 仍停在没有 F1/F2 的 7a9ed18；为满足"含 F1、F2 已合并"的前提，本分支自本地 main 新建；
- 回复类别在任务书示例的 7 类之外补充了 `clarify`（机器人追问轮次），示例列表为"例如"性质，已 在 schema docstring 注明；
- `sends_confirmation_card` 语义锚定 ADR-0008 §4 的"同步确认卡片"（自然语言同步须经确认），不涵盖改期审批卡片；
- 评测生成的报告文件随本分支提交（作为基线留档），后续运行会覆盖。

---


## 2026-10-11：修复飞书消息链路 4 个 P0 缺陷（分支 `fix/feishu-sync-routing-p0`）

### 已修复

- **B1 探针回复路径在网关层必然崩溃**（`gateway/service.py`、`coordination/sync_coordinator.py`）：
  - 根因：`handle_sync_member_reply` 读取了 `InMemoryEventLog` 上不存在的 `.events` 属性（只有 `all()`），并以错误签名调用 `save_state(project_id, state)`；`handle_member_reply` 丢弃 `run_turn` 返回值导致本轮生成的事件从未持久化；缓存的 coordinator agent 落后于数据库；
  - 修复：`handle_member_reply` 返回（收敛标志、澄清问题、本轮全部新事件）；`handle_sync_member_reply` 在每次处理回复前用 `get_or_replay_agent` 刷新 coordinator 的 agent 与 adapter，仅持久化本轮新事件，并经 `save_agent_state` 保存快照、cases 与 approvals；`bootstrap-sandbox` 路由同类的 `save_state` 签名错误一并改正；
- **B2 同步意图判断顺序错误且无角色校验**（`feishu/webhook.py`）：
  - 根因：关键词子串匹配排在"是否为探针回复"之前，探针成员回复含"当前进度"等词时误开新会话；任何人都可触发全员同步；
  - 修复：存在活跃会话且发送者在 `member_probes` 中时一律按探针回复处理；仅投影角色为 `pm` 的成员可触发同步，其余消息走普通抽取；关键词列表保持不变。sandbox-chat 路由存在同款顺序与角色缺陷，按相同策略修正；
- **B3 收敛回复被二次处理、重复投递不幂等**（`feishu/webhook.py`、`coordination/sync_coordinator.py`、`domain/sync_models.py`）：
  - 根因：`handle_sync_member_reply` 返回 converged 后未 return，同一条消息继续走 `ingest_message`，两条路径生成的 event_id 不同（sync 路径的 MessageContext 未带 source_ref），产生重复 claim；飞书重复投递同一 message_id 会重复累加 `turns_count`；
  - 修复：探针回复处理完一律 return，返回体区分 `clarification_sent` 与 `probe_collected`；飞书 message_id 作为 source_ref 贯穿 sync 路径，两条管线派生相同 event_id 由事件库去重；`MemberProbeState` 记录 `processed_source_refs`，重复投递不再计数、不再产生事件；
- **B4 未注册发送者写入"毒事件"导致项目永久不可用**（`gateway/service.py`、`feishu/webhook.py`）：
  - 根因：`ingest_message` 先把抽取事件写入事件库再投影，`_report_health` 校验 `unknown event actor` 抛出 `DomainInvariantError` 时事件已落库，此后每次重放必然失败；
  - 修复：抽取事件先在内存投影副本上整批校验，通过后才写事件库；校验失败返回显式错误结果（不抛未处理异常），`auto_run_turn=False` 同样满足；webhook 层对未注册发送者直接拒收（记日志 + 回复简短提示，不产生任何事件），未实现自动注册成员（留待产品决策）；`/messages` 与 sandbox-chat 将校验错误转译为 400 / message_rejected。

### 追加修复（同分支第二轮）

- **F1 仅等待中的成员按探针回复处理；同步会话增加超时**（`feishu/webhook.py`、`gateway/routes/coordination.py`、`coordination/sync_coordinator.py`）：
  - 根因：路由仅凭"发送者在 member_probes 中"判定探针回复，已 COLLECTED 的成员再发无关消息会被重新追问，探针状态从 collected 退回 clarifying；会话没有超时，只要有一人不回复就永远活跃；
  - 修复：仅探针状态为 PENDING / CLARIFYING 的成员消息按探针回复处理，webhook、sandbox-chat 与 `handle_member_reply` 内部防护三处一致；COLLECTED 成员的后续消息走同步关键词判断再走普通抽取；
  - `ProgressSyncCoordinator` 新增 `session_timeout`（默认 24 小时，可经构造参数与 `GatewayService(sync_session_timeout=...)` 配置），超时判断基于消息的 occurred_at（默认回退墙钟）：空闲超时后会话标记 `SyncSessionStatus.TIMEOUT`、未收敛探针标记 `ProbeMemberStatus.TIMEOUT`，用已收集信息合成简报（推荐项中列出未回复成员）并投递 PM，`get_active_session` 不再返回已超时会话；
- **F2 飞书适配器探针/追问消息签名错误**（`feishu/adapter.py`）：
  - 根因：`send_private_message` 文本分支调用 `client.send_message(receive_id=..., content=...)` 缺少必填 `msg_type`，且 content 传纯字符串，真实飞书中立即抛 TypeError，被 sync_coordinator 的 try/except 吞掉仅记日志，探针询问与多轮追问从未送达；
  - 修复：改为 `msg_type="text"`、`content={"text": msg_text}`；任一目标发送失败即返回 `CommandStatus.FAILED` 并附错误信息（记录审计日志），不再静默成功；成功仍返回 SUCCESS。

#### 追加测试

- `test_collected_member_is_not_re_probed_by_later_messages`：COLLECTED 成员在会话活跃期间的无关消息走普通路由，探针状态不回退、不再追问；
- `test_session_timeout_briefs_collected_info_and_routes_normally`：25 小时后消息触发会话超时，消息走普通路由，简报基于已收集信息生成并标注未回复成员，`get_active_session` 不再返回该会话；
- `test_feishu_adapter_sends_probe_and_clarification_text`：探针与追问以 `text` + `{"text": ...}` 结构送达；
- `test_feishu_adapter_reports_failed_when_text_send_raises`：发送失败返回 FAILED 且携带错误信息；
- sandbox 端到端测试补充 COLLECTED 成员后续消息走 `normal_turn` 的一致性断言。

#### 配套改动（非缺陷本身，如实说明）

- `tests/test_feishu_webhook.py`、`tests/test_gateway_api.py` 的固定时间基准（2026-09-10）与真实时钟的审批过期窗口冲突，4 个既有测试自 2026-09-12 起在 origin/main 上持续失败；改为锚定真实时钟并动态推导期望改期时间（独立 `test:` 提交）；
- origin/main 实测覆盖率 86.1%（低于 90% 门槛，开发日志中的 93% 早于 sync 与 ws 模块加入），为恢复门槛补充了 sandbox 端到端、简报卡片、dotenv/配置校验、`/messages` 拒收、审批决策错误分支与飞书 client 回退分支的测试（独立 `test:` 提交）。

### 新增测试（T1-T6 均为飞书 webhook 端到端，内存 SQLite + mock adapter）

- T1 `test_sync_probe_reply_persists_health_event_and_replays_consistently`：PM 发起同步、探针成员完整回复，事件落库且重放健康度一致；
- T2 `test_probe_reply_with_sync_keyword_is_not_treated_as_new_sync`：活跃会话中含同步关键词的回复按回复处理，不新建会话；
- T3 `test_non_pm_member_cannot_trigger_progress_sync`：非 PM 成员发"同步进度"不创建会话；
- T4 `test_converged_probe_reply_is_idempotent_per_message_id`：收敛回复仅产生一组 claim；同一 message_id 重复投递不改变 turns_count 与事件数；
- T5 `test_unregistered_sender_writes_no_events_and_pm_still_works`：未注册成员发风险消息零事件，PM 消息正常处理；
- T6 `test_sync_reply_persists_turn_outputs_and_survives_replay`：sync 路径 run_turn 生成的事件、cases 与 approvals 重放后仍在。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
pytest: 166 passed
coverage: 90.59% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

### 未覆盖与已知遗留（如实标注）

- `feishu/ws.py`（WebSocket 监听器内部逻辑）覆盖率仍约 47%，本次未扩展；
- `feishu/adapter.py` 澄清分支调用 `client.send_message(receive_id=..., content=...)` 缺少必填的 `msg_type`，属潜在缺陷，超出本次 4 个缺陷范围未改动、未覆盖；
- `bootstrap-sandbox` 路由的"重置"语义在二次调用时因 member.registered 幂等冲突会失败（事件 ID 含时间戳），属演示端点遗留问题，未改动；
- 真实飞书开放平台联调与真实 LLM 抽取未执行，避免未经确认的外部副作用。

---


## 2026-08-26：W1 Web 全局可视化控制台与实时 DAG 拓扑看板

### 已实现

- 创建分支 `w1/web-dashboard-and-dag` 并编写 `ADR-0006`；
- 构建 DAG 拓扑计算引擎（`src/orgpilot/gateway/routes/dag.py`）：
  - `GET /api/v1/projects/{project_id}/dag`：自动计算拓扑分层（Topological Layers）、入度/出度、关键路径（Critical Path）与多跳风险传播高亮（Impacted Tasks）；
  - `GET /api/v1/projects/{project_id}/timeline`：聚合不可变事件、Case 状态转移、审批请求与执行审计，生成全链路可解释性时序时间线；
- 编写 Single-Page 现代化控制台 UI（`src/orgpilot/gateway/static/index.html`）：
  - 暗色现代化架构（Tailwind CSS），支持项目快速切换与 5s 自动轮询；
  - 原生可交互 SVG 渲染自适应 DAG 拓扑图，支持缩放/拖拽与节点点击抽屉（Node Inspector Drawer）；
  - 包含 PM 快速审批决策面板与自然语言消息摄入/轮次触发模拟台；
- 在 FastAPI 网关挂载根路径静态控制台（访问 `http://localhost:8000/` 或 `/dashboard` 即开即用）；
- 编写单元与端到端集成测试（`tests/test_gateway_dag_and_timeline.py`）。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples, 100% F1, 100% Grounding)
pytest: 144 passed in 5.58s
coverage: 93.76% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 192 files formatted)
```

---

## 2026-08-26：集成硬化与完成度校正

### 已修复

- 审批状态机严格校验指定审批人，拒绝请求体或卡片回调中的身份冒用；
- Agent 产生的任务更新事件写入 SQL 事件库，快照恢复后不会丢失已批准改期；引用未持久化事件的异常快照会被丢弃并完整重放；
- 消息事件 ID 改为基于上游消息 ID 或完整消息身份的稳定哈希，消除秒级碰撞；
- 增加显式 `OrgPilotSettings`，外部 LLM 和飞书适配器默认关闭，避免测试意外调用和计费；
- 接入 AIHubMix Anthropic Messages 兼容客户端，按内容块类型选择文本并执行 Pydantic 校验；
- 真实飞书适配器接入 GatewayService，Webhook 增加 Verification Token 与审批人 `open_id` 校验；
- 网关默认使用持久化 SQLite，并可通过 `ORGPILOT_DATABASE_URL` 配置 PostgreSQL；项目 REST API 支持可选 Bearer Token 门禁；
- 校正文档：WebSocket、日历、多维表格、在线模型准确率和生产级多实例一致性均不再被标记为已完成。

### 验证状态

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 个离线规则样本；不代表在线模型准确率)
pytest: 138 passed
coverage: 93.81% branch-aware total coverage (fail_under=90%)
ruff: All checks passed
git diff --check: PASS
```

真实 AIHubMix 和飞书调用未执行，避免未经确认产生费用或外部副作用。

---

## 2026-08-26：F1 飞书开放平台适配层与交互式卡片原型

### 已实现

- 创建分支 `f1/feishu-integration` 并编写 `ADR-0005`；
- 编写《飞书开放平台极速配置指南》（`docs/feishu-setup-guide.md`），提供 2 分钟极速建应用与权限配置文档；
- 构建飞书 2.0 交互式卡片生成器（`src/orgpilot/feishu/cards.py`），支持延期追问卡片、带回调按钮的审批卡片、原地审批完成态卡片与群周知卡片；
- 实现统一飞书 OpenAPI 客户端（`src/orgpilot/feishu/client.py`）：
  - `AsyncFeishuClient`：管理 `tenant_access_token` 自动获取、TTL 提前刷新与消息/卡片/任务 API 调用；
  - `MockFeishuClient`：实现离线调用审计与 100% 确定性自动化测试。
- 实现 `FeishuCollaborationAdapter`（`src/orgpilot/feishu/adapter.py`），将 4 类 ActionCommand 转为飞书交互卡片与任务 API 更新；
- 实现飞书 Webhook 与事件调度器（`src/orgpilot/feishu/webhook.py`），处理 URL Challenge 握手、自然语言消息摄入与卡片按钮回调；
- 在 FastAPI 网关挂载 `/api/v1/feishu/events` 路由；
- 编写完整的单元与端到端集成测试（`tests/test_feishu_cards.py`, `tests/test_feishu_client.py`, `tests/test_feishu_adapter.py`, `tests/test_feishu_webhook.py`）。

### 当前验证结果

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples, 100% F1, 100% Grounding)
pytest: 122 passed in 2.23s
coverage: 94.76% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 93 files formatted)
```

---

## 2026-08-26：P1 SQL 异步持久化存储与 FastAPI 事件网关

### 已实现

- 创建分支 `p1/storage-and-gateway` 并编写 `ADR-0004`；
- 引入 SQLAlchemy 2.0 Async 现代声明式模型与异步会话管理（`src/orgpilot/storage/`）；
- 实现双驱动支持：生产环境支持 PostgreSQL（`asyncpg`），本地开发与测试原生支持异步 SQLite（`aiosqlite`）；
- 实现数据库级不可变追加日志 `SqlEventStore`，支持 `(project_id, event_id)` 唯一索引幂等与 SHA-256 哈希冲突校验；
- 实现 `SqlStateStore`，支持 `OrgState` 快照持久化，以及 `CaseLedger` 与 `ApprovalManager` 的数据库持久化与瞬时恢复；
- 构建 FastAPI 异步事件网关与 REST API（`src/orgpilot/gateway/`）：
  - `/api/v1/projects/{project_id}/events`（结构化事件摄入与流式查询）；
  - `/api/v1/projects/{project_id}/messages`（自然语言文本摄入、自动 ClaimExtractor 抽取与可选 Agent 轮次驱动）；
  - `/api/v1/projects/{project_id}/cases`（Case 查询与单 Case 详情）；
  - `/api/v1/projects/{project_id}/approvals`（待审批列表与批准/拒绝决策 Webhook）；
  - `/api/v1/projects/{project_id}/run-turn`（显式触发 Agent 协调轮次）；
  - `/api/v1/projects/{project_id}/state`（项目当前投影状态快照）。
- 编写完整的异步测试套件（`tests/test_sql_event_store.py`, `tests/test_sql_state_store.py`, `tests/test_gateway_api.py`）。

### 当前验证结果

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples, 100% F1, 100% Grounding)
pytest: 107 passed in 2.81s
coverage: 94.83% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 68 files formatted)
```

---

## 2026-08-26：M2 LLM 声明抽取与置信度评估

### 已实现

- 完成 M1 代码审查并合并至 `main`，发布 `m1.0.0` 标签；
- 创建分支 `m2/llm-claim-extraction` 并编写 `ADR-0003`；
- 建立 `src/orgpilot/extraction/` 结构化声明抽取模块；
- 定义 Pydantic v2 强类型输出契约（`ExtractedHealthClaim`, `ExtractedCommitment`, `ExtractionResult`）；
- 实现 `GroundingVerifier`，严格进行引文反查与上下文实体合法性校验；
- 实现 `TemporalResolver`，支持相对时间表达式向绝对 ISO 8601 时区时间戳的精确归一化；
- 实现 Provider-Agnostic `LLMClient` 协议体系（`MockLLMClient`, `RecordedReplayClient`）；
- 构建包含 20 个覆盖各类口语、假警报、多任务、相对时间、承诺与恢复样本的 Gold Dataset（`evals/extraction/gold_dataset.yaml`）；
- 实现 `orgpilot eval-extraction` 评测命令行工具并输出定量指标；
- 实现从非结构化自然语言文本到 M1 `CoordinationAgent` 闭环协调的端到端集成测试。

### 当前验证结果

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
orgpilot eval-extraction: PASS (20 samples)
  - Health Status Precision: 100.00%, Recall: 100.00%, F1: 100.00%
  - Task ID Accuracy: 100.00%
  - Slot DateTime Accuracy: 100.00%
  - False Alarm Rate: 0.00%
  - Grounding Valid Rate: 100.00%
pytest: 95 passed in 2.06s
coverage: 94.07% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 64 files formatted)
```

---

## 2026-08-26：M1 Mock 闭环协调 Agent

### 已实现

- 完成 P0 代码审查并合并至 `main`，发布 `p0.1.0` 标签；
- 创建分支 `m1/mock-coordination-loop` 并编写 `ADR-0002`；
- 引入持久化 `CaseLedger`，支持 8 种生命周期状态及自动化对齐消除（Reconciliation）；
- 规范三阶段 Action 生命周期（`CoordinationAction` 候选 → `ActionCommand` 指令 → `ActionResult` 执行结果）；
- 实现 `ApprovalManager` 审批状态机，严格防范未授权修改、过期审批、拒绝执行及重复消费；
- 实现 `MockCollaborationAdapter`，支持 4 种核心操作、调用审计与模拟反馈事件生成；
- 实现有边界的 `CoordinationAgent`（Agent Loop），支持可重复验证的确定性轨迹；
- 构建 5 个多轮交互场景（S1-S5），并通过 CLI 与自动化测试进行严格断言。

### 当前验证结果

```text
orgpilot replay --all: 9/9 PASS (4 P0 + 5 M1)
pytest: 83 passed in 1.82s
coverage: 94.96% branch-aware total coverage (fail_under=90%)
ruff: All checks passed (0 errors, 52 files formatted)
```

---

## 2026-08-26：P0 领域、事件与 Ground Truth

### 已实现

- 建立 `main` Git 基线提交，并创建 `p0/domain-events-ground-truth` 分支；
- 建立 `src/` Python 工程结构和锁定依赖环境；
- 分离任务正式 workflow 与来源支持的 health 状态；
- 实现六类严格、不可变、带时区的版本化事件；
- 实现进程内 append-only Event Log、幂等和 ID 冲突拒绝；
- 实现成员、任务、健康声明和承诺投影；
- 实现同一陈述者声明 supersession 和多来源冲突保留；
- 实现依赖图校验、循环拒绝、传递影响路径；
- 实现 Coordination Case、缺失信息识别和恢复时间询问候选；
- 将风险与审批判断放入独立 Policy Engine；
- 建立四个 YAML Ground Truth 场景和统一 CLI 回放入口。

### 当前验证结果

```text
orgpilot replay --all: 4/4 PASS
pytest: 38 passed
coverage: 95.75% branch-aware total coverage
ruff: All checks passed
```

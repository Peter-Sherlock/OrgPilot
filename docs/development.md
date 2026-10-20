# 开发指南

## 环境

- Python 3.12 或更高版本；
- 使用 `uv` 创建仓库内 `.venv`；
- 依赖和工具版本由 `pyproject.toml` 与 `uv.lock` 管理。

Windows 权限受限环境下使用仓库内缓存：

```powershell
$env:UV_CACHE_DIR='.uv-cache'
uv sync --dev
```

## 验证命令

```powershell
$env:UV_CACHE_DIR='.uv-cache'
uv run orgpilot replay --all
uv run pytest --basetemp .pytest-tmp/current
uv run pytest --cov=orgpilot --cov-report=term-missing --basetemp .pytest-tmp/coverage
uv run ruff check .
```

通过 `--basetemp` 把临时目录限定在 `.pytest-tmp` 内，避免 Windows 用户级临时目录 ACL 干扰。
pytest 会清理所指定的目录，请为不同运行选择专用子目录，不要指向业务数据目录。
`.uv-cache`、`.venv` 和 `.pytest-tmp` 均不进入 Git。若 uv 缓存权限失败，可使用
`.\.venv\Scripts\python.exe -u -m pytest` 与 `.\.venv\Scripts\ruff.exe` 执行同样检查。

## Git 工作流

- `main`：可验证基线；
- 每个阶段使用独立分支；
- 当前工作分支：`codex/m3-feishu-acceptance`；P0 分支仅为历史基线；
- 提交前执行 replay、pytest、coverage、ruff 和 `git diff --check`；
- 每个提交只收口当前阶段，不混入未验收的 M4/M5 功能；提交/推送按操作者授权执行。

当前 Codex 桌面沙箱将 Git 元数据保存在工作树内的 `.git-data`，并由隐藏的
`.git` 指针文件引用。`.git-data` 被忽略，它只影响本地仓库元数据，不影响克隆后的
标准 Git 布局。

## 目录结构

```text
src/orgpilot/
├── domain/          # 领域词汇、状态、稳定异常
├── events/          # 不可变事件和事件日志
├── state/           # 事件投影
├── dependencies/    # 图校验和影响传播
├── coordination/    # Case、指令、任务提案审批、进度同步
├── policy/          # 权限与审批规则
├── agent/           # 有界协调循环
├── extraction/      # 意图路由、槽位抽取与接地验证
├── adapter/         # 平台契约与 Mock
├── feishu/          # OpenAPI、HTTP/WS 与独立 L2 验收
├── gateway/         # 服务、REST、outbox 调度与 Web 沙箱
├── storage/         # SQL 事件/快照/outbox
└── scenarios/       # 回放和 Ground Truth 评估

evals/scenarios/     # 版本化 YAML 场景
tests/               # 单元测试、契约测试、场景测试
docs/                # 架构、语义、开发记录和 ADR
```

## Definition of Done

当前阶段变更只有在以下条件同时满足时才算完成：

- 新状态都能追溯到事件；
- 重放结果确定且幂等；
- 领域不变量有失败测试；
- 九个 Ground Truth 场景（4 P0 + 5 M1）全部通过，34 条离线金标回归通过；
- 总测试覆盖率不低于 90%；
- ruff 和 `git diff --check` 无错误；
- 文档明确区分已实现能力与未来计划。

L2 还必须离线覆盖：只允许私聊用户、并发/重复事件最多一次发送、过期拒绝、审计写入失败拒绝
发送、发送结果不确定不重试、SDK 线程确实退出、凭证不进入日志。线上通过需另有真实发送者
确认可见的证据；JSON 中 `api_accepted` 只能证明平台接受请求。

SDK 生命周期必须另外使用真实 `lark-oapi` 构造函数、缓存任务和关闭方法做离线回归，不能仅用
FakeClient 代替。`tests/test_feishu_ws_lifecycle.py` 在隔离子进程中替换 HTTP/WS 网络边界，
阻断外网（Windows asyncio 的本机 self-pipe 除外），使用虚构凭证，不读取 `.env`。
检查缓存任务所属循环、取消完成、关闭结果、外部循环保护及解释器退出 stderr；这些检查不等于
真实飞书连接验收。新增 SDK 版本后，应先通过此测试，再单独规划受控在线验证。

L3-A 的离线门槛还包括 SQLite 单次额度原子占用、重开后 held 不进入 due 查询、审计落盘失败
拒绝外发、正确 SDK 操作者/卡片上下文、伪造与重复/过期回调拒绝。预览命令必须不读配置、不建
验收目录；同意测试只能记录未消费的测试审批，不得调用任务更新、额外消息或模型。

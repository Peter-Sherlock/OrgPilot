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
uv run pytest
uv run pytest --cov=orgpilot --cov-report=term-missing
uv run ruff check .
```

pytest 临时目录固定为 `.pytest-tmp`，避免 Windows 用户级临时目录 ACL 干扰项目
测试。`.uv-cache`、`.venv` 和 `.pytest-tmp` 均不进入 Git。

## Git 工作流

- `main`：可验证基线；
- 每个阶段使用独立分支；
- 当前 P0 分支：`p0/domain-events-ground-truth`；
- 提交前执行 replay、pytest、coverage、ruff 和 `git diff --check`；
- 不在一个提交里混入下一阶段的飞书、数据库或 LLM 能力。

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
├── coordination/    # Coordination Case 和候选动作
├── policy/          # 权限与审批规则
└── scenarios/       # 回放和 Ground Truth 评估

evals/scenarios/     # 版本化 YAML 场景
tests/               # 单元测试、契约测试、场景测试
docs/                # 架构、语义、开发记录和 ADR
```

## Definition of Done

一次 P0 变更只有在以下条件同时满足时才算完成：

- 新状态都能追溯到事件；
- 重放结果确定且幂等；
- 领域不变量有失败测试；
- 四个 Ground Truth 场景仍全部通过；
- 总测试覆盖率不低于 90%；
- ruff 和 `git diff --check` 无错误；
- 文档明确区分已实现能力与未来计划。

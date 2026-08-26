# Ground Truth 场景

## 场景作为可执行规格

`evals/scenarios/*.yaml` 同时包含输入事件和预期结果。测试只调用公开的
`ScenarioRunner`，然后对以下结果做结构化比较：

- 事件、成员和任务数量；
- 每个关注任务的 workflow/health 状态；
- 依赖影响以及完整路径；
- 当前开放的 Coordination Case；
- 缺失信息和候选动作；
- Policy Engine 的授权/审批结论；
- 声明与承诺的生命周期。

## P0 四个场景

| 文件 | 验证点 |
| --- | --- |
| `01_delay_propagation.yaml` | 延期沿两级依赖传播，并追问恢复时间 |
| `02_ambiguous_report_requires_question.yaml` | 低置信风险先私聊追问，不公开升级 |
| `03_workflow_health_separation.yaml` | `doing` 与 `at_risk` 同时成立，互不覆盖 |
| `04_recovery_supersedes_risk.yaml` | 新恢复声明 supersede 旧风险，旧承诺显式失效 |

## 新增场景规则

1. 使用新的 `scenario_id` 和独立 `project_id`；
2. 所有时间必须包含时区；
3. 所有实体通过事件建立，测试不得直接注入投影状态；
4. `event_id` 在场景中必须稳定；
5. Ground Truth 只声明可程序判定的结果，不写主观总结；
6. 新场景必须通过 CLI、pytest、ruff 和覆盖率检查。

## 当前边界

P0 不评测自然语言抽取准确率，也不模拟真实成员回复延迟。这里验证的是：给定已经
结构化的事件，状态恢复、风险传播和动作选择是否正确、稳定、可追溯。

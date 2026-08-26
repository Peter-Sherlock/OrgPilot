# ADR-0006：Web 全局可视化控制台与实时 DAG 拓扑看板

- 状态：Accepted
- 日期：2026-08-26

## 背景

在 P0 至 F1 阶段，OrgPilot 建立了确定性状态投影、依赖分析、Case 账本、LLM 声明抽取、SQL 异步持久化及飞书 2.0 交互卡片。

为了给管理者（PM、架构师、技术总监）提供全局的**可解释性溯源与项目拓扑全景视窗**，需要一个集成的 Web 可视化控制台：
1. **零部署门槛**：直接由 FastAPI 网关托管，无需额外的 npm/node 打包部署，访问 `http://localhost:8000/` 即开即用；
2. **直观的 DAG 拓扑图**：实时计算任务层次结构、关键路径（Critical Path）与风险扩散红线；
3. **全链路可解释性**：对任意 Case 可视化展示完整的证据流转闭环（原始消息 → 声明抽取 → Policy 判定 → 审批流 → 飞书执行）；
4. **统一的 REST API 支撑**：提供 `/api/v1/projects/{id}/dag` 与 `/api/v1/projects/{id}/timeline` 标准端点。

## 决策

1. **DAG 拓扑计算引擎 (`src/orgpilot/gateway/routes/dag.py`)**：
   - 基于 `DependencyAnalyzer` 与 `OrgState` 计算拓扑分层（Topological Generations/Layers）；
   - 计算各节点入度、出度、多跳受阻影响链路以及关键路径（Critical Path）；
   - 返回标准化的 `DagResponse` JSON 数据模型。

2. **可解释性时间线聚合 (`GET .../timeline`)**：
   - 聚合事件日志、Case 生命周期状态转移、审批请求与适配器动作审计；
   - 生成统一的时序时间线对象列表（`TimelineResponse`），支持按任务和 Case 溯源过滤。

3. **内置 Single-Page 现代化控制台 (`src/orgpilot/gateway/static/index.html`)**：
   - 采用现代化 Dark-themed UI 架构与 Tailwind CSS 样式系统；
   - 基于原生可交互 SVG 渲染自适应 DAG 拓扑，支持缩放、拖拽与节点点击交互；
   - 包含多项目切换、健康度统计卡片、节点详情抽屉、Case 详情流与快速交互模拟器。

## 后果

正面影响：
- 极大提升多智能体系统的透明度与说服力；
- PM 无需查阅日志或数据库，即可一目了然看清风险源头与受波及下游；
- 保持极低的运行与部署复杂度（零外部构建依赖）。

限制与代价：
- 当项目包含数百个超大规模任务时，拓扑图需要支持视口虚拟化或分组折叠。

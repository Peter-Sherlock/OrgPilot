"""FastAPI routes for interactive DAG topology and explainability timeline."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from orgpilot.dependencies.analyzer import DependencyAnalyzer
from orgpilot.domain.enums import HealthStatus, WorkflowStatus
from orgpilot.gateway.schemas import (
    DagEdge,
    DagNode,
    DagResponse,
    DagSummary,
    TimelineEntry,
    TimelineResponse,
)
from orgpilot.gateway.service import GatewayService

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["dag"])


def get_service(request: Request) -> GatewayService:
    return GatewayService(request.app.state.db)


@router.get("/dag", response_model=DagResponse)
async def get_project_dag(
    project_id: str,
    service: GatewayService = Depends(get_service),
) -> DagResponse:
    """Calculates interactive DAG topology, layering, critical path, and risk propagation."""
    agent = await service.get_or_replay_agent(project_id)
    state = agent.projector.state
    analyzer = DependencyAnalyzer()

    # 1. Collect tasks and dependencies
    tasks = state.tasks

    # In-degree, out-degree, and adjacency
    in_degrees: dict[str, int] = dict.fromkeys(tasks, 0)
    out_degrees: dict[str, int] = dict.fromkeys(tasks, 0)
    adj: dict[str, list[str]] = {t_id: [] for t_id in tasks}
    raw_edges: list[tuple[str, str]] = []

    for task_id, task in tasks.items():
        for dep_id in task.dependencies:
            if dep_id in tasks:
                # task_id depends on dep_id: dep_id -> task_id
                adj[dep_id].append(task_id)
                out_degrees[dep_id] += 1
                in_degrees[task_id] += 1
                raw_edges.append((dep_id, task_id))

    # 2. Compute topological layers (roots at 0, children at max(parent + 1))
    layers: dict[str, int] = {}
    for t_id, indeg in in_degrees.items():
        if indeg == 0:
            layers[t_id] = 0

    # Breadth-first / relaxation for longest distance in DAG
    queue = [t_id for t_id, layer in layers.items() if layer == 0]
    while queue:
        curr = queue.pop(0)
        curr_layer = layers[curr]
        for neighbor in adj.get(curr, []):
            if neighbor not in layers or layers[neighbor] < curr_layer + 1:
                layers[neighbor] = curr_layer + 1
                queue.append(neighbor)

    for t_id in tasks:
        if t_id not in layers:
            layers[t_id] = 0

    # 3. Analyze risk propagation
    impact_tuples = analyzer.impacts(tasks)
    impacted_task_ids = {imp.impacted_task_id for imp in impact_tuples}
    impacted_edges_set = {(imp.source_task_id, imp.impacted_task_id) for imp in impact_tuples}

    # 4. Critical path estimation (longest path in DAG)
    critical_path: list[str] = []
    if tasks:
        # Find node with maximum layer
        max_layer_node = max(tasks.keys(), key=lambda t: layers.get(t, 0), default=None)
        if max_layer_node:
            curr_trace: list[str] = [max_layer_node]
            curr = max_layer_node
            while layers.get(curr, 0) > 0:
                curr_layer = layers[curr]
                parent = next(
                    (
                        p
                        for p, children in adj.items()
                        if curr in children and layers.get(p, 0) == curr_layer - 1
                    ),
                    None,
                )
                if parent:
                    curr_trace.insert(0, parent)
                    curr = parent
                else:
                    break
            critical_path = curr_trace

    critical_edges_set = {
        (critical_path[i], critical_path[i + 1]) for i in range(len(critical_path) - 1)
    }

    # 5. Build nodes
    nodes: list[DagNode] = []
    on_track_cnt = 0
    at_risk_cnt = 0
    delayed_cnt = 0
    completed_cnt = 0

    for task_id, task in sorted(tasks.items()):
        health = task.health_status
        wf = task.workflow_status

        if wf == WorkflowStatus.DONE:
            completed_cnt += 1
        elif health == HealthStatus.DELAYED:
            delayed_cnt += 1
        elif health == HealthStatus.AT_RISK:
            at_risk_cnt += 1
        elif health == HealthStatus.ON_TRACK:
            on_track_cnt += 1

        claims = [
            state.health_claims[cid] for cid in task.health_claim_ids if cid in state.health_claims
        ]
        blockers = [c.blocker for c in claims if c.blocker]
        exp_comp = next((c.expected_completion for c in claims if c.expected_completion), None)

        nodes.append(
            DagNode(
                task_id=task.task_id,
                title=task.title,
                owner_id=task.owner_id,
                workflow_status=wf.value,
                health_status=health.value,
                deadline=task.deadline,
                layer=layers.get(task_id, 0),
                in_degree=in_degrees.get(task_id, 0),
                out_degree=out_degrees.get(task_id, 0),
                is_at_risk=health == HealthStatus.AT_RISK or task_id in impacted_task_ids,
                is_delayed=health == HealthStatus.DELAYED,
                is_critical_path=task_id in critical_path,
                blockers=blockers,
                expected_completion=exp_comp,
            )
        )

    # 6. Build edges
    edges: list[DagEdge] = []
    for from_t, to_t in raw_edges:
        is_imp = (from_t, to_t) in impacted_edges_set or to_t in impacted_task_ids
        is_crit = (from_t, to_t) in critical_edges_set
        edges.append(
            DagEdge(
                from_task=from_t,
                to_task=to_t,
                is_impacted=is_imp,
                is_critical=is_crit,
            )
        )

    summary = DagSummary(
        total_tasks=len(tasks),
        on_track_count=on_track_cnt,
        at_risk_count=at_risk_cnt,
        delayed_count=delayed_cnt,
        completed_count=completed_cnt,
        critical_path=critical_path,
        impacted_tasks=sorted(impacted_task_ids),
    )

    return DagResponse(
        project_id=project_id,
        nodes=nodes,
        edges=edges,
        summary=summary,
    )


@router.get("/timeline", response_model=TimelineResponse)
async def get_project_timeline(
    project_id: str,
    service: GatewayService = Depends(get_service),
) -> TimelineResponse:
    """Aggregates unified explainability timeline linking events, cases, and approvals."""
    agent = await service.get_or_replay_agent(project_id)
    events = await service.event_store.get_events(project_id)
    cases = agent.case_ledger.get_all_cases()
    approvals = agent.approval_manager.get_all_requests()

    entries: list[TimelineEntry] = []

    # 1. Map events
    for evt in events:
        details: dict[str, Any] = {
            "source": evt.source.value if hasattr(evt.source, "value") else str(evt.source),
            "source_ref": evt.source_ref,
            "payload": (
                evt.payload.model_dump(mode="json")
                if hasattr(evt.payload, "model_dump")
                else dict(evt.payload)
            ),
        }
        task_id = getattr(evt.payload, "task_id", None)
        title = f"事件: {evt.event_type}"
        desc = f"由 {evt.actor_id or 'System'} 触发"

        if evt.event_type == "task.health_reported":
            h_stat = getattr(evt.payload, "health_status", "")
            title = f"健康度声明: {task_id} [{h_stat}]"
            desc = f"报告状态: {h_stat}, 置信度: {getattr(evt.payload, 'confidence', 1.0):.2f}"
        elif evt.event_type == "task.created":
            title = f"任务创建: {task_id} ({getattr(evt.payload, 'title', '')})"
            wf_stat = getattr(evt.payload, "workflow_status", "")
            desc = f"负责人: {getattr(evt.payload, 'owner_id', '')}, 状态: {wf_stat}"
        elif evt.event_type == "task.updated":
            title = f"排期更新: {task_id}"
            desc = f"最新截止时间: {getattr(evt.payload, 'deadline', '')}"
        elif evt.event_type.startswith("directive."):
            action = evt.event_type.split(".", 1)[1]
            action_labels = {
                "issued": "📩 指令下达",
                "acknowledged": "✅ 指令确认",
                "completed": "🏁 指令完成",
                "reminded": "⏰ 指令催办",
                "escalated": "🚨 指令升级",
                "delivered": "📨 指令送达",
                "delivery_failed": "⚠️ 指令送达失败",
                "clarification_requested": "🤔 指令澄清",
                "clarification_resolved": "💬 指令澄清完成",
            }
            payload = evt.payload
            dir_id = getattr(payload, "directive_id", "") or getattr(
                payload, "clarification_id", ""
            )
            title = f"{action_labels.get(action, evt.event_type)}: {dir_id}"
            if action == "issued":
                desc = (
                    f"{getattr(payload, 'issuer_id', '')} → {getattr(payload, 'target_id', '')}"
                    f"，截止: {getattr(payload, 'deadline', '未指定')}"
                )
            else:
                desc = f"指令 {dir_id} 状态变更由 {evt.actor_id} 触发"

        entries.append(
            TimelineEntry(
                entry_id=evt.event_id,
                timestamp=evt.occurred_at,
                category="event",
                title=title,
                description=desc,
                status=None,
                task_id=task_id,
                actor_id=evt.actor_id,
                details=details,
            )
        )

    # 2. Map cases
    for c in cases:
        entries.append(
            TimelineEntry(
                entry_id=c.case_id,
                timestamp=c.created_at or datetime.now(UTC),
                category="case",
                title=f"Coordination Case: {c.case_id}",
                description=f"针对任务 {c.source_task_id} 的风险协调，当前状态: {c.status.value}",
                status=c.status.value,
                task_id=c.source_task_id,
                actor_id=c.waiting_for,
                details={
                    "status": c.status.value,
                    "terminal_reason": c.terminal_reason,
                    "impacted_task_ids": list(c.impacted_task_ids),
                    "executed_commands": [
                        cmd.model_dump(mode="json") for cmd in c.executed_commands
                    ],
                },
            )
        )

    # 3. Map approvals
    for app in approvals:
        task_id = app.proposed_command.payload.get("task_id")
        entries.append(
            TimelineEntry(
                entry_id=app.approval_id,
                timestamp=getattr(app, "created_at", None) or datetime.now(UTC),
                category="approval",
                title=f"审批请求: {app.approval_id}",
                description=f"向 {app.approver_id} 发起改期审批，状态: {app.status.value}",
                status=app.status.value,
                task_id=task_id,
                actor_id=app.approver_id,
                details={
                    "rejection_reason": app.rejection_reason,
                    "approved_at": app.approved_at.isoformat() if app.approved_at else None,
                    "proposed_command": app.proposed_command.model_dump(mode="json"),
                },
            )
        )

    # Sort chronologically
    entries.sort(key=lambda e: e.timestamp or datetime.now(UTC))

    return TimelineResponse(
        project_id=project_id,
        total_entries=len(entries),
        entries=entries,
    )

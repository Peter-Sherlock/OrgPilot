from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request

from orgpilot.domain.enums import WorkflowStatus
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    OrgEvent,
    TaskCreatedEvent,
    TaskCreatedPayload,
)
from orgpilot.gateway.schemas import (
    ProjectStateResponse,
    TurnRunRequest,
    TurnRunResponse,
)
from orgpilot.gateway.service import GatewayService

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["coordination"])


def get_service(request: Request) -> GatewayService:
    return request.app.state.gateway_service


@router.post("/run-turn", response_model=TurnRunResponse)
async def run_turn(
    project_id: str,
    body: TurnRunRequest | None = None,
    service: GatewayService = Depends(get_service),
) -> TurnRunResponse:
    """Explicitly triggers one turn of the CoordinationAgent loop."""
    now = (body.current_time if body and body.current_time else None) or datetime.now(UTC)
    agent = await service.get_or_replay_agent(project_id)
    turn_trace, _ = await service.run_agent_turn(agent, [], now)

    return TurnRunResponse(
        round_number=turn_trace.round_number,
        termination_reason=turn_trace.termination_reason.value,
        active_cases_count=len(turn_trace.active_case_ids),
        executed_commands=list(turn_trace.executed_command_ids),
    )


@router.get("/state", response_model=ProjectStateResponse)
async def get_state(
    project_id: str,
    service: GatewayService = Depends(get_service),
) -> ProjectStateResponse:
    """Returns the current projected state, active cases, and pending approvals."""
    agent = await service.get_or_replay_agent(project_id)
    state = agent.projector.state

    return ProjectStateResponse(
        project_id=project_id,
        tasks={t_id: t.model_dump(mode="json") for t_id, t in state.tasks.items()},
        members={m_id: m.model_dump(mode="json") for m_id, m in state.members.items()},
        active_cases=[c.model_dump(mode="json") for c in agent.case_ledger.get_active_cases()],
        pending_approvals=[
            r.model_dump(mode="json") for r in agent.approval_manager.get_pending_requests()
        ],
    )


@router.post("/sync")
async def start_progress_sync_endpoint(
    project_id: str,
    initiated_by: str = "pm",
    custom_intro: str | None = None,
    service: GatewayService = Depends(get_service),
) -> dict:
    """Explicitly triggers a proactive progress sync probe across all active project members."""
    session = await service.start_progress_sync(
        project_id=project_id,
        initiated_by=initiated_by,
        custom_intro=custom_intro,
    )
    return session.model_dump(mode="json")


@router.get("/sync-sessions/{session_id}")
async def get_sync_session_endpoint(
    project_id: str,
    session_id: str,
    service: GatewayService = Depends(get_service),
) -> dict:
    """Retrieves progress sync session status, member probe replies, and executive briefing."""
    coordinator = await service.get_sync_coordinator(project_id)
    session = coordinator.get_session(session_id)
    if not session:
        return {"error": "session not found", "session_id": session_id}
    return session.model_dump(mode="json")


@router.post("/bootstrap-sandbox")
async def bootstrap_sandbox_endpoint(
    project_id: str,
    service: GatewayService = Depends(get_service),
) -> dict:
    """Initializes or resets a demo sandbox with PM, Alice, Bob, and dependent task chain."""
    now = datetime.now(UTC)
    events: list[OrgEvent] = [
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"boot-m-pm-{now.timestamp()}",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="sandbox-init",
            occurred_at=now,
            received_at=now,
            payload=MemberRegisteredPayload(
                member_id="ou_pm", display_name="项目负责人 (PM)", role="pm"
            ),
        ),
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"boot-m-alice-{now.timestamp()}",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="sandbox-init",
            occurred_at=now,
            received_at=now,
            payload=MemberRegisteredPayload(
                member_id="ou_alice", display_name="支付工程师 Alice", role="engineer"
            ),
        ),
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"boot-m-bob-{now.timestamp()}",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="sandbox-init",
            occurred_at=now,
            received_at=now,
            payload=MemberRegisteredPayload(
                member_id="ou_bob", display_name="前端工程师 Bob", role="engineer"
            ),
        ),
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"boot-m-david-{now.timestamp()}",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="sandbox-init",
            occurred_at=now,
            received_at=now,
            payload=MemberRegisteredPayload(
                member_id="ou_david", display_name="QA 测试 David", role="qa"
            ),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id=f"boot-t-pay-{now.timestamp()}",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="sandbox-init",
            occurred_at=now,
            received_at=now,
            payload=TaskCreatedPayload(
                task_id="task-payment",
                title="支付SDK接入",
                owner_id="ou_alice",
                workflow_status=WorkflowStatus.DOING,
                deadline=now + timedelta(days=2),
            ),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id=f"boot-t-checkout-{now.timestamp()}",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="sandbox-init",
            occurred_at=now,
            received_at=now,
            payload=TaskCreatedPayload(
                task_id="task-checkout",
                title="收银台前端结账",
                owner_id="ou_bob",
                workflow_status=WorkflowStatus.TODO,
                deadline=now + timedelta(days=3),
                dependencies=("task-payment",),
            ),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id=f"boot-t-qa-{now.timestamp()}",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="sandbox-init",
            occurred_at=now,
            received_at=now,
            payload=TaskCreatedPayload(
                task_id="task-qa",
                title="支付全链路压测与验收",
                owner_id="ou_david",
                workflow_status=WorkflowStatus.TODO,
                deadline=now + timedelta(days=5),
                dependencies=("task-checkout",),
            ),
        ),
    ]
    for e in events:
        await service.event_store.append(e)

    agent = await service.get_or_replay_agent(project_id)
    await service.state_store.save_state(project_id, agent.projector.state)
    return {"status": "initialized", "members_count": 4, "tasks_count": 3}


@router.post("/sandbox-chat")
async def sandbox_chat_endpoint(
    project_id: str,
    actor_id: str,
    message: str,
    service: GatewayService = Depends(get_service),
) -> dict:
    """Handles sandbox chat from any role (PM, Alice, Bob), returning bot responses."""
    now = datetime.now(UTC)
    is_sync = any(
        k in message
        for k in [
            "跟进进度",
            "同步进度",
            "项目进度",
            "进度汇总",
            "检查进度",
            "当前进度",
            "我要知道当前的项目进度",
        ]
    )
    if is_sync:
        session = await service.start_progress_sync(project_id, initiated_by=actor_id)
        probes_cnt = len(session.member_probes)
        return {
            "type": "sync_started",
            "session_id": session.session_id,
            "bot_reply": (
                f"🚀 **收到指令！已启动全员进度探针与协同工作流**\n\n"
                f"• **探测范围**：已向 {probes_cnt} 位任务负责人发起 1 对 1 私聊探针\n"
                f"• **智能追问**：若成员回复存在时间或阻塞模糊，系统将自主多轮追问收敛\n"
                f"• **拓扑决策**：全员信息就绪后，将自动推演关键路径并呈送【决策简报】！"
            ),
            "probed_members": list(session.member_probes.keys()),
            "session": session.model_dump(mode="json"),
        }

    # Check active sync probe
    coordinator = await service.get_sync_coordinator(project_id)
    active_session = coordinator.get_active_session(project_id)
    if active_session and actor_id in active_session.member_probes:
        converged, clarify_q, session = await service.handle_sync_member_reply(
            project_id=project_id,
            member_id=actor_id,
            message=message,
            occurred_at=now,
        )
        if not converged and clarify_q:
            return {
                "type": "clarification_needed",
                "bot_reply": clarify_q,
                "session": session.model_dump(mode="json") if session else None,
            }
        if session and session.briefing:
            return {
                "type": "sync_completed",
                "bot_reply": "✅ 全员信息已采集收敛，项目决策简报已生成！",
                "session": session.model_dump(mode="json"),
                "briefing": session.briefing.model_dump(mode="json"),
            }
        return {
            "type": "member_collected",
            "bot_reply": "收到！您的任务进度已登记，等待其余成员同步收敛...",
            "session": session.model_dump(mode="json") if session else None,
        }

    # Normal message
    is_act, ext_events, agent, reason, round_num = await service.ingest_message(
        project_id=project_id,
        message=message,
        actor_id=actor_id,
        occurred_at=now,
        auto_run_turn=True,
    )
    return {
        "type": "normal_turn",
        "is_actionable": is_act,
        "extracted_events_count": len(ext_events),
        "bot_reply": (
            "已识别任务状态变更并更新项目账本" if is_act else "收到消息，当前无需要变更的任务状态"
        ),
        "turn_reason": reason,
    }

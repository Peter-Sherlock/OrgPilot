"""Agent coordination loop execution and state snapshot endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request

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

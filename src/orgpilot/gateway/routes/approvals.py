"""Human approval listing and decision endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from orgpilot.gateway.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
)
from orgpilot.gateway.service import GatewayService

router = APIRouter(prefix="/api/v1/projects/{project_id}/approvals", tags=["approvals"])


def get_service(request: Request) -> GatewayService:
    return GatewayService(request.app.state.db)


@router.get("")
async def list_approvals(
    project_id: str,
    pending_only: bool = True,
    service: GatewayService = Depends(get_service),
) -> list[dict]:
    """Lists approval requests for a project."""
    agent = await service.get_or_replay_agent(project_id)
    requests = (
        agent.approval_manager.get_pending_requests()
        if pending_only
        else agent.approval_manager.get_all_requests()
    )
    return [r.model_dump(mode="json") for r in requests]


@router.post("/{approval_id}/decision", response_model=ApprovalDecisionResponse)
async def submit_approval_decision(
    project_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    service: GatewayService = Depends(get_service),
) -> ApprovalDecisionResponse:
    """Submits a human approval decision (approved / rejected) and runs next agent turn."""
    now = datetime.now(UTC)
    agent = await service.get_or_replay_agent(project_id)

    req = agent.approval_manager.get_request(approval_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"Approval request {approval_id} not found")

    try:
        if body.decision == "approved":
            agent.approval_manager.approve(approval_id, body.approver_id, now)
        else:
            agent.approval_manager.reject(
                approval_id, body.approver_id, body.reason or "declined", now
            )

        # Run an agent turn to execute approved actions or escalate rejected actions
        turn_trace, _ = agent.run_turn([], now)
        await service.save_agent_state(agent)

        updated_req = agent.approval_manager.get_request(approval_id)
        return ApprovalDecisionResponse(
            approval_id=approval_id,
            decision=body.decision,
            status=updated_req.status.value if updated_req else "unknown",
            turn_termination_reason=turn_trace.termination_reason.value,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

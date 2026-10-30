"""Context the web console needs before it can act on a project."""

from fastapi import APIRouter, Depends, Request

from orgpilot.coordination.service import pm_member_ids
from orgpilot.gateway.schemas import ConsoleContextResponse
from orgpilot.gateway.service import GatewayService

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["console"])


def get_service(request: Request) -> GatewayService:
    return request.app.state.gateway_service


@router.get("/console-context", response_model=ConsoleContextResponse)
async def get_console_context(
    project_id: str,
    service: GatewayService = Depends(get_service),
) -> ConsoleContextResponse:
    """Names the member the console acts as when it decides approvals.

    The console has no login of its own (only the optional API token), so the server
    picks the operator: the project PM, the same member the coordinator names as the
    approver of every reschedule request. A project without a PM has no operator.
    """
    agent = await service.get_or_replay_agent(project_id)
    state = agent.projector.state
    pm_ids = pm_member_ids(state)
    if not pm_ids:
        return ConsoleContextResponse(project_id=project_id)
    operator = state.members[pm_ids[0]]
    return ConsoleContextResponse(
        project_id=project_id,
        operator_id=operator.member_id,
        operator_name=operator.display_name,
    )

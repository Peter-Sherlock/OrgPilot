"""Coordination Case query endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request

from orgpilot.gateway.service import GatewayService

router = APIRouter(prefix="/api/v1/projects/{project_id}/cases", tags=["cases"])


def get_service(request: Request) -> GatewayService:
    return request.app.state.gateway_service


@router.get("")
async def list_cases(
    project_id: str,
    active_only: bool = False,
    service: GatewayService = Depends(get_service),
) -> list[dict]:
    """Lists all or active coordination cases for a project."""
    agent = await service.get_or_replay_agent(project_id)
    cases = (
        agent.case_ledger.get_active_cases() if active_only else agent.case_ledger.get_all_cases()
    )
    return [c.model_dump(mode="json") for c in cases]


@router.get("/{case_id}")
async def get_case(
    project_id: str,
    case_id: str,
    service: GatewayService = Depends(get_service),
) -> dict:
    """Gets details for a specific coordination case."""
    agent = await service.get_or_replay_agent(project_id)
    case = agent.case_ledger.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    return case.model_dump(mode="json")

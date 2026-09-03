"""Event and natural language message ingestion endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request

from orgpilot.gateway.schemas import (
    EventIngestRequest,
    EventIngestResponse,
    MessageIngestRequest,
    MessageIngestResponse,
)
from orgpilot.gateway.service import GatewayService

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["events"])


def get_service(request: Request) -> GatewayService:
    return request.app.state.gateway_service


@router.post("/events", response_model=EventIngestResponse)
async def ingest_events(
    project_id: str,
    body: EventIngestRequest,
    service: GatewayService = Depends(get_service),
) -> EventIngestResponse:
    """Ingests one or more raw OrgEvent dictionaries into persistent event log."""
    try:
        appended, duplicates = await service.ingest_raw_events(project_id, body.events)
        total = await service.event_store.count(project_id)
        return EventIngestResponse(appended=appended, duplicates=duplicates, total_events=total)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/events")
async def get_events(
    project_id: str,
    service: GatewayService = Depends(get_service),
) -> list[dict]:
    """Retrieves all immutable events for a project."""
    events = await service.event_store.get_events(project_id)
    return [e.model_dump(mode="json") for e in events]


@router.post("/messages", response_model=MessageIngestResponse)
async def ingest_message(
    project_id: str,
    body: MessageIngestRequest,
    service: GatewayService = Depends(get_service),
) -> MessageIngestResponse:
    """Ingests a natural language chat message, extracts claims, and optionally triggers a turn."""
    try:
        result = await service.ingest_message(
            project_id=project_id,
            message=body.message,
            actor_id=body.actor_id,
            occurred_at=body.occurred_at,
            source_ref=body.message_id,
            auto_run_turn=body.auto_run_turn,
        )
        return MessageIngestResponse(
            is_actionable=result.is_actionable,
            extracted_events_count=len(result.events),
            extracted_events=[e.model_dump(mode="json") for e in result.events],
            turn_termination_reason=result.turn_reason,
            turn_round_number=result.round_num,
            intent=result.intent,
            directive_kind=result.directive_kind,
            notices=[{"actor_id": n.actor_id, "text": n.text} for n in result.notices],
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

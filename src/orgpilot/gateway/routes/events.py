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
    """Legacy low-level extraction interface (ADR-0008 §3): bypasses the intent
    router and runs the raw ClaimExtractor directly. Kept for scripts and tests;
    chat surfaces (webhook/WS/sandbox) go through the MessageRouter instead.
    """
    # Same business-timezone normalization as the Feishu entries (B5).
    occurred_at = (
        service.to_business_timezone(body.occurred_at)
        if body.occurred_at is not None
        else service.to_business_timezone(service.clock.now())
    )
    try:
        is_act, ext_events, agent, reason, round_num, error = await service.ingest_message(
            project_id=project_id,
            message=body.message,
            actor_id=body.actor_id,
            occurred_at=occurred_at,
            source_ref=body.message_id,
            auto_run_turn=body.auto_run_turn,
        )
        if error:
            raise HTTPException(status_code=400, detail=error)
        return MessageIngestResponse(
            is_actionable=is_act,
            extracted_events_count=len(ext_events),
            extracted_events=[e.model_dump(mode="json") for e in ext_events],
            turn_termination_reason=reason,
            turn_round_number=round_num,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

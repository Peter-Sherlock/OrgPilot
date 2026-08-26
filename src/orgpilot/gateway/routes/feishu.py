"""FastAPI route handler for Feishu Webhook events and card actions."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.service import GatewayService

router = APIRouter(prefix="/api/v1/feishu", tags=["feishu"])


def get_service(request: Request) -> GatewayService:
    return request.app.state.gateway_service


@router.post("/events")
async def handle_feishu_webhook(
    request: Request,
    body: dict[str, Any],
    project_id: str | None = None,
    service: GatewayService = Depends(get_service),
) -> dict[str, Any]:
    """Receives and dispatches incoming Feishu events, messages, and card action callbacks."""
    settings = request.app.state.settings
    resolved_project_id = project_id or settings.feishu_project_id
    if (
        settings.collaboration_adapter == "feishu"
        and resolved_project_id != settings.feishu_project_id
    ):
        raise HTTPException(status_code=403, detail="Unexpected Feishu project mapping")
    handler = FeishuWebhookHandler(
        service=service,
        project_id=resolved_project_id,
        verification_token=settings.feishu_verification_token,
    )
    try:
        return await handler.handle_event(body)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

"""FastAPI route handler for Feishu Webhook events and card actions."""

from typing import Any

from fastapi import APIRouter, Depends, Request

from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.service import GatewayService

router = APIRouter(prefix="/api/v1/feishu", tags=["feishu"])


def get_service(request: Request) -> GatewayService:
    return GatewayService(request.app.state.db)


@router.post("/events")
async def handle_feishu_webhook(
    request: Request,
    body: dict[str, Any],
    project_id: str = "feishu-project",
    service: GatewayService = Depends(get_service),
) -> dict[str, Any]:
    """Receives and dispatches incoming Feishu events, messages, and card action callbacks."""
    handler = FeishuWebhookHandler(service=service, project_id=project_id)
    return await handler.handle_event(body)

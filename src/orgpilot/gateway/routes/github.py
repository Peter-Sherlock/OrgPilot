"""Gateway route for GitHub webhook deliveries (ADR-0015 C3).

Verify → persist → respond: the HMAC signature is checked against the raw
body before anything else; only deliveries for a bound repository are
persisted; processing happens inline under the project's serial drain lock
and never runs a model. Unsigned or mis-signed requests are rejected before
any state is touched.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from orgpilot.github.sync import SUPPORTED_EVENTS, GitHubSyncService, verify_signature
from orgpilot.knowledge.errors import DuplicateDeliveryConflict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["github"])


def get_sync_service(request: Request) -> GitHubSyncService:
    service = getattr(request.app.state, "github_sync", None)
    if service is None:
        raise HTTPException(status_code=404, detail="GitHub project sync is disabled")
    return service


@router.post("/webhook")
async def handle_github_webhook(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    service: GitHubSyncService = get_sync_service(request)
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(settings.github_webhook_secret or "", body, signature):
        raise HTTPException(status_code=401, detail="invalid signature")
    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if not delivery_id:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery")
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    repository = payload.get("repository") or {}
    repository_id = str(repository.get("id", ""))
    binding = await service.resolve_binding(repository_id)
    if binding is None:
        # Not a bound repository: acknowledge without persisting anything.
        return {"code": 0, "msg": "unknown_repository"}
    if event_type not in SUPPORTED_EVENTS:
        # CI, stars, deployments and the like never refresh material; storing
        # them would only add inbox rows and empty sync runs.
        return {"code": 0, "msg": "ignored_event"}
    try:
        outcome = await service.ingest_webhook(
            repository_id,
            delivery_id,
            event_type,
            payload.get("action"),
            payload,
        )
    except DuplicateDeliveryConflict:
        # Same delivery id with a different payload: the original row stays,
        # and the redelivery is acknowledged so the platform stops retrying.
        return {"code": 0, "msg": "conflict"}
    # The durable inbox owns the work. The lifecycle worker consumes it after
    # acknowledgement, including retries without a subsequent notification.
    return {"code": 0, "msg": outcome}

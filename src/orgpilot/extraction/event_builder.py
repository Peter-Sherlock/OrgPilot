"""Reusable OrgEvent construction for extracted claims and commitments.

Shared by ``ClaimExtractor`` (keyword/heuristic pipeline) and the intent
handlers, so both paths produce byte-identical event ids and payloads.
"""

import hashlib
from datetime import datetime

from orgpilot.domain.enums import HealthStatus
from orgpilot.events.models import (
    CommitmentMadeEvent,
    CommitmentMadePayload,
    EventSource,
    TaskHealthReportedEvent,
    TaskHealthReportedPayload,
)


def message_identity(
    project_id: str,
    actor_id: str,
    occurred_at: datetime,
    message: str,
    source_ref: str | None,
) -> tuple[str, str]:
    """Returns the (message_key, source_ref) pair used for event ids and idempotency."""
    identity = source_ref or (
        f"{project_id}|{actor_id}|{occurred_at.isoformat()}|{message}"
    )
    message_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return message_key, f"message:{source_ref or message_key}"


def build_health_reported_event(
    *,
    project_id: str,
    actor_id: str,
    occurred_at: datetime,
    source_ref: str,
    event_id: str,
    task_id: str,
    health_status: HealthStatus | str,
    expected_completion: datetime | None,
    blocker: str | None,
    confidence: float,
) -> TaskHealthReportedEvent:
    """Builds one task.health_reported event from a resolved claim."""
    return TaskHealthReportedEvent(
        project_id=project_id,
        event_id=event_id,
        event_type="task.health_reported",
        source=EventSource.MESSAGE,
        source_ref=source_ref,
        actor_id=actor_id,
        occurred_at=occurred_at,
        received_at=occurred_at,
        payload=TaskHealthReportedPayload(
            task_id=task_id,
            health_status=health_status,
            expected_completion=expected_completion,
            blocker=blocker,
            confidence=confidence,
        ),
    )


def build_commitment_made_event(
    *,
    project_id: str,
    actor_id: str,
    occurred_at: datetime,
    source_ref: str,
    event_id: str,
    commitment_id: str,
    target_id: str,
    predicate: str = "workflow_status",
    expected_value: str = "done",
    due_at: datetime | None,
) -> CommitmentMadeEvent:
    """Builds one commitment.made event from a resolved commitment."""
    return CommitmentMadeEvent(
        project_id=project_id,
        event_id=event_id,
        event_type="commitment.made",
        source=EventSource.MESSAGE,
        source_ref=source_ref,
        actor_id=actor_id,
        occurred_at=occurred_at,
        received_at=occurred_at,
        payload=CommitmentMadePayload(
            commitment_id=commitment_id,
            target_type="task",
            target_id=target_id,
            predicate=predicate,
            expected_value=expected_value,
            due_at=due_at,
        ),
    )

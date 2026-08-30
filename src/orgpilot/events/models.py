"""Versioned, immutable organization event contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from orgpilot.domain.enums import HealthStatus, WorkflowStatus


class EventSource(StrEnum):
    MESSAGE = "message"
    TASK = "task"
    CALENDAR = "calendar"
    DOCUMENT = "document"
    HUMAN = "human"
    SCENARIO = "scenario"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include an explicit timezone")
    return value


class EventBase(FrozenModel):
    schema_version: Literal[1] = 1
    project_id: str
    event_id: str
    event_type: str
    source: EventSource
    source_ref: str
    actor_id: str | None = None
    occurred_at: datetime
    received_at: datetime

    _timezone_required = field_validator("occurred_at", "received_at")(_require_timezone)


class MemberRegisteredPayload(FrozenModel):
    member_id: str
    display_name: str
    role: str


class TaskCreatedPayload(FrozenModel):
    task_id: str
    title: str
    owner_id: str
    workflow_status: WorkflowStatus = WorkflowStatus.TODO
    deadline: datetime | None = None
    dependencies: tuple[str, ...] = ()

    _deadline_timezone_required = field_validator("deadline")(
        lambda value: _require_timezone(value) if value is not None else value
    )


class TaskWorkflowChangedPayload(FrozenModel):
    task_id: str
    from_status: WorkflowStatus | None = None
    to_status: WorkflowStatus


class TaskUpdatedPayload(FrozenModel):
    task_id: str
    deadline: datetime | None = None
    title: str | None = None
    owner_id: str | None = None

    _deadline_timezone_required = field_validator("deadline")(
        lambda value: _require_timezone(value) if value is not None else value
    )


class TaskHealthReportedPayload(FrozenModel):
    task_id: str
    health_status: HealthStatus
    expected_completion: datetime | None = None
    blocker: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)

    _completion_timezone_required = field_validator("expected_completion")(
        lambda value: _require_timezone(value) if value is not None else value
    )


class CommitmentMadePayload(FrozenModel):
    commitment_id: str
    target_type: Literal["task"]
    target_id: str
    predicate: Literal["workflow_status", "health_status"]
    expected_value: str
    due_at: datetime | None = None

    _due_timezone_required = field_validator("due_at")(
        lambda value: _require_timezone(value) if value is not None else value
    )


class CommitmentSupersededPayload(FrozenModel):
    commitment_id: str
    reason: str
    replacement_commitment_id: str | None = None


class DirectiveIssuedPayload(FrozenModel):
    directive_id: str
    text: str
    issuer_id: str
    target_id: str
    task_id: str | None = None
    deadline: datetime | None = None

    _deadline_timezone_required = field_validator("deadline")(
        lambda value: _require_timezone(value) if value is not None else value
    )


class DirectiveAcknowledgedPayload(FrozenModel):
    directive_id: str
    ack_by: str
    response_text: str | None = None


class DirectiveCompletedPayload(FrozenModel):
    directive_id: str
    completed_by: str
    note: str | None = None


class DirectiveRemindedPayload(FrozenModel):
    directive_id: str
    reminded_by: str
    reminder_index: int


class DirectiveEscalatedPayload(FrozenModel):
    directive_id: str
    reason: str


class DirectiveDeliveredPayload(FrozenModel):
    directive_id: str
    command_id: str
    target_id: str


class DirectiveDeliveryFailedPayload(FrozenModel):
    directive_id: str
    command_id: str
    target_id: str
    error: str
    attempts: int


class DirectiveClarificationRequestedPayload(FrozenModel):
    clarification_id: str
    issuer_id: str
    draft_text: str
    missing_slots: tuple[str, ...]
    targets: tuple[str, ...] = ()
    task_id: str | None = None
    time_expr: str | None = None


class DirectiveClarificationResolvedPayload(FrozenModel):
    clarification_id: str
    # Empty when the issuer cancelled the clarification outright.
    directive_id: str = ""


class MemberRegisteredEvent(EventBase):
    event_type: Literal["member.registered"]
    payload: MemberRegisteredPayload


class TaskCreatedEvent(EventBase):
    event_type: Literal["task.created"]
    payload: TaskCreatedPayload


class TaskWorkflowChangedEvent(EventBase):
    event_type: Literal["task.workflow_changed"]
    payload: TaskWorkflowChangedPayload


class TaskUpdatedEvent(EventBase):
    event_type: Literal["task.updated"]
    payload: TaskUpdatedPayload


class TaskHealthReportedEvent(EventBase):
    event_type: Literal["task.health_reported"]
    payload: TaskHealthReportedPayload


class CommitmentMadeEvent(EventBase):
    event_type: Literal["commitment.made"]
    payload: CommitmentMadePayload


class CommitmentSupersededEvent(EventBase):
    event_type: Literal["commitment.superseded"]
    payload: CommitmentSupersededPayload


class DirectiveIssuedEvent(EventBase):
    event_type: Literal["directive.issued"]
    payload: DirectiveIssuedPayload


class DirectiveAcknowledgedEvent(EventBase):
    event_type: Literal["directive.acknowledged"]
    payload: DirectiveAcknowledgedPayload


class DirectiveCompletedEvent(EventBase):
    event_type: Literal["directive.completed"]
    payload: DirectiveCompletedPayload


class DirectiveRemindedEvent(EventBase):
    event_type: Literal["directive.reminded"]
    payload: DirectiveRemindedPayload


class DirectiveEscalatedEvent(EventBase):
    event_type: Literal["directive.escalated"]
    payload: DirectiveEscalatedPayload


class DirectiveDeliveredEvent(EventBase):
    event_type: Literal["directive.delivered"]
    payload: DirectiveDeliveredPayload


class DirectiveDeliveryFailedEvent(EventBase):
    event_type: Literal["directive.delivery_failed"]
    payload: DirectiveDeliveryFailedPayload


class DirectiveClarificationRequestedEvent(EventBase):
    event_type: Literal["directive.clarification_requested"]
    payload: DirectiveClarificationRequestedPayload


class DirectiveClarificationResolvedEvent(EventBase):
    event_type: Literal["directive.clarification_resolved"]
    payload: DirectiveClarificationResolvedPayload


type OrgEvent = Annotated[
    MemberRegisteredEvent
    | TaskCreatedEvent
    | TaskWorkflowChangedEvent
    | TaskUpdatedEvent
    | TaskHealthReportedEvent
    | CommitmentMadeEvent
    | CommitmentSupersededEvent
    | DirectiveIssuedEvent
    | DirectiveAcknowledgedEvent
    | DirectiveCompletedEvent
    | DirectiveRemindedEvent
    | DirectiveEscalatedEvent
    | DirectiveDeliveredEvent
    | DirectiveDeliveryFailedEvent
    | DirectiveClarificationRequestedEvent
    | DirectiveClarificationResolvedEvent,
    Field(discriminator="event_type"),
]

ORG_EVENT_ADAPTER = TypeAdapter(OrgEvent)


def parse_event(data: object) -> OrgEvent:
    """Validate untrusted input against the versioned event union."""

    return ORG_EVENT_ADAPTER.validate_python(data)

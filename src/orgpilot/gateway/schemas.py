"""Pydantic API request and response schemas for FastAPI gateway."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EventIngestRequest(StrictSchema):
    events: list[dict[str, Any]] = Field(
        description="One or more raw OrgEvent dictionaries to ingest"
    )


class EventIngestResponse(StrictSchema):
    appended: int
    duplicates: int
    total_events: int


class MessageIngestRequest(StrictSchema):
    message: str = Field(description="Natural language chat message text")
    actor_id: str = Field(description="ID of the member who authored the message")
    message_id: str | None = Field(
        default=None,
        description="Stable upstream message identifier used for idempotency",
    )
    occurred_at: datetime | None = Field(
        default=None, description="Message timestamp; defaults to now UTC if omitted"
    )
    auto_run_turn: bool = Field(
        default=True, description="Whether to immediately trigger an Agent coordination turn"
    )


class MessageIngestResponse(StrictSchema):
    is_actionable: bool
    extracted_events_count: int
    extracted_events: list[dict[str, Any]]
    turn_termination_reason: str | None = None
    turn_round_number: int | None = None


class ApprovalDecisionRequest(StrictSchema):
    decision: Literal["approved", "rejected"]
    approver_id: str
    reason: str | None = None


class ApprovalDecisionResponse(StrictSchema):
    approval_id: str
    decision: str
    status: str
    turn_termination_reason: str | None = None


class TurnRunRequest(StrictSchema):
    current_time: datetime | None = None


class TurnRunResponse(StrictSchema):
    round_number: int
    termination_reason: str
    active_cases_count: int
    executed_commands: list[str]


class ProjectStateResponse(StrictSchema):
    project_id: str
    tasks: dict[str, Any]
    members: dict[str, Any]
    active_cases: list[dict[str, Any]]
    pending_approvals: list[dict[str, Any]]


class DagNode(StrictSchema):
    task_id: str
    title: str
    owner_id: str
    workflow_status: str
    health_status: str
    deadline: datetime | None = None
    layer: int = 0
    in_degree: int = 0
    out_degree: int = 0
    is_at_risk: bool = False
    is_delayed: bool = False
    is_critical_path: bool = False
    blockers: list[str] = Field(default_factory=list)
    expected_completion: datetime | None = None


class DagEdge(StrictSchema):
    from_task: str
    to_task: str
    is_impacted: bool = False
    is_critical: bool = False


class DagSummary(StrictSchema):
    total_tasks: int
    on_track_count: int
    at_risk_count: int
    delayed_count: int
    completed_count: int
    critical_path: list[str]
    impacted_tasks: list[str]


class DagResponse(StrictSchema):
    project_id: str
    nodes: list[DagNode]
    edges: list[DagEdge]
    summary: DagSummary


class TimelineEntry(StrictSchema):
    entry_id: str
    timestamp: datetime
    category: Literal["event", "case", "approval", "action"]
    title: str
    description: str
    status: str | None = None
    task_id: str | None = None
    actor_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TimelineResponse(StrictSchema):
    project_id: str
    total_entries: int
    entries: list[TimelineEntry]

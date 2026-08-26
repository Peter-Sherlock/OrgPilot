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

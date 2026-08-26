"""Domain state produced by replaying organization events."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from orgpilot.domain.enums import (
    ActionType,
    ClaimStatus,
    CommitmentStatus,
    CoordinationCaseStatus,
    HealthStatus,
    PolicyDisposition,
    RiskLevel,
    WorkflowStatus,
)


class StrictModel(BaseModel):
    """Mutable validated state model with no silent extra fields."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MemberState(StrictModel):
    member_id: str
    display_name: str
    role: str
    source_event_id: str
    last_update_at: datetime


class TaskState(StrictModel):
    task_id: str
    title: str
    owner_id: str
    workflow_status: WorkflowStatus
    health_status: HealthStatus = HealthStatus.UNKNOWN
    health_conflict: bool = False
    deadline: datetime | None = None
    dependencies: tuple[str, ...] = ()
    health_claim_ids: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    last_update_at: datetime


class TaskHealthClaim(StrictModel):
    claim_id: str
    task_id: str
    stated_by: str | None
    health_status: HealthStatus
    expected_completion: datetime | None = None
    blocker: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source_event_id: str
    source_ref: str
    occurred_at: datetime
    status: ClaimStatus = ClaimStatus.ACTIVE
    superseded_by: str | None = None


class Commitment(StrictModel):
    commitment_id: str
    actor_id: str
    target_type: str
    target_id: str
    predicate: str
    expected_value: str
    due_at: datetime | None = None
    source_event_id: str
    status: CommitmentStatus = CommitmentStatus.ACTIVE
    superseded_by: str | None = None
    last_update_at: datetime


class DependencyImpact(StrictModel):
    source_task_id: str
    impacted_task_id: str
    path: tuple[str, ...]


class CoordinationAction(StrictModel):
    action_id: str
    action_type: ActionType
    targets: tuple[str, ...]
    reason_refs: tuple[str, ...]
    expected_effect: str


class PolicyDecision(StrictModel):
    action_id: str
    disposition: PolicyDisposition
    risk_level: RiskLevel
    requires_approval: bool
    reason: str


class CoordinationCase(StrictModel):
    case_id: str
    source_task_id: str
    status: CoordinationCaseStatus
    evidence_claim_ids: tuple[str, ...]
    impacted_task_ids: tuple[str, ...]
    missing_information: tuple[str, ...]
    candidate_actions: tuple[CoordinationAction, ...]


class OrgState(StrictModel):
    project_id: str
    members: dict[str, MemberState] = Field(default_factory=dict)
    tasks: dict[str, TaskState] = Field(default_factory=dict)
    health_claims: dict[str, TaskHealthClaim] = Field(default_factory=dict)
    commitments: dict[str, Commitment] = Field(default_factory=dict)
    processed_event_ids: set[str] = Field(default_factory=set)
    last_event_id: str | None = None

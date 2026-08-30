"""Domain state produced by replaying organization events and managing coordination cases."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orgpilot.domain.enums import (
    ActionType,
    AgentTerminationReason,
    ApprovalStatus,
    ClaimStatus,
    CommandStatus,
    CommitmentStatus,
    CoordinationCaseStatus,
    DirectiveStatus,
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


class DirectiveState(StrictModel):
    """A relayed work directive from an issuer (usually PM) to a target member."""

    directive_id: str
    text: str
    issuer_id: str
    target_id: str
    task_id: str | None = None
    deadline: datetime | None = None
    status: DirectiveStatus = DirectiveStatus.ISSUED
    # Transport-level delivery ledger: pending until an adapter confirms relay.
    delivery_status: str = "pending"
    issued_at: datetime
    acknowledged_at: datetime | None = None
    completed_at: datetime | None = None
    reminder_count: int = 0
    escalated: bool = False
    source_event_ids: tuple[str, ...] = ()
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
    payload: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(StrictModel):
    action_id: str
    disposition: PolicyDisposition
    risk_level: RiskLevel
    requires_approval: bool
    reason: str


class ActionCommand(StrictModel):
    """Concrete command verified by policy and ready for adapter execution."""

    command_id: str
    action_id: str
    action_type: ActionType
    targets: tuple[str, ...]
    reason_refs: tuple[str, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)
    approved_by: str | None = None
    idempotency_key: str
    created_at: datetime


class ActionResult(StrictModel):
    """Execution result returned by collaboration adapter."""

    command_id: str
    action_id: str
    status: CommandStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    executed_at: datetime


class ApprovalRequest(StrictModel):
    """Human approval lifecycle model."""

    approval_id: str
    case_id: str
    action_id: str
    action_type: ActionType
    approver_id: str
    proposed_command: ActionCommand
    status: ApprovalStatus = ApprovalStatus.PENDING
    rejection_reason: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    consumed: bool = False
    consumed_at: datetime | None = None


class CoordinationCase(StrictModel):
    """Persistent case tracking organization risks and coordination rounds."""

    case_id: str
    source_task_id: str
    status: CoordinationCaseStatus
    evidence_claim_ids: tuple[str, ...] = ()
    impacted_task_ids: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    candidate_actions: tuple[CoordinationAction, ...] = ()
    executed_commands: tuple[ActionCommand, ...] = ()
    waiting_for: str | None = None
    waiting_until: datetime | None = None
    round_count: int = 0
    terminal_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentTurnTrace(StrictModel):
    """Execution record for a single turn of the agent loop."""

    round_number: int
    occurred_at: datetime
    ingested_event_ids: tuple[str, ...]
    active_case_ids: tuple[str, ...]
    candidate_action_ids: tuple[str, ...]
    policy_decision_ids: tuple[str, ...]
    executed_command_ids: tuple[str, ...]
    generated_event_ids: tuple[str, ...]
    termination_reason: AgentTerminationReason | None = None


class AgentExecutionTrace(StrictModel):
    """Complete multi-turn deterministic trace for a scenario execution."""

    scenario_id: str
    turns: tuple[AgentTurnTrace, ...]
    final_termination_reason: AgentTerminationReason
    final_cases: tuple[CoordinationCase, ...]


class OrgState(StrictModel):
    project_id: str
    members: dict[str, MemberState] = Field(default_factory=dict)
    tasks: dict[str, TaskState] = Field(default_factory=dict)
    health_claims: dict[str, TaskHealthClaim] = Field(default_factory=dict)
    commitments: dict[str, Commitment] = Field(default_factory=dict)
    directives: dict[str, DirectiveState] = Field(default_factory=dict)
    processed_event_ids: set[str] = Field(default_factory=set)
    last_event_id: str | None = None

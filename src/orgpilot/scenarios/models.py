"""Validated scenario contracts, interactive rounds, and replay results."""

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orgpilot.domain.enums import (
    ActionType,
    AgentTerminationReason,
    ApprovalStatus,
    ClaimStatus,
    CommitmentStatus,
    CoordinationCaseStatus,
    HealthStatus,
    PolicyDisposition,
    WorkflowStatus,
)
from orgpilot.domain.models import (
    AgentExecutionTrace,
    CoordinationCase,
    DependencyImpact,
    OrgState,
    PolicyDecision,
)
from orgpilot.events.models import OrgEvent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedTaskState(StrictModel):
    workflow_status: WorkflowStatus
    health_status: HealthStatus
    health_conflict: bool = False
    deadline: datetime | None = None


class ExpectedImpact(StrictModel):
    source_task_id: str
    impacted_task_id: str
    path: tuple[str, ...]


class ExpectedCase(StrictModel):
    source_task_id: str
    status: CoordinationCaseStatus = CoordinationCaseStatus.OPEN
    impacted_task_ids: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    action_types: tuple[ActionType, ...] = ()
    waiting_for: str | None = None
    terminal_reason: str | None = None


class ExpectedAction(StrictModel):
    action_type: ActionType
    targets: tuple[str, ...]
    disposition: PolicyDisposition
    requires_approval: bool


class ExpectedClaim(StrictModel):
    status: ClaimStatus
    health_status: HealthStatus


class ExpectedCommitment(StrictModel):
    status: CommitmentStatus


class GroundTruth(StrictModel):
    event_count: int = Field(ge=0)
    member_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    tasks: dict[str, ExpectedTaskState] = Field(default_factory=dict)
    impacts: tuple[ExpectedImpact, ...] = ()
    open_cases: tuple[ExpectedCase, ...] = ()
    expected_actions: tuple[ExpectedAction, ...] = ()
    claims: dict[str, ExpectedClaim] = Field(default_factory=dict)
    commitments: dict[str, ExpectedCommitment] = Field(default_factory=dict)


class ScenarioApprovalAction(StrictModel):
    approver_id: str
    decision: ApprovalStatus
    reason: str | None = None
    approval_id: str | None = None


class ScenarioRound(StrictModel):
    round_number: int
    current_time: datetime
    events: tuple[OrgEvent, ...] = ()
    approvals: tuple[ScenarioApprovalAction, ...] = ()
    expected_termination: AgentTerminationReason | None = None
    expected_case_statuses: dict[str, CoordinationCaseStatus] = Field(default_factory=dict)


class InteractiveGroundTruth(StrictModel):
    total_rounds: int
    final_termination_reason: AgentTerminationReason
    final_cases: dict[str, CoordinationCaseStatus] = Field(default_factory=dict)
    case_terminal_reasons: dict[str, str] = Field(default_factory=dict)
    executed_command_types: tuple[ActionType, ...] = ()
    tasks: dict[str, ExpectedTaskState] = Field(default_factory=dict)


class ScenarioDefinition(StrictModel):
    schema_version: int
    scenario_id: str
    title: str
    description: str
    project_id: str
    source_path: Path
    events: tuple[OrgEvent, ...] = ()
    rounds: tuple[ScenarioRound, ...] = ()
    ground_truth: GroundTruth | None = None
    interactive_ground_truth: InteractiveGroundTruth | None = None


class ReplayResult(StrictModel):
    scenario_id: str
    state: OrgState
    impacts: tuple[DependencyImpact, ...]
    cases: tuple[CoordinationCase, ...]
    policy_decisions: tuple[PolicyDecision, ...]
    event_count: int
    duplicate_event_count: int
    agent_trace: AgentExecutionTrace | None = None


class AssertionResult(StrictModel):
    name: str
    passed: bool
    expected: Any
    actual: Any


class GroundTruthReport(StrictModel):
    scenario_id: str
    passed: bool
    assertions: tuple[AssertionResult, ...]

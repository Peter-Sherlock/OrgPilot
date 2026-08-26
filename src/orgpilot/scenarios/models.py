"""Validated scenario contracts and replay results."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orgpilot.domain.enums import (
    ActionType,
    ClaimStatus,
    CommitmentStatus,
    HealthStatus,
    PolicyDisposition,
    WorkflowStatus,
)
from orgpilot.domain.models import (
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


class ExpectedImpact(StrictModel):
    source_task_id: str
    impacted_task_id: str
    path: tuple[str, ...]


class ExpectedCase(StrictModel):
    source_task_id: str
    impacted_task_ids: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    action_types: tuple[ActionType, ...] = ()


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


class ScenarioDefinition(StrictModel):
    schema_version: int
    scenario_id: str
    title: str
    description: str
    project_id: str
    source_path: Path
    events: tuple[OrgEvent, ...]
    ground_truth: GroundTruth


class ReplayResult(StrictModel):
    scenario_id: str
    state: OrgState
    impacts: tuple[DependencyImpact, ...]
    cases: tuple[CoordinationCase, ...]
    policy_decisions: tuple[PolicyDecision, ...]
    event_count: int
    duplicate_event_count: int


class AssertionResult(StrictModel):
    name: str
    passed: bool
    expected: Any
    actual: Any


class GroundTruthReport(StrictModel):
    scenario_id: str
    passed: bool
    assertions: tuple[AssertionResult, ...]

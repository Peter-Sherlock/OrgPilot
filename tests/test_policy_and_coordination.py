"""Planner candidates and policy decisions remain independent."""

from datetime import datetime

import pytest

from orgpilot.coordination import CoordinationService
from orgpilot.domain.enums import (
    ActionType,
    ClaimStatus,
    HealthStatus,
    PolicyDisposition,
    RiskLevel,
    WorkflowStatus,
)
from orgpilot.domain.models import (
    CoordinationAction,
    DependencyImpact,
    MemberState,
    OrgState,
    TaskHealthClaim,
    TaskState,
)
from orgpilot.policy import PolicyEngine

NOW = datetime.fromisoformat("2026-09-01T09:00:00+08:00")


def _action(action_type: ActionType) -> CoordinationAction:
    return CoordinationAction(
        action_id=f"action:{action_type}",
        action_type=action_type,
        targets=("alice",),
        reason_refs=("evt-risk",),
        expected_effect="test",
    )


@pytest.mark.parametrize(
    ("action_type", "disposition", "risk", "approval"),
    [
        (ActionType.ASK_RECOVERY_ESTIMATE, PolicyDisposition.ALLOW, RiskLevel.LOW, False),
        (ActionType.ASK_CLARIFICATION, PolicyDisposition.ALLOW, RiskLevel.LOW, False),
        (ActionType.PROPOSE_RESCHEDULE, PolicyDisposition.ALLOW, RiskLevel.MEDIUM, False),
        (
            ActionType.NOTIFY_GROUP,
            PolicyDisposition.REQUIRE_APPROVAL,
            RiskLevel.HIGH,
            True,
        ),
        (
            ActionType.UPDATE_TASK,
            PolicyDisposition.REQUIRE_APPROVAL,
            RiskLevel.HIGH,
            True,
        ),
    ],
)
def test_policy_classification(
    action_type: ActionType,
    disposition: PolicyDisposition,
    risk: RiskLevel,
    approval: bool,
) -> None:
    decision = PolicyEngine().evaluate(_action(action_type))
    assert decision.disposition is disposition
    assert decision.risk_level is risk
    assert decision.requires_approval is approval


def test_coordination_uses_owner_when_claim_has_no_actor() -> None:
    state = OrgState(
        project_id="test-project",
        members={
            "alice": MemberState(
                member_id="alice",
                display_name="Alice",
                role="backend",
                source_event_id="evt-member",
                last_update_at=NOW,
            )
        },
        tasks={
            "api": TaskState(
                task_id="api",
                title="API",
                owner_id="alice",
                workflow_status=WorkflowStatus.DOING,
                health_status=HealthStatus.AT_RISK,
                source_event_ids=("evt-task", "evt-risk"),
                health_claim_ids=("claim-risk",),
                last_update_at=NOW,
            )
        },
        health_claims={
            "claim-risk": TaskHealthClaim(
                claim_id="claim-risk",
                task_id="api",
                stated_by=None,
                health_status=HealthStatus.AT_RISK,
                confidence=0.8,
                source_event_id="evt-risk",
                source_ref="task-system:api",
                occurred_at=NOW,
            )
        },
    )

    case = CoordinationService().build_cases(state, ())[0]
    assert case.candidate_actions[0].targets == ("alice",)


def test_expected_completion_prevents_redundant_question() -> None:
    state = OrgState(
        project_id="test-project",
        tasks={
            "api": TaskState(
                task_id="api",
                title="API",
                owner_id="alice",
                workflow_status=WorkflowStatus.DOING,
                health_status=HealthStatus.AT_RISK,
                source_event_ids=("evt-task",),
                last_update_at=NOW,
            )
        },
        health_claims={
            "claim-risk": TaskHealthClaim(
                claim_id="claim-risk",
                task_id="api",
                stated_by="alice",
                health_status=HealthStatus.AT_RISK,
                expected_completion=NOW,
                confidence=0.8,
                source_event_id="evt-risk",
                source_ref="message:1",
                occurred_at=NOW,
            )
        },
    )
    impacts = (
        DependencyImpact(source_task_id="api", impacted_task_id="client", path=("api", "client")),
    )

    case = CoordinationService().build_cases(state, impacts)[0]
    assert case.missing_information == ()
    assert case.candidate_actions == ()


def test_superseded_claim_is_not_evidence() -> None:
    state = OrgState(
        project_id="test-project",
        tasks={
            "api": TaskState(
                task_id="api",
                title="API",
                owner_id="alice",
                workflow_status=WorkflowStatus.BLOCKED,
                health_status=HealthStatus.UNKNOWN,
                source_event_ids=("evt-task",),
                last_update_at=NOW,
            )
        },
        health_claims={
            "old": TaskHealthClaim(
                claim_id="old",
                task_id="api",
                stated_by="alice",
                health_status=HealthStatus.AT_RISK,
                confidence=0.8,
                source_event_id="evt-risk",
                source_ref="message:1",
                occurred_at=NOW,
                status=ClaimStatus.SUPERSEDED,
                superseded_by="new",
            )
        },
    )

    case = CoordinationService().build_cases(state, ())[0]
    assert case.evidence_claim_ids == ()
    assert case.candidate_actions == ()

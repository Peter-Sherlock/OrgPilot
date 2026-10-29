"""Per-round checkpoints, scripted approvals and commitments in interactive scenarios.

These pin the evaluator's own semantics on a two-member, one-task project, so
they keep holding when the lifecycle scenario's kernel defects get fixed.
"""

from datetime import datetime
from pathlib import Path

from orgpilot.domain.enums import (
    AgentTerminationReason,
    ApprovalStatus,
    CommitmentStatus,
    CoordinationCaseStatus,
    WorkflowStatus,
)
from orgpilot.scenarios.evaluator import evaluate_scenario
from orgpilot.scenarios.models import (
    ExpectedCommitment,
    ExpectedTaskState,
    InteractiveGroundTruth,
    ScenarioApprovalAction,
    ScenarioDefinition,
    ScenarioRound,
)
from orgpilot.scenarios.runner import ScenarioRunner
from tests.conftest import make_event

CASE = "case:task-health:api"
NOW = datetime.fromisoformat("2026-09-01T10:00:00+08:00")


def _scenario(
    *,
    approvals: tuple[ScenarioApprovalAction, ...] = (),
    waiting_for: str | None = "alice",
) -> ScenarioDefinition:
    events = (
        make_event(
            "m-alice",
            "member.registered",
            {"member_id": "alice", "display_name": "Alice", "role": "backend"},
            minute=0,
        ),
        make_event(
            "m-carol",
            "member.registered",
            {"member_id": "carol", "display_name": "Carol", "role": "pm"},
            minute=1,
        ),
        make_event(
            "t-api",
            "task.created",
            {
                "task_id": "api",
                "title": "API",
                "owner_id": "alice",
                "workflow_status": "doing",
                "deadline": "2026-09-05T18:00:00+08:00",
                "dependencies": [],
            },
            minute=2,
        ),
        make_event(
            "c-api",
            "commitment.made",
            {
                "commitment_id": "c_api_done",
                "target_type": "task",
                "target_id": "api",
                "predicate": "workflow_status",
                "expected_value": "done",
                "due_at": "2026-09-05T18:00:00+08:00",
            },
            actor_id="alice",
            minute=3,
        ),
        # A risk without an estimate: the agent should ask the owner and wait.
        make_event(
            "h-api",
            "task.health_reported",
            {"task_id": "api", "health_status": "at_risk", "confidence": 0.8},
            actor_id="alice",
            minute=4,
        ),
    )
    return ScenarioDefinition(
        schema_version=1,
        scenario_id="round_checks",
        title="Round checks",
        description="Per-round checkpoints on a one-task project.",
        project_id="test-project",
        source_path=Path("round_checks.yaml"),
        rounds=(
            ScenarioRound(
                round_number=1,
                current_time=NOW,
                events=events,
                approvals=approvals,
                expected_termination=AgentTerminationReason.WAITING_RESPONSE,
                expected_case_statuses={CASE: CoordinationCaseStatus.WAITING_FOR_RESPONSE},
                expected_waiting_for={CASE: waiting_for},
            ),
        ),
        interactive_ground_truth=InteractiveGroundTruth(
            total_rounds=1,
            final_termination_reason=AgentTerminationReason.WAITING_RESPONSE,
            # health_status is left unset, so it is not asserted.
            tasks={"api": ExpectedTaskState(workflow_status=WorkflowStatus.DOING)},
            commitments={"c_api_done": ExpectedCommitment(status=CommitmentStatus.ACTIVE)},
        ),
    )


def _assertions(scenario: ScenarioDefinition) -> dict[str, tuple[bool, object]]:
    report = evaluate_scenario(scenario, ScenarioRunner().run(scenario))
    return {item.name: (item.passed, item.actual) for item in report.assertions}


def test_round_checkpoints_compare_against_the_snapshot_after_that_round() -> None:
    results = _assertions(_scenario())

    assert results["round_1:termination"] == (True, AgentTerminationReason.WAITING_RESPONSE)
    assert results[f"round_1:case_status:{CASE}"][0] is True
    assert results[f"round_1:waiting_for:{CASE}"] == (True, "alice")
    assert results["task:api"][0] is True
    assert results["commitment:c_api_done"][0] is True


def test_waiting_for_the_wrong_member_is_a_failed_check() -> None:
    results = _assertions(_scenario(waiting_for="carol"))

    assert results[f"round_1:waiting_for:{CASE}"] == (False, "alice")


def test_scripted_approval_with_nothing_pending_fails_instead_of_passing_silently() -> None:
    approval = ScenarioApprovalAction(approver_id="carol", decision=ApprovalStatus.APPROVED)

    results = _assertions(_scenario(approvals=(approval,)))

    assert results["round_1:approval_applied:carol:approved"] == (False, False)


def test_round_snapshots_record_the_state_after_each_turn() -> None:
    result = ScenarioRunner().run(_scenario())

    (snapshot,) = result.round_snapshots
    assert snapshot.round_number == 1
    assert snapshot.case_statuses == {CASE: CoordinationCaseStatus.WAITING_FOR_RESPONSE}
    assert snapshot.case_waiting_for == {CASE: "alice"}
    assert snapshot.unmatched_approvals == ()

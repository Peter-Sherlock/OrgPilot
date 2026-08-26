"""Tests for CaseLedger lifecycle management, timeouts, and state reconciliation."""

from datetime import datetime, timedelta

import pytest

from orgpilot.coordination.ledger import CaseLedger
from orgpilot.domain.enums import (
    ActionType,
    CoordinationCaseStatus,
    HealthStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import (
    ActionCommand,
    CoordinationAction,
    OrgState,
    TaskHealthClaim,
    TaskState,
)

NOW = datetime.fromisoformat("2026-09-01T10:00:00+08:00")


def _task(task_id: str, workflow: WorkflowStatus, health: HealthStatus) -> TaskState:
    return TaskState(
        task_id=task_id,
        title=f"Task {task_id}",
        owner_id="alice",
        workflow_status=workflow,
        health_status=health,
        source_event_ids=("evt-1",),
        last_update_at=NOW,
    )


def test_ledger_initial_and_active_queries() -> None:
    ledger = CaseLedger()
    state = OrgState(
        project_id="p1",
        tasks={
            "t1": _task("t1", WorkflowStatus.DOING, HealthStatus.DELAYED),
            "t2": _task("t2", WorkflowStatus.DOING, HealthStatus.ON_TRACK),
        },
    )
    cases = ledger.reconcile(state, (), NOW)
    assert len(cases) == 1
    assert cases[0].case_id == "case:task-health:t1"
    assert cases[0].status is CoordinationCaseStatus.OPEN
    assert len(ledger.get_active_cases()) == 1


def test_ledger_reconcile_cancels_when_task_recovers_while_waiting() -> None:
    ledger = CaseLedger()
    state = OrgState(
        project_id="p1",
        tasks={"t1": _task("t1", WorkflowStatus.DOING, HealthStatus.DELAYED)},
    )
    ledger.reconcile(state, (), NOW)
    ledger.transition(
        "case:task-health:t1",
        CoordinationCaseStatus.WAITING_FOR_RESPONSE,
        NOW,
        waiting_for="alice",
        waiting_until=NOW + timedelta(hours=2),
    )

    # Task recovers
    recovered_state = OrgState(
        project_id="p1",
        tasks={"t1": _task("t1", WorkflowStatus.DOING, HealthStatus.ON_TRACK)},
    )
    ledger.reconcile(recovered_state, (), NOW + timedelta(minutes=30))
    case = ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.CANCELLED
    assert case.terminal_reason == "task recovered while waiting"
    assert len(ledger.get_active_cases()) == 0


def test_ledger_reconcile_resolves_when_estimate_received() -> None:
    ledger = CaseLedger()
    state = OrgState(
        project_id="p1",
        tasks={"t1": _task("t1", WorkflowStatus.DOING, HealthStatus.DELAYED)},
        health_claims={
            "c1": TaskHealthClaim(
                claim_id="c1",
                task_id="t1",
                stated_by="alice",
                health_status=HealthStatus.DELAYED,
                expected_completion=None,
                confidence=0.9,
                source_event_id="evt-1",
                source_ref="ref",
                occurred_at=NOW,
            )
        },
    )
    ledger.reconcile(state, (), NOW)
    ledger.transition(
        "case:task-health:t1",
        CoordinationCaseStatus.WAITING_FOR_RESPONSE,
        NOW,
        waiting_for="alice",
    )

    # Estimate provided
    updated_state = OrgState(
        project_id="p1",
        tasks={"t1": _task("t1", WorkflowStatus.DOING, HealthStatus.DELAYED)},
        health_claims={
            "c2": TaskHealthClaim(
                claim_id="c2",
                task_id="t1",
                stated_by="alice",
                health_status=HealthStatus.DELAYED,
                expected_completion=NOW + timedelta(days=1),
                confidence=0.9,
                source_event_id="evt-2",
                source_ref="ref",
                occurred_at=NOW + timedelta(hours=1),
            )
        },
    )
    ledger.reconcile(updated_state, (), NOW + timedelta(hours=1))
    case = ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.RESOLVED
    assert case.terminal_reason == "recovery estimate received"


def test_ledger_timeout_escalation() -> None:
    ledger = CaseLedger()
    state = OrgState(
        project_id="p1",
        tasks={"t1": _task("t1", WorkflowStatus.DOING, HealthStatus.DELAYED)},
    )
    ledger.reconcile(state, (), NOW)
    ledger.transition(
        "case:task-health:t1",
        CoordinationCaseStatus.WAITING_FOR_RESPONSE,
        NOW,
        waiting_for="alice",
        waiting_until=NOW + timedelta(hours=2),
    )

    # Time advances past deadline
    ledger.reconcile(state, (), NOW + timedelta(hours=3))
    case = ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.ESCALATED
    assert "timeout" in (case.terminal_reason or "")


def test_ledger_duplicate_action_detection() -> None:
    ledger = CaseLedger()
    state = OrgState(
        project_id="p1",
        tasks={"t1": _task("t1", WorkflowStatus.DOING, HealthStatus.DELAYED)},
    )
    ledger.reconcile(state, (), NOW)
    action = CoordinationAction(
        action_id="a1",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("alice",),
        reason_refs=("evt-1",),
        expected_effect="ask",
    )
    assert not ledger.is_action_duplicate("case:task-health:t1", action)

    cmd = ActionCommand(
        command_id="cmd-1",
        action_id="a1",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("alice",),
        idempotency_key="idem-1",
        created_at=NOW,
    )
    ledger.record_command("case:task-health:t1", cmd, NOW)
    assert ledger.is_action_duplicate("case:task-health:t1", action)


def test_ledger_missing_case_errors() -> None:
    ledger = CaseLedger()
    with pytest.raises(KeyError):
        ledger.transition("missing", CoordinationCaseStatus.OPEN, NOW)
    with pytest.raises(KeyError):
        ledger.record_command(
            "missing",
            ActionCommand(
                command_id="c",
                action_id="a",
                action_type=ActionType.ASK_CLARIFICATION,
                targets=("alice",),
                idempotency_key="idem",
                created_at=NOW,
            ),
            NOW,
        )


def test_ledger_task_deleted_cancels_active_case() -> None:
    ledger = CaseLedger()
    state = OrgState(
        project_id="p1",
        tasks={"t1": _task("t1", WorkflowStatus.DOING, HealthStatus.DELAYED)},
    )
    ledger.reconcile(state, (), NOW)
    assert len(ledger.get_active_cases()) == 1

    # Task is deleted from state
    empty_state = OrgState(project_id="p1", tasks={})
    ledger.reconcile(empty_state, (), NOW + timedelta(minutes=5))
    case = ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.CANCELLED
    assert case.terminal_reason == "task deleted"

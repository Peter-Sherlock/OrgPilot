"""Tests for CaseLedger lifecycle management, timeouts, and state reconciliation."""

from datetime import datetime, timedelta

import pytest

from orgpilot.coordination.ledger import CaseLedger
from orgpilot.coordination.service import CoordinationService
from orgpilot.domain.enums import (
    ActionType,
    CoordinationCaseStatus,
    HealthStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import (
    ActionCommand,
    CoordinationAction,
    MemberState,
    OrgState,
    TaskHealthClaim,
    TaskState,
)

NOW = datetime.fromisoformat("2026-09-01T10:00:00+08:00")


def _task(
    task_id: str,
    workflow: WorkflowStatus,
    health: HealthStatus,
    deadline: datetime | None = None,
) -> TaskState:
    return TaskState(
        task_id=task_id,
        title=f"Task {task_id}",
        owner_id="alice",
        workflow_status=workflow,
        health_status=health,
        deadline=deadline,
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


# D1a: an estimate that arrives while the case waits for it is compared with the
# task deadline before the case may close.
CASE_T1 = "case:task-health:t1"
DEADLINE = NOW + timedelta(days=2)
REPLY_AT = NOW + timedelta(hours=1)
PM = MemberState(
    member_id="carol",
    display_name="Carol",
    role="pm",
    source_event_id="evt-member",
    last_update_at=NOW,
)


def _claim(
    claim_id: str,
    expected_completion: datetime | None,
    *,
    stated_by: str = "alice",
    health: HealthStatus = HealthStatus.DELAYED,
    occurred_at: datetime = REPLY_AT,
) -> TaskHealthClaim:
    return TaskHealthClaim(
        claim_id=claim_id,
        task_id="t1",
        stated_by=stated_by,
        health_status=health,
        expected_completion=expected_completion,
        confidence=0.9,
        source_event_id=f"evt-{claim_id}",
        source_ref="ref",
        occurred_at=occurred_at,
    )


def _state(*claims: TaskHealthClaim) -> OrgState:
    return OrgState(
        project_id="p1",
        members={"carol": PM},
        tasks={"t1": _task("t1", WorkflowStatus.DOING, HealthStatus.DELAYED, DEADLINE)},
        health_claims={claim.claim_id: claim for claim in claims},
    )


def _ledger_waiting_for_estimate() -> CaseLedger:
    """t1 is reported delayed without an estimate, and alice has been asked for one."""
    ledger = CaseLedger()
    ledger.reconcile(_state(_claim("c0", None, occurred_at=NOW)), (), NOW)
    ledger.transition(
        CASE_T1,
        CoordinationCaseStatus.WAITING_FOR_RESPONSE,
        NOW,
        waiting_for="alice",
        waiting_until=NOW + timedelta(hours=2),
    )
    return ledger


def _reschedules(ledger: CaseLedger, state: OrgState) -> tuple[CoordinationAction, ...]:
    case = ledger.get_case(CASE_T1)
    assert case is not None
    actions = CoordinationService().plan_actions_for_case(case, state)
    return tuple(a for a in actions if a.action_type is ActionType.PROPOSE_RESCHEDULE)


def test_estimate_within_deadline_resolves_waiting_case() -> None:
    ledger = _ledger_waiting_for_estimate()

    ledger.reconcile(_state(_claim("c1", DEADLINE - timedelta(hours=6))), (), REPLY_AT)

    case = ledger.get_case(CASE_T1)
    assert case is not None
    assert case.status is CoordinationCaseStatus.RESOLVED
    assert case.terminal_reason == "recovery estimate received"
    assert case.waiting_for is None
    assert case.waiting_until is None
    assert ledger.get_active_cases() == ()


def test_estimate_past_deadline_reopens_case_for_reschedule() -> None:
    ledger = _ledger_waiting_for_estimate()
    late_eta = DEADLINE + timedelta(days=2)
    state = _state(_claim("c1", late_eta))

    ledger.reconcile(state, (), REPLY_AT)

    case = ledger.get_case(CASE_T1)
    assert case is not None
    assert case.status is CoordinationCaseStatus.OPEN
    assert case.terminal_reason is None
    assert case.missing_information == ()
    assert case.waiting_for is None
    assert case.waiting_until is None
    assert case.candidate_actions == ()
    assert case.updated_at == REPLY_AT
    assert ledger.get_active_cases() == (case,)

    # The open case goes down the same path as a first report with a late ETA.
    (proposal,) = _reschedules(ledger, state)
    assert proposal.targets == ("carol",)
    assert proposal.reason_refs == ("evt-c1",)
    assert proposal.payload["new_deadline"] == late_eta.isoformat()


def test_estimate_equal_to_deadline_meets_it() -> None:
    """Boundary: finishing exactly at the deadline is on time, as in the planner."""
    ledger = _ledger_waiting_for_estimate()
    state = _state(_claim("c1", DEADLINE))

    ledger.reconcile(state, (), REPLY_AT)

    case = ledger.get_case(CASE_T1)
    assert case is not None
    assert case.status is CoordinationCaseStatus.RESOLVED
    assert case.terminal_reason == "recovery estimate received"
    assert _reschedules(ledger, state) == ()


LATE = DEADLINE + timedelta(days=1)
ON_TIME = DEADLINE - timedelta(days=1)


@pytest.mark.parametrize(
    ("claims", "stays_open"),
    [
        pytest.param(
            (
                _claim("c1", LATE, stated_by="frank", health=HealthStatus.AT_RISK),
                _claim(
                    "c2",
                    ON_TIME,
                    health=HealthStatus.AT_RISK,
                    occurred_at=REPLY_AT + timedelta(minutes=5),
                ),
            ),
            False,
            id="newer-on-time-estimate-wins",
        ),
        pytest.param(
            (
                _claim("c1", LATE, stated_by="frank", health=HealthStatus.DELAYED),
                _claim(
                    "c2",
                    ON_TIME,
                    health=HealthStatus.AT_RISK,
                    occurred_at=REPLY_AT + timedelta(minutes=5),
                ),
            ),
            True,
            id="more-severe-late-estimate-wins",
        ),
        pytest.param(
            (
                _claim("c1", LATE, stated_by="frank", health=HealthStatus.AT_RISK),
                _claim("c2", None, health=HealthStatus.DELAYED),
            ),
            False,
            id="primary-claim-has-no-estimate",
        ),
    ],
)
def test_deadline_check_reads_the_planners_primary_claim(
    claims: tuple[TaskHealthClaim, ...], stays_open: bool
) -> None:
    """The case stays open exactly when the planner will propose a reschedule.

    With several active claims, both sides read the most severe, then latest one,
    so a reopened case never sits open with nothing planned.
    """
    ledger = _ledger_waiting_for_estimate()
    state = _state(*claims)

    ledger.reconcile(state, (), REPLY_AT)

    case = ledger.get_case(CASE_T1)
    assert case is not None
    assert (case.status is CoordinationCaseStatus.OPEN) is stays_open
    assert bool(_reschedules(ledger, state)) is stays_open


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

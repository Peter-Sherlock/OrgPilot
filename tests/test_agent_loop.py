"""Tests for bounded CoordinationAgent loop and multi-turn execution."""

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.agent.loop import CoordinationAgent
from orgpilot.domain.enums import (
    ActionType,
    AgentTerminationReason,
    CoordinationCaseStatus,
    HealthStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import ActionCommand, CoordinationAction
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    TaskCreatedEvent,
    TaskCreatedPayload,
    TaskHealthReportedEvent,
    TaskHealthReportedPayload,
)
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import MockFeishuClient

NOW = datetime.fromisoformat("2026-09-01T10:00:00+08:00")


def _init_events() -> list:
    return [
        MemberRegisteredEvent(
            project_id="test-proj",
            event_id="evt-mem-1",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=MemberRegisteredPayload(
                member_id="alice", display_name="Alice", role="backend"
            ),
        ),
        MemberRegisteredEvent(
            project_id="test-proj",
            event_id="evt-mem-2",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=MemberRegisteredPayload(member_id="carol", display_name="Carol", role="pm"),
        ),
        TaskCreatedEvent(
            project_id="test-proj",
            event_id="evt-task-1",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskCreatedPayload(
                task_id="t1",
                title="Task 1",
                owner_id="alice",
                workflow_status=WorkflowStatus.DOING,
                deadline=NOW + timedelta(days=2),
            ),
        ),
    ]


def test_agent_run_turn_private_inquiry() -> None:
    agent = CoordinationAgent(project_id="test-proj")
    events = _init_events()
    events.append(
        TaskHealthReportedEvent(
            project_id="test-proj",
            event_id="evt-delay",
            event_type="task.health_reported",
            source=EventSource.MESSAGE,
            source_ref="ref",
            actor_id="alice",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskHealthReportedPayload(
                task_id="t1",
                health_status=HealthStatus.DELAYED,
                confidence=0.9,
            ),
        )
    )
    trace, generated = agent.run_turn(events, NOW)
    assert trace.round_number == 1
    assert trace.termination_reason is AgentTerminationReason.WAITING_RESPONSE
    assert len(trace.executed_command_ids) == 1

    case = agent.case_ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.WAITING_FOR_RESPONSE
    assert case.waiting_for == "alice"


def test_agent_asks_owner_when_someone_else_reports_the_risk() -> None:
    """D2: the member messaged and the case's waiting_for are both the owner, not QA."""
    adapter = MockCollaborationAdapter(project_id="test-proj")
    agent = CoordinationAgent(project_id="test-proj", adapter=adapter)
    qa = MemberRegisteredEvent(
        project_id="test-proj",
        event_id="evt-mem-qa",
        event_type="member.registered",
        source=EventSource.HUMAN,
        source_ref="setup",
        occurred_at=NOW,
        received_at=NOW,
        payload=MemberRegisteredPayload(member_id="frank", display_name="Frank", role="qa"),
    )
    qa_report = TaskHealthReportedEvent(
        project_id="test-proj",
        event_id="evt-qa-risk",
        event_type="task.health_reported",
        source=EventSource.MESSAGE,
        source_ref="ref",
        actor_id="frank",
        occurred_at=NOW,
        received_at=NOW,
        payload=TaskHealthReportedPayload(
            task_id="t1",
            health_status=HealthStatus.AT_RISK,
            blocker="no test environment yet",
            confidence=0.8,
        ),
    )

    agent.run_turn([*_init_events(), qa, qa_report], NOW)

    [(command, _)] = adapter.audit_log
    assert command.action_type is ActionType.ASK_RECOVERY_ESTIMATE
    assert command.targets == ("alice",)
    case = agent.case_ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.WAITING_FOR_RESPONSE
    assert case.waiting_for == "alice"


def test_agent_run_turn_no_action_when_healthy() -> None:
    agent = CoordinationAgent(project_id="test-proj")
    events = _init_events()
    trace, generated = agent.run_turn(events, NOW)
    assert trace.round_number == 1
    assert trace.termination_reason is AgentTerminationReason.NO_ACTION
    assert len(agent.case_ledger.get_all_cases()) == 0


def test_agent_run_turn_update_task_approval() -> None:
    agent = CoordinationAgent(project_id="test-proj")
    events = _init_events()
    events.append(
        TaskHealthReportedEvent(
            project_id="test-proj",
            event_id="evt-delay-overdue",
            event_type="task.health_reported",
            source=EventSource.MESSAGE,
            source_ref="ref",
            actor_id="alice",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskHealthReportedPayload(
                task_id="t1",
                health_status=HealthStatus.DELAYED,
                expected_completion=NOW + timedelta(days=5),
                confidence=0.95,
            ),
        )
    )
    trace, _ = agent.run_turn(events, NOW)
    assert trace.termination_reason is AgentTerminationReason.WAITING_APPROVAL

    # PM Carol approves the pending request
    pending = agent.approval_manager.get_pending_requests()
    assert len(pending) == 1
    agent.approval_manager.approve(pending[0].approval_id, "carol", NOW + timedelta(hours=1))

    trace2, generated = agent.run_turn([], NOW + timedelta(hours=1))
    assert trace2.termination_reason is AgentTerminationReason.ALL_RESOLVED
    case = agent.case_ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.RESOLVED


def _delay_report(
    event_id: str, task_id: str, at: datetime, expected_completion: datetime | None = None
) -> TaskHealthReportedEvent:
    return TaskHealthReportedEvent(
        project_id="test-proj",
        event_id=event_id,
        event_type="task.health_reported",
        source=EventSource.MESSAGE,
        source_ref="ref",
        actor_id="alice",
        occurred_at=at,
        received_at=at,
        payload=TaskHealthReportedPayload(
            task_id=task_id,
            health_status=HealthStatus.DELAYED,
            expected_completion=expected_completion,
            confidence=0.95,
        ),
    )


class _RejectingClient(MockFeishuClient):
    async def update_task_deadline(self, task_guid: str, deadline: datetime) -> dict:
        raise RuntimeError("Feishu update task failed: task not found")


# Each helper returns an agent whose next turn, run at the returned time,
# escalates case:task-health:t1, one helper per way into ESCALATED.


def _before_response_timeout() -> tuple[CoordinationAgent, datetime]:
    """Alice is asked for a recovery estimate and never answers."""
    agent = CoordinationAgent(project_id="test-proj")
    agent.run_turn([*_init_events(), _delay_report("evt-delay", "t1", NOW)], NOW)
    return agent, NOW + timedelta(hours=3)


def _before_rejected_approval_applies() -> tuple[CoordinationAgent, datetime]:
    """Carol rejects the proposed reschedule."""
    agent = CoordinationAgent(project_id="test-proj")
    late = _delay_report("evt-delay-overdue", "t1", NOW, NOW + timedelta(days=5))
    agent.run_turn([*_init_events(), late], NOW)
    [pending] = agent.approval_manager.get_pending_requests()
    agent.approval_manager.reject(
        pending.approval_id, "carol", "launch date is fixed", NOW + timedelta(hours=1)
    )
    return agent, NOW + timedelta(hours=1)


def _before_approved_update_fails() -> tuple[CoordinationAgent, datetime]:
    """Carol approves the reschedule, but writing it back to the platform fails."""
    adapter = FeishuCollaborationAdapter(
        client=_RejectingClient(), project_id="test-proj", task_writeback=True
    )
    agent = CoordinationAgent(project_id="test-proj", adapter=adapter)
    late = _delay_report("evt-delay-overdue", "t1", NOW, NOW + timedelta(days=5))
    agent.run_turn([*_init_events(), late], NOW)
    [pending] = agent.approval_manager.get_pending_requests()
    agent.approval_manager.approve(pending.approval_id, "carol", NOW + timedelta(hours=1))
    return agent, NOW + timedelta(hours=1)


def test_agent_escalates_when_the_approved_update_fails() -> None:
    """An approved reschedule that the platform rejects is escalated, not resolved."""
    agent, approved_at = _before_approved_update_fails()

    agent.run_turn([], approved_at)

    case = agent.case_ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.ESCALATED
    assert "task not found" in (case.terminal_reason or "")
    assert agent.state.tasks["t1"].deadline == NOW + timedelta(days=2)


@pytest.mark.parametrize(
    "before_escalation",
    [_before_response_timeout, _before_rejected_approval_applies, _before_approved_update_fails],
    ids=["response_timeout", "approval_rejected", "approved_update_failed"],
)
def test_escalation_is_reported_only_in_the_turn_it_happens(
    before_escalation: Callable[[], tuple[CoordinationAgent, datetime]],
) -> None:
    """Lifecycle defect D5: an old escalated case must not make later turns report it."""
    agent, escalation_time = before_escalation()

    escalating, _ = agent.run_turn([], escalation_time)
    quiet, _ = agent.run_turn([], escalation_time + timedelta(days=1))

    assert escalating.termination_reason is AgentTerminationReason.ESCALATED
    assert quiet.termination_reason is AgentTerminationReason.ALL_RESOLVED
    # Only the turn report changes; the case keeps its escalated status.
    case = agent.case_ledger.get_case("case:task-health:t1")
    assert case is not None
    assert case.status is CoordinationCaseStatus.ESCALATED


def test_a_new_escalation_is_reported_after_an_earlier_one() -> None:
    agent, first_escalation = _before_response_timeout()
    agent.run_turn([], first_escalation)
    t2_reported = first_escalation + timedelta(days=1)
    t2_created = TaskCreatedEvent(
        project_id="test-proj",
        event_id="evt-task-2",
        event_type="task.created",
        source=EventSource.TASK,
        source_ref="setup",
        occurred_at=t2_reported,
        received_at=t2_reported,
        payload=TaskCreatedPayload(
            task_id="t2",
            title="Task 2",
            owner_id="alice",
            workflow_status=WorkflowStatus.DOING,
            deadline=t2_reported + timedelta(days=2),
        ),
    )

    asked, _ = agent.run_turn(
        [t2_created, _delay_report("evt-delay-t2", "t2", t2_reported)], t2_reported
    )
    escalating, _ = agent.run_turn([], t2_reported + timedelta(hours=3))

    assert asked.termination_reason is AgentTerminationReason.WAITING_RESPONSE
    assert escalating.termination_reason is AgentTerminationReason.ESCALATED
    case = agent.case_ledger.get_case("case:task-health:t2")
    assert case is not None
    assert case.status is CoordinationCaseStatus.ESCALATED


def test_agent_run_turn_direct_update_task_action_approval() -> None:
    agent = CoordinationAgent(project_id="test-proj")
    events = _init_events()
    events.append(
        TaskHealthReportedEvent(
            project_id="test-proj",
            event_id="evt-delay",
            event_type="task.health_reported",
            source=EventSource.MESSAGE,
            source_ref="ref",
            actor_id="alice",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskHealthReportedPayload(
                task_id="t1",
                health_status=HealthStatus.DELAYED,
                confidence=0.9,
            ),
        )
    )
    agent.run_turn(events, NOW)

    # Manually queue an UPDATE_TASK action for testing direct update action flow
    case = agent.case_ledger.get_case("case:task-health:t1")
    assert case is not None
    action = CoordinationAction(
        action_id="act:direct-update",
        action_type=ActionType.UPDATE_TASK,
        targets=("carol",),
        reason_refs=("evt-delay",),
        expected_effect="update",
        payload={"task_id": "t1"},
    )
    cmd = ActionCommand(
        command_id="cmd:direct-update",
        action_id="act:direct-update",
        action_type=ActionType.UPDATE_TASK,
        targets=("t1",),
        payload={"task_id": "t1"},
        idempotency_key="idem:dir",
        created_at=NOW,
    )
    req = agent.approval_manager.create_request(case.case_id, action, cmd, "carol", NOW)
    agent.case_ledger.transition(case.case_id, CoordinationCaseStatus.WAITING_FOR_APPROVAL, NOW)
    agent.approval_manager.approve(req.approval_id, "carol", NOW + timedelta(minutes=5))

    trace, _ = agent.run_turn([], NOW + timedelta(minutes=6))
    assert trace.termination_reason is AgentTerminationReason.ALL_RESOLVED
    assert case.status is CoordinationCaseStatus.RESOLVED

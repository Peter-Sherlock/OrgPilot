"""Tests for bounded CoordinationAgent loop and multi-turn execution."""

from datetime import datetime, timedelta

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


class _ExplodingAdapter(MockCollaborationAdapter):
    """Simulates a channel outage: every transport call raises."""

    def send_private_message(self, command: ActionCommand):
        raise ConnectionError("feishu channel down")

    def request_approval(self, command: ActionCommand, approver_id: str):
        raise ConnectionError("feishu channel down")


def test_agent_turn_survives_adapter_transport_failure() -> None:
    """Regression: a transport-level adapter failure (Feishu 4xx, network outage)
    used to crash the whole coordination turn with an unhandled exception; it must
    degrade to a logged error while the case lifecycle continues."""
    agent = CoordinationAgent(project_id="test-proj", adapter=_ExplodingAdapter("test-proj"))
    events = _init_events()
    events.append(
        TaskHealthReportedEvent(
            project_id="test-proj",
            event_id="evt-delay-transport",
            event_type="task.health_reported",
            source=EventSource.MESSAGE,
            source_ref="ref",
            actor_id="alice",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskHealthReportedPayload(
                task_id="t1",
                health_status=HealthStatus.AT_RISK,
                blocker="SDK broken",
                confidence=0.9,
            ),
        )
    )

    trace, _ = agent.run_turn(events, NOW)

    assert trace.termination_reason is AgentTerminationReason.WAITING_RESPONSE
    cases = agent.case_ledger.get_active_cases()
    assert len(cases) == 1
    assert cases[0].status is CoordinationCaseStatus.WAITING_FOR_RESPONSE

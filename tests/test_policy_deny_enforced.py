"""The agent loop must honour a DENY decision instead of routing it to approval."""

from datetime import datetime

from orgpilot.agent.loop import CoordinationAgent
from orgpilot.domain.enums import (
    CoordinationCaseStatus,
    PolicyDisposition,
    RiskLevel,
    WorkflowStatus,
)
from orgpilot.domain.models import CoordinationAction, PolicyDecision
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    TaskCreatedEvent,
    TaskCreatedPayload,
    TaskHealthReportedEvent,
    TaskHealthReportedPayload,
)
from orgpilot.policy.engine import PolicyEngine

NOW = datetime.fromisoformat("2026-11-18T10:00:00+08:00")
PROJECT = "p-deny"


class DenyEverything(PolicyEngine):
    def evaluate(self, action: CoordinationAction) -> PolicyDecision:
        return PolicyDecision(
            action_id=action.action_id,
            disposition=PolicyDisposition.DENY,
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            reason="denied by test policy",
        )


def _events() -> list:
    common = {
        "project_id": PROJECT,
        "source_ref": "setup",
        "occurred_at": NOW,
        "received_at": NOW,
    }
    return [
        MemberRegisteredEvent(
            event_id="evt-alice",
            event_type="member.registered",
            source=EventSource.HUMAN,
            payload=MemberRegisteredPayload(member_id="alice", display_name="Alice", role="dev"),
            **common,
        ),
        TaskCreatedEvent(
            event_id="evt-task",
            event_type="task.created",
            source=EventSource.TASK,
            payload=TaskCreatedPayload(
                task_id="api",
                title="API",
                owner_id="alice",
                workflow_status=WorkflowStatus.DOING,
            ),
            **common,
        ),
        TaskHealthReportedEvent(
            event_id="evt-delay",
            event_type="task.health_reported",
            source=EventSource.MESSAGE,
            actor_id="alice",
            payload=TaskHealthReportedPayload(
                task_id="api", health_status="delayed", confidence=0.9
            ),
            **common,
        ),
    ]


def test_denied_action_is_neither_sent_nor_sent_for_approval() -> None:
    agent = CoordinationAgent(project_id=PROJECT)
    agent.policy_engine = DenyEverything()

    trace, _ = agent.run_turn(_events(), NOW)

    assert agent.adapter.audit_log == []
    assert agent.approval_manager.get_pending_requests() == ()
    assert trace.executed_command_ids == ()
    case = agent.case_ledger.get_case("case:task-health:api")
    assert case is not None
    assert case.status is CoordinationCaseStatus.ESCALATED
    assert "denied by test policy" in (case.terminal_reason or "")

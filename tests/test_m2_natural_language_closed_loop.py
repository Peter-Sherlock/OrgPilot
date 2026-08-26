"""End-to-end integration test: Text -> ClaimExtractor -> CoordinationAgent Loop."""

from datetime import datetime, timedelta

from orgpilot.agent.loop import CoordinationAgent
from orgpilot.domain.enums import (
    AgentTerminationReason,
    CoordinationCaseStatus,
    WorkflowStatus,
)
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    TaskCreatedEvent,
    TaskCreatedPayload,
)
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import MessageContext

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


def _init_project_events() -> list:
    return [
        MemberRegisteredEvent(
            project_id="p-nl-loop",
            event_id="evt-mem-alice",
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
            project_id="p-nl-loop",
            event_id="evt-mem-carol",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=MemberRegisteredPayload(member_id="carol", display_name="Carol", role="pm"),
        ),
        TaskCreatedEvent(
            project_id="p-nl-loop",
            event_id="evt-task-api",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskCreatedPayload(
                task_id="backend_api",
                title="Backend API",
                owner_id="alice",
                workflow_status=WorkflowStatus.DOING,
                deadline=NOW + timedelta(hours=4),
            ),
        ),
    ]


def test_natural_language_to_closed_loop_coordination() -> None:
    extractor = ClaimExtractor()
    agent = CoordinationAgent(project_id="p-nl-loop")

    # Round 1: Alice sends a chat message indicating delay with tomorrow completion
    msg_r1 = "支付 SDK 报错，排查需要到明天下午 5 点"
    context_r1 = MessageContext(
        project_id="p-nl-loop",
        actor_id="alice",
        occurred_at=NOW,
        known_tasks={"backend_api": "Backend API"},
        known_members={"alice": "engineer", "carol": "pm"},
    )
    result_r1, extracted_events_r1 = extractor.extract_from_message(msg_r1, context_r1)
    assert result_r1.is_actionable is True
    assert len(extracted_events_r1) == 1

    # Ingest baseline setup events + extracted event into Agent Turn 1
    events_round1 = _init_project_events() + extracted_events_r1
    trace_r1, _ = agent.run_turn(events_round1, NOW)

    # Since expected completion (tomorrow 17:00) exceeds deadline (today 14:00),
    # Agent proposes reschedule to PM Carol, which requires approval
    assert trace_r1.termination_reason is AgentTerminationReason.WAITING_APPROVAL
    case = agent.case_ledger.get_case("case:task-health:backend_api")
    assert case is not None
    assert case.status is CoordinationCaseStatus.WAITING_FOR_APPROVAL

    # PM Carol approves the pending reschedule request
    pending_approvals = agent.approval_manager.get_pending_requests()
    assert len(pending_approvals) == 1
    agent.approval_manager.approve(
        pending_approvals[0].approval_id, "carol", NOW + timedelta(minutes=10)
    )

    # Agent Turn 2: Executes approved task update and resolves case
    trace_r2, _ = agent.run_turn([], NOW + timedelta(minutes=11))
    assert trace_r2.termination_reason is AgentTerminationReason.ALL_RESOLVED
    assert case.status is CoordinationCaseStatus.RESOLVED

    # Check that task deadline in projector state was officially updated
    task = agent.projector.state.tasks["backend_api"]
    assert task.deadline is not None
    assert task.deadline.hour == 17

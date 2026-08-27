"""Tests for ProgressSyncCoordinator, multi-turn clarification, and DAG briefing."""

from datetime import UTC, datetime, timedelta

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.agent.loop import CoordinationAgent
from orgpilot.coordination.sync_coordinator import ProgressSyncCoordinator
from orgpilot.domain.enums import (
    ProbeMemberStatus,
    SyncSessionStatus,
    WorkflowStatus,
)
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    TaskCreatedEvent,
    TaskCreatedPayload,
)


def test_sync_coordinator_full_lifecycle() -> None:
    now = datetime.now(UTC)
    project_id = "test-sync-proj"
    adapter = MockCollaborationAdapter(project_id)
    agent = CoordinationAgent(project_id, adapter)

    # 1. Setup project state: 3 members, 3 dependent tasks
    events = [
        MemberRegisteredEvent(
            project_id=project_id,
            event_id="m-pm",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="init",
            occurred_at=now,
            received_at=now,
            payload=MemberRegisteredPayload(
                member_id="ou_pm", display_name="Project Manager", role="pm"
            ),
        ),
        MemberRegisteredEvent(
            project_id=project_id,
            event_id="m-alice",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="init",
            occurred_at=now,
            received_at=now,
            payload=MemberRegisteredPayload(
                member_id="ou_alice", display_name="Alice", role="engineer"
            ),
        ),
        MemberRegisteredEvent(
            project_id=project_id,
            event_id="m-bob",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="init",
            occurred_at=now,
            received_at=now,
            payload=MemberRegisteredPayload(
                member_id="ou_bob", display_name="Bob", role="engineer"
            ),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id="t-pay",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="init",
            occurred_at=now,
            received_at=now,
            payload=TaskCreatedPayload(
                task_id="task-payment",
                title="支付SDK接入",
                owner_id="ou_alice",
                workflow_status=WorkflowStatus.DOING,
                deadline=now + timedelta(days=2),
            ),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id="t-checkout",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="init",
            occurred_at=now,
            received_at=now,
            payload=TaskCreatedPayload(
                task_id="task-checkout",
                title="收银台前端结账",
                owner_id="ou_bob",
                workflow_status=WorkflowStatus.TODO,
                deadline=now + timedelta(days=3),
                dependencies=("task-payment",),
            ),
        ),
    ]
    for e in events:
        agent.event_log.append(e)
        agent.projector.apply(e)

    coordinator = ProgressSyncCoordinator(agent, adapter)

    # 2. PM triggers sync probe
    session = coordinator.start_sync_session(project_id=project_id, initiated_by="ou_pm")
    assert session.status == SyncSessionStatus.PROBING
    assert "ou_alice" in session.member_probes
    assert "ou_bob" in session.member_probes
    assert len(adapter.audit_log) >= 2

    # 3. Bob replies with clear on-track progress
    converged_bob, clarify_bob = coordinator.handle_member_reply(
        session_id=session.session_id,
        member_id="ou_bob",
        message="收银台前端一切正常，按原计划推进",
        occurred_at=now,
    )
    assert converged_bob is True
    assert clarify_bob is None
    assert session.member_probes["ou_bob"].status == ProbeMemberStatus.COLLECTED

    # 4. Alice replies with vague blocker without time (triggers clarification)
    converged_alice1, clarify_alice1 = coordinator.handle_member_reply(
        session_id=session.session_id,
        member_id="ou_alice",
        message="支付 SDK 报错卡住了，还在排查",
        occurred_at=now,
    )
    assert converged_alice1 is False
    assert clarify_alice1 is not None
    assert "什么时候" in clarify_alice1 or "几点" in clarify_alice1
    assert session.member_probes["ou_alice"].status == ProbeMemberStatus.CLARIFYING
    assert session.status == SyncSessionStatus.CLARIFYING

    # 5. Alice replies to clarification with specific resolution time
    converged_alice2, clarify_alice2 = coordinator.handle_member_reply(
        session_id=session.session_id,
        member_id="ou_alice",
        message="预计排查需要到明天下午 5 点",
        occurred_at=now,
    )
    assert converged_alice2 is True
    assert clarify_alice2 is None
    assert session.member_probes["ou_alice"].status == ProbeMemberStatus.COLLECTED

    # 6. Verify session completed and ExecutiveBriefing synthesized
    assert session.status == SyncSessionStatus.COMPLETED
    assert session.briefing is not None

    briefing = session.briefing
    assert briefing.total_active_tasks == 2
    assert briefing.delayed_count == 1
    assert briefing.on_track_count == 1

    # Verify DAG impact was calculated (Alice's delay impacts Bob's checkout task)
    assert len(briefing.topological_risks) == 1
    risk = briefing.topological_risks[0]
    assert risk.source_task_id == "task-payment"
    assert "task-checkout" in risk.cascading_impact_tasks
    assert risk.severity == "CRITICAL"
    assert len(briefing.recommended_actions) > 0

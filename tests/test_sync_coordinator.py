"""Tests for ProgressSyncCoordinator: convergence, timeout, and DAG briefing.

Since 3a.1 the coordinator no longer extracts replies — the intent pipeline
persists claims and reports outcomes via ``notify_probe_finished``. These tests
drive that public API directly, applying claims to the agent state the way the
handlers do.
"""

from datetime import UTC, datetime, timedelta

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.agent.loop import CoordinationAgent
from orgpilot.coordination.sync_coordinator import ProgressSyncCoordinator
from orgpilot.domain.enums import (
    HealthStatus,
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
    TaskHealthReportedEvent,
    TaskHealthReportedPayload,
)

NOW = datetime.now(UTC)


def _seed(agent: CoordinationAgent, project_id: str) -> None:
    events = [
        MemberRegisteredEvent(
            project_id=project_id,
            event_id="m-pm",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="init",
            occurred_at=NOW,
            received_at=NOW,
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
            occurred_at=NOW,
            received_at=NOW,
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
            occurred_at=NOW,
            received_at=NOW,
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
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskCreatedPayload(
                task_id="task-payment",
                title="支付SDK接入",
                owner_id="ou_alice",
                workflow_status=WorkflowStatus.DOING,
                deadline=NOW + timedelta(days=2),
            ),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id="t-checkout",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="init",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskCreatedPayload(
                task_id="task-checkout",
                title="收银台前端结账",
                owner_id="ou_bob",
                workflow_status=WorkflowStatus.TODO,
                deadline=NOW + timedelta(days=3),
                dependencies=("task-payment",),
            ),
        ),
    ]
    for event in events:
        agent.event_log.append(event)
        agent.projector.apply(event)


def _report_claim(
    project_id: str, task_id: str, actor: str, health: HealthStatus, moment: datetime
) -> TaskHealthReportedEvent:
    return TaskHealthReportedEvent(
        project_id=project_id,
        event_id=f"evt-{task_id}-{actor}-{moment.timestamp()}",
        event_type="task.health_reported",
        source=EventSource.MESSAGE,
        source_ref=f"message:{task_id}:{actor}:{moment.timestamp()}",
        actor_id=actor,
        occurred_at=moment,
        received_at=moment,
        payload=TaskHealthReportedPayload(
            task_id=task_id,
            health_status=health,
            confidence=0.95,
        ),
    )


def test_notify_probe_finished_converges_and_synthesizes_briefing() -> None:
    now = datetime.now(UTC)
    project_id = "test-sync-proj"
    adapter = MockCollaborationAdapter(project_id)
    agent = CoordinationAgent(project_id, adapter)
    _seed(agent, project_id)

    coordinator = ProgressSyncCoordinator(agent, adapter)
    session = coordinator.start_sync_session(project_id=project_id, initiated_by="ou_pm")
    assert session.status == SyncSessionStatus.PROBING
    assert "ou_alice" in session.member_probes
    assert "ou_bob" in session.member_probes
    assert len(adapter.audit_log) >= 2

    # Bob reports his checkout task on track (claim persisted via the pipeline)
    bob_claim = _report_claim(project_id, "task-checkout", "ou_bob", HealthStatus.ON_TRACK, now)
    agent.event_log.append(bob_claim)
    agent.projector.apply(bob_claim)
    session = coordinator.notify_probe_finished(
        project_id, "ou_bob", complete=True, occurred_at=now
    )
    assert session is not None
    assert session.member_probes["ou_bob"].status == ProbeMemberStatus.COLLECTED
    assert session.status == SyncSessionStatus.PROBING  # alice still pending

    # Alice reports her payment task delayed — this converges the session
    alice_claim = _report_claim(project_id, "task-payment", "ou_alice", HealthStatus.DELAYED, now)
    agent.event_log.append(alice_claim)
    agent.projector.apply(alice_claim)
    session = coordinator.notify_probe_finished(
        project_id, "ou_alice", complete=True, occurred_at=now
    )
    assert session is not None
    assert session.member_probes["ou_alice"].status == ProbeMemberStatus.COLLECTED
    assert session.status == SyncSessionStatus.COMPLETED
    assert session.briefing is not None

    briefing = session.briefing
    assert briefing.total_active_tasks == 2
    assert briefing.delayed_count == 1
    assert briefing.on_track_count == 1

    # DAG impact: alice's delay cascades into bob's checkout task
    assert len(briefing.topological_risks) == 1
    risk = briefing.topological_risks[0]
    assert risk.source_task_id == "task-payment"
    assert "task-checkout" in risk.cascading_impact_tasks
    assert risk.severity == "CRITICAL"
    assert len(briefing.recommended_actions) > 0


def test_incomplete_probe_is_flagged_in_briefing() -> None:
    now = datetime.now(UTC)
    project_id = "test-sync-incomplete"
    adapter = MockCollaborationAdapter(project_id)
    agent = CoordinationAgent(project_id, adapter)
    _seed(agent, project_id)

    coordinator = ProgressSyncCoordinator(agent, adapter)
    session = coordinator.start_sync_session(project_id=project_id, initiated_by="ou_pm")

    bob_claim = _report_claim(project_id, "task-checkout", "ou_bob", HealthStatus.ON_TRACK, now)
    agent.event_log.append(bob_claim)
    agent.projector.apply(bob_claim)
    coordinator.notify_probe_finished(project_id, "ou_bob", complete=True, occurred_at=now)

    # Alice's answers stayed partial after the max rounds: collected but flagged
    coordinator.notify_probe_finished(
        project_id, "ou_alice", complete=False, occurred_at=now, note="信息不全"
    )
    assert session.member_probes["ou_alice"].status == ProbeMemberStatus.COLLECTED
    assert session.member_probes["ou_alice"].incomplete is True
    assert session.status == SyncSessionStatus.COMPLETED
    assert session.briefing is not None
    joined = "\n".join(session.briefing.recommended_actions)
    assert "信息不全" in joined
    assert "Alice" in joined


def test_notify_ignores_unknown_members_and_finished_probes() -> None:
    now = datetime.now(UTC)
    project_id = "test-sync-notify-guard"
    adapter = MockCollaborationAdapter(project_id)
    agent = CoordinationAgent(project_id, adapter)
    _seed(agent, project_id)

    coordinator = ProgressSyncCoordinator(agent, adapter)
    session = coordinator.start_sync_session(project_id=project_id, initiated_by="ou_pm")

    assert coordinator.notify_probe_finished(project_id, "ou_stranger", complete=True) is None
    first = coordinator.notify_probe_finished(
        project_id, "ou_bob", complete=True, occurred_at=now
    )
    assert first is not None
    # a second notification for an already-collected probe is a no-op
    again = coordinator.notify_probe_finished(
        project_id, "ou_bob", complete=True, occurred_at=now
    )
    assert again is None
    assert session.member_probes["ou_bob"].status == ProbeMemberStatus.COLLECTED

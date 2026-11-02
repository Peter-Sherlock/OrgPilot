"""Completion retires risk evidence without erasing history or inventing health."""

import pytest

from orgpilot.dependencies import DependencyAnalyzer
from orgpilot.domain.enums import ClaimStatus, HealthStatus, WorkflowStatus
from orgpilot.events.models import OrgEvent
from orgpilot.state.projector import OrgProjector
from tests.conftest import make_event


def _events(risk: str, keep_healthy: bool) -> list[OrgEvent]:
    events = [
        make_event(
            "alice",
            "member.registered",
            {"member_id": "alice", "display_name": "Alice", "role": "engineer"},
        ),
        make_event(
            "bob", "member.registered", {"member_id": "bob", "display_name": "Bob", "role": "qa"}
        ),
        make_event(
            "api",
            "task.created",
            {"task_id": "api", "title": "API", "owner_id": "alice", "workflow_status": "doing"},
            minute=1,
        ),
        make_event(
            "ui",
            "task.created",
            {"task_id": "ui", "title": "UI", "owner_id": "alice", "dependencies": ["api"]},
            minute=2,
        ),
        make_event(
            "risk",
            "task.health_reported",
            {"task_id": "api", "health_status": risk, "confidence": 0.9},
            actor_id="alice",
            minute=3,
        ),
    ]
    if keep_healthy:
        events.append(
            make_event(
                "healthy",
                "task.health_reported",
                {"task_id": "api", "health_status": "on_track", "confidence": 0.9},
                actor_id="bob",
                minute=4,
            )
        )
    events.append(
        make_event(
            "done", "task.workflow_changed", {"task_id": "api", "to_status": "done"}, minute=5
        )
    )
    return events


@pytest.mark.parametrize("risk", ["at_risk", "delayed"])
@pytest.mark.parametrize("keep_healthy", [False, True])
def test_done_retires_only_risk_claims_and_replays_identically(risk, keep_healthy):
    events = _events(risk, keep_healthy)
    projector = OrgProjector("test-project")
    state = projector.replay(events)
    task = state.tasks["api"]
    claim = state.health_claims["claim:risk:task_health"]

    assert task.workflow_status is WorkflowStatus.DONE
    assert task.health_status is (HealthStatus.ON_TRACK if keep_healthy else HealthStatus.UNKNOWN)
    assert not task.health_conflict
    assert claim.status is ClaimStatus.SUPERSEDED
    assert claim.superseded_by == "done"
    assert claim.source_event_id == "risk"
    assert claim.health_status.value == risk
    assert "risk" in task.source_event_ids and "done" in task.source_event_ids
    if keep_healthy:
        assert state.health_claims["claim:healthy:task_health"].status is ClaimStatus.ACTIVE
    assert DependencyAnalyzer().impacts(state.tasks) == ()
    snapshot = state.model_dump(mode="json")
    assert not projector.apply(events[-1])
    assert projector.state.model_dump(mode="json") == snapshot
    assert OrgProjector("test-project").replay(events).model_dump(mode="json") == snapshot


def test_risk_report_after_completion_stays_historical_until_task_reopens():
    projector = OrgProjector("test-project")
    projector.replay(_events("delayed", False))
    late = make_event(
        "late-risk",
        "task.health_reported",
        {"task_id": "api", "health_status": "delayed", "confidence": 0.9},
        actor_id="alice",
        minute=6,
    )
    projector.apply(late)
    assert projector.state.tasks["api"].workflow_status is WorkflowStatus.DONE
    assert projector.state.tasks["api"].health_status is HealthStatus.UNKNOWN
    assert (
        projector.state.health_claims["claim:late-risk:task_health"].status
        is ClaimStatus.SUPERSEDED
    )
    assert DependencyAnalyzer().impacts(projector.state.tasks) == ()

    projector.apply(
        make_event(
            "reopen",
            "task.workflow_changed",
            {"task_id": "api", "from_status": "done", "to_status": "doing"},
            minute=7,
        )
    )
    assert projector.state.tasks["api"].health_status is HealthStatus.UNKNOWN
    projector.apply(
        late.model_copy(
            update={"event_id": "new-risk", "occurred_at": late.occurred_at.replace(minute=8)}
        )
    )
    assert projector.state.tasks["api"].health_status is HealthStatus.DELAYED
    assert projector.state.health_claims["claim:new-risk:task_health"].status is ClaimStatus.ACTIVE
    assert len(DependencyAnalyzer().impacts(projector.state.tasks)) == 1


@pytest.mark.parametrize("risk", [HealthStatus.AT_RISK, HealthStatus.DELAYED])
def test_done_task_is_not_a_risk_seed_even_in_a_legacy_snapshot(risk):
    state = OrgProjector("test-project").replay(_events("delayed", False))
    state.tasks["api"].health_status = risk
    assert DependencyAnalyzer().impacts(state.tasks) == ()

"""Projection tests cover provenance, invariants, and claim supersession."""

import pytest

from orgpilot.domain.enums import ClaimStatus, CommitmentStatus, HealthStatus, WorkflowStatus
from orgpilot.domain.errors import DomainInvariantError
from orgpilot.state.projector import OrgProjector
from tests.conftest import make_event


def _register(
    projector: OrgProjector,
    member_id: str,
    minute: int = 0,
    event_id: str | None = None,
) -> None:
    projector.apply(
        make_event(
            event_id or f"evt-member-{member_id}",
            "member.registered",
            {
                "member_id": member_id,
                "display_name": member_id.title(),
                "role": "engineer",
            },
            minute=minute,
        )
    )


def _create_task(
    projector: OrgProjector,
    task_id: str = "api",
    *,
    owner_id: str = "alice",
    dependencies: list[str] | None = None,
    minute: int = 1,
    event_id: str | None = None,
) -> None:
    projector.apply(
        make_event(
            event_id or f"evt-task-{task_id}",
            "task.created",
            {
                "task_id": task_id,
                "title": task_id.title(),
                "owner_id": owner_id,
                "workflow_status": "doing",
                "dependencies": dependencies or [],
            },
            minute=minute,
        )
    )


def test_health_claims_preserve_workflow_and_supersede_per_actor() -> None:
    projector = OrgProjector("test-project")
    _register(projector, "alice")
    _register(projector, "bob", minute=1)
    _create_task(projector, minute=2)

    alice_risk = make_event(
        "evt-alice-risk",
        "task.health_reported",
        {"task_id": "api", "health_status": "at_risk", "confidence": 0.9},
        actor_id="alice",
        source="message",
        minute=3,
    )
    assert projector.apply(alice_risk)
    assert not projector.apply(alice_risk)
    assert projector.state.tasks["api"].workflow_status is WorkflowStatus.DOING
    assert projector.state.tasks["api"].health_status is HealthStatus.AT_RISK

    projector.apply(
        make_event(
            "evt-bob-on-track",
            "task.health_reported",
            {"task_id": "api", "health_status": "on_track", "confidence": 0.8},
            actor_id="bob",
            source="message",
            minute=4,
        )
    )
    assert projector.state.tasks["api"].health_status is HealthStatus.AT_RISK
    assert projector.state.tasks["api"].health_conflict

    projector.apply(
        make_event(
            "evt-alice-on-track",
            "task.health_reported",
            {
                "task_id": "api",
                "health_status": "on_track",
                "expected_completion": "2026-09-02T18:00:00+08:00",
                "confidence": 0.95,
            },
            actor_id="alice",
            source="message",
            minute=5,
        )
    )
    assert (
        projector.state.health_claims["claim:evt-alice-risk:task_health"].status
        is ClaimStatus.SUPERSEDED
    )
    assert projector.state.tasks["api"].health_status is HealthStatus.ON_TRACK
    assert not projector.state.tasks["api"].health_conflict


def test_matching_commitment_is_fulfilled_by_workflow_change() -> None:
    projector = OrgProjector("test-project")
    _register(projector, "alice")
    _create_task(projector)
    projector.apply(
        make_event(
            "evt-commitment",
            "commitment.made",
            {
                "commitment_id": "commitment-1",
                "target_type": "task",
                "target_id": "api",
                "predicate": "workflow_status",
                "expected_value": "done",
            },
            actor_id="alice",
            minute=2,
        )
    )
    projector.apply(
        make_event(
            "evt-task-done",
            "task.workflow_changed",
            {"task_id": "api", "from_status": "doing", "to_status": "done"},
            minute=3,
        )
    )

    assert projector.state.commitments["commitment-1"].status is CommitmentStatus.FULFILLED


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (
            make_event(
                "evt-bad-project",
                "member.registered",
                {"member_id": "alice", "display_name": "Alice", "role": "backend"},
                project_id="other-project",
            ),
            "does not match",
        ),
        (
            make_event(
                "evt-bad-owner",
                "task.created",
                {"task_id": "api", "title": "API", "owner_id": "missing"},
            ),
            "unknown task owner",
        ),
    ],
)
def test_basic_projection_invariants(event: object, message: str) -> None:
    projector = OrgProjector("test-project")
    with pytest.raises(DomainInvariantError, match=message):
        projector.apply(event)  # type: ignore[arg-type]


def test_task_dependency_and_workflow_invariants() -> None:
    projector = OrgProjector("test-project")
    _register(projector, "alice")
    with pytest.raises(DomainInvariantError, match="cannot depend on itself"):
        _create_task(projector, dependencies=["api"])
    with pytest.raises(DomainInvariantError, match="unknown task dependencies"):
        _create_task(projector, dependencies=["missing"])

    _create_task(projector)
    with pytest.raises(DomainInvariantError, match="already exists"):
        _create_task(projector, event_id="evt-task-api-again")
    with pytest.raises(DomainInvariantError, match="expected todo"):
        projector.apply(
            make_event(
                "evt-wrong-from",
                "task.workflow_changed",
                {"task_id": "api", "from_status": "todo", "to_status": "done"},
                minute=2,
            )
        )


def test_member_and_health_actor_invariants() -> None:
    projector = OrgProjector("test-project")
    _register(projector, "alice")
    with pytest.raises(DomainInvariantError, match="already exists"):
        _register(projector, "alice", event_id="evt-member-alice-again")
    _create_task(projector)
    with pytest.raises(DomainInvariantError, match="unknown event actor"):
        projector.apply(
            make_event(
                "evt-unknown-actor",
                "task.health_reported",
                {"task_id": "api", "health_status": "at_risk", "confidence": 0.8},
                actor_id="bob",
            )
        )
    with pytest.raises(DomainInvariantError, match="unknown task"):
        projector.apply(
            make_event(
                "evt-unknown-task",
                "task.health_reported",
                {"task_id": "missing", "health_status": "at_risk", "confidence": 0.8},
                actor_id="alice",
            )
        )


def test_commitment_invariants_and_explicit_supersession() -> None:
    projector = OrgProjector("test-project")
    _register(projector, "alice")
    _create_task(projector)
    base_payload = {
        "commitment_id": "commitment-1",
        "target_type": "task",
        "target_id": "api",
        "predicate": "workflow_status",
        "expected_value": "done",
    }
    with pytest.raises(DomainInvariantError, match="requires actor_id"):
        projector.apply(make_event("evt-no-actor", "commitment.made", base_payload))
    with pytest.raises(DomainInvariantError, match="unknown commitment actor"):
        projector.apply(
            make_event(
                "evt-bad-actor",
                "commitment.made",
                base_payload,
                actor_id="bob",
            )
        )
    bad_target = {**base_payload, "target_id": "missing"}
    with pytest.raises(DomainInvariantError, match="unknown commitment target"):
        projector.apply(
            make_event("evt-bad-target", "commitment.made", bad_target, actor_id="alice")
        )

    commitment_event = make_event(
        "evt-commitment", "commitment.made", base_payload, actor_id="alice"
    )
    projector.apply(commitment_event)
    with pytest.raises(DomainInvariantError, match="already exists"):
        projector.apply(
            make_event(
                "evt-duplicate-commitment",
                "commitment.made",
                base_payload,
                actor_id="alice",
            )
        )
    with pytest.raises(DomainInvariantError, match="unknown commitment"):
        projector.apply(
            make_event(
                "evt-supersede-missing",
                "commitment.superseded",
                {"commitment_id": "missing", "reason": "test"},
            )
        )

    supersede = make_event(
        "evt-supersede",
        "commitment.superseded",
        {"commitment_id": "commitment-1", "reason": "new forecast"},
    )
    projector.apply(supersede)
    assert projector.state.commitments["commitment-1"].status is CommitmentStatus.SUPERSEDED
    with pytest.raises(DomainInvariantError, match="only active"):
        projector.apply(
            make_event(
                "evt-supersede-again",
                "commitment.superseded",
                {"commitment_id": "commitment-1", "reason": "again"},
            )
        )


def test_task_updated_event_handling() -> None:
    projector = OrgProjector("test-project")
    _register(projector, "alice")
    _register(projector, "bob", minute=1)
    _create_task(projector, "api", owner_id="alice", minute=2)

    # Update deadline, title, and owner to bob
    update_evt = make_event(
        "evt-update-api",
        "task.updated",
        {
            "task_id": "api",
            "deadline": "2026-09-20T18:00:00+08:00",
            "title": "API v2",
            "owner_id": "bob",
        },
        minute=3,
        actor_id="carol",
    )
    projector.apply(update_evt)
    task = projector.state.tasks["api"]
    assert task.title == "API v2"
    assert task.owner_id == "bob"
    assert task.deadline is not None

    # Unknown owner
    bad_owner_evt = make_event(
        "evt-bad-owner",
        "task.updated",
        {"task_id": "api", "owner_id": "unknown"},
        minute=4,
    )
    with pytest.raises(DomainInvariantError, match="unknown task owner"):
        projector.apply(bad_owner_evt)

    # Unknown task
    bad_task_evt = make_event(
        "evt-bad-task",
        "task.updated",
        {"task_id": "missing", "title": "New"},
        minute=5,
    )
    with pytest.raises(DomainInvariantError, match="unknown task"):
        projector.apply(bad_task_evt)

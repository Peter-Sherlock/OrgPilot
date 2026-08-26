"""Dependency graph tests protect deterministic impact paths and validation."""

from datetime import datetime

import pytest

from orgpilot.dependencies import DependencyAnalyzer
from orgpilot.domain.enums import HealthStatus, WorkflowStatus
from orgpilot.domain.errors import DependencyCycleError, DomainInvariantError
from orgpilot.domain.models import TaskState

NOW = datetime.fromisoformat("2026-09-01T09:00:00+08:00")


def _task(
    task_id: str,
    dependencies: tuple[str, ...] = (),
    *,
    health: HealthStatus = HealthStatus.UNKNOWN,
    workflow: WorkflowStatus = WorkflowStatus.DOING,
) -> TaskState:
    return TaskState(
        task_id=task_id,
        title=task_id,
        owner_id="alice",
        workflow_status=workflow,
        health_status=health,
        dependencies=dependencies,
        source_event_ids=(f"evt-{task_id}",),
        last_update_at=NOW,
    )


def test_propagates_transitive_impacts_with_paths() -> None:
    tasks = {
        "a": _task("a", health=HealthStatus.DELAYED),
        "b": _task("b", ("a",)),
        "c": _task("c", ("b",)),
    }

    impacts = DependencyAnalyzer().impacts(tasks)

    assert [(item.impacted_task_id, item.path) for item in impacts] == [
        ("b", ("a", "b")),
        ("c", ("a", "b", "c")),
    ]


def test_blocked_workflow_is_also_a_risk_seed() -> None:
    tasks = {
        "a": _task("a", workflow=WorkflowStatus.BLOCKED),
        "b": _task("b", ("a",)),
    }
    assert len(DependencyAnalyzer().impacts(tasks)) == 1


def test_rejects_unknown_dependency() -> None:
    tasks = {"a": _task("a", ("missing",))}
    with pytest.raises(DomainInvariantError, match="unknown dependencies"):
        DependencyAnalyzer().validate(tasks)


def test_rejects_dependency_cycle() -> None:
    tasks = {"a": _task("a", ("b",)), "b": _task("b", ("a",))}
    with pytest.raises(DependencyCycleError, match="cycle"):
        DependencyAnalyzer().validate(tasks)

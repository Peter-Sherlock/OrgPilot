"""Deterministic task dependency validation and impact propagation."""

from collections import defaultdict, deque

from orgpilot.domain.enums import HealthStatus, WorkflowStatus
from orgpilot.domain.errors import DependencyCycleError, DomainInvariantError
from orgpilot.domain.models import DependencyImpact, TaskState


class DependencyAnalyzer:
    """Traverses prerequisite-to-dependent edges without an external graph database."""

    def validate(self, tasks: dict[str, TaskState]) -> None:
        for task in tasks.values():
            missing = set(task.dependencies) - tasks.keys()
            if missing:
                raise DomainInvariantError(
                    f"task {task.task_id!r} has unknown dependencies: {sorted(missing)}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise DependencyCycleError(f"dependency cycle includes task {task_id!r}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in tasks[task_id].dependencies:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in sorted(tasks):
            visit(task_id)

    def impacts(self, tasks: dict[str, TaskState]) -> tuple[DependencyImpact, ...]:
        self.validate(tasks)
        dependents: dict[str, list[str]] = defaultdict(list)
        for task in tasks.values():
            for dependency_id in task.dependencies:
                dependents[dependency_id].append(task.task_id)
        for task_ids in dependents.values():
            task_ids.sort()

        risky_tasks = sorted(
            task.task_id
            for task in tasks.values()
            if task.workflow_status is WorkflowStatus.BLOCKED
            or task.health_status in {HealthStatus.AT_RISK, HealthStatus.DELAYED}
        )

        results: list[DependencyImpact] = []
        for source_task_id in risky_tasks:
            queue: deque[tuple[str, tuple[str, ...]]] = deque(
                (task_id, (source_task_id, task_id)) for task_id in dependents[source_task_id]
            )
            seen: set[str] = set()
            while queue:
                impacted_task_id, path = queue.popleft()
                if impacted_task_id in seen:
                    continue
                seen.add(impacted_task_id)
                results.append(
                    DependencyImpact(
                        source_task_id=source_task_id,
                        impacted_task_id=impacted_task_id,
                        path=path,
                    )
                )
                queue.extend(
                    (dependent_id, (*path, dependent_id))
                    for dependent_id in dependents[impacted_task_id]
                )

        return tuple(sorted(results, key=lambda item: (item.source_task_id, item.impacted_task_id)))

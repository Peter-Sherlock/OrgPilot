"""Plans the events a team.yaml import writes (ADR-0009 §4-§5).

Pure: the caller supplies the projected state, the event history, resolved
open_ids, and the clock. Task fields merge three ways against the value the last
import set, so re-importing an unchanged team.yaml never reverts a deadline or
status that changed at runtime (an approved reschedule, a chat status report).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from orgpilot.domain.enums import WorkflowStatus
from orgpilot.domain.models import OrgState, TaskState
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    OrgEvent,
    TaskCreatedEvent,
    TaskCreatedPayload,
    TaskUpdatedEvent,
    TaskUpdatedPayload,
    TaskWorkflowChangedEvent,
    TaskWorkflowChangedPayload,
)
from orgpilot.team.spec import (
    FORMER_PRIVILEGED_ROLES,
    PM_ROLE,
    TaskSpec,
    TeamImportError,
    TeamSpec,
)

IMPORT_SOURCE_PREFIX = "team-import@"
_MISSING = object()


class ChangeKind(StrEnum):
    ADD = "+"
    UPDATE = "~"
    KEEP = "="
    SKIP = "!"


@dataclass(frozen=True)
class PlannedChange:
    kind: ChangeKind
    entity: str
    key: str
    detail: str

    def render(self) -> str:
        return f"  {self.kind.value} {self.entity:<6}  {self.key}  {self.detail}"


@dataclass(frozen=True)
class ImportPlan:
    events: tuple[OrgEvent, ...]
    changes: tuple[PlannedChange, ...]
    warnings: tuple[str, ...] = ()

    @property
    def skipped(self) -> tuple[PlannedChange, ...]:
        return tuple(c for c in self.changes if c.kind is ChangeKind.SKIP)


@dataclass
class _Builder:
    project_id: str
    now: datetime
    source_ref: str
    run_id: str
    events: list[OrgEvent] = field(default_factory=list)
    changes: list[PlannedChange] = field(default_factory=list)

    def note(self, kind: ChangeKind, entity: str, key: str, detail: str) -> None:
        self.changes.append(PlannedChange(kind, entity, key, detail))

    def stamp(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_ref": self.source_ref,
            "occurred_at": self.now,
            "received_at": self.now,
        }


def _fmt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%m-%d %H:%M")
    if isinstance(value, WorkflowStatus):
        return value.value
    return "无" if value is None else str(value)


def imported_baseline(history: Iterable[OrgEvent]) -> dict[str, dict[str, Any]]:
    """Per task, the field values most recently written by a team import."""
    baseline: dict[str, dict[str, Any]] = {}
    for event in history:
        if not event.source_ref.startswith(IMPORT_SOURCE_PREFIX):
            continue
        match event:
            case TaskCreatedEvent(payload=p):
                baseline[p.task_id] = {
                    "title": p.title,
                    "owner_id": p.owner_id,
                    "deadline": p.deadline,
                    "workflow_status": p.workflow_status,
                }
            case TaskUpdatedEvent(payload=p):
                fields = baseline.setdefault(p.task_id, {})
                for name in ("title", "owner_id", "deadline"):
                    value = getattr(p, name)
                    if value is not None:
                        fields[name] = value
            case TaskWorkflowChangedEvent(payload=p):
                baseline.setdefault(p.task_id, {})["workflow_status"] = p.to_status
    return baseline


def _order_new_tasks(new_tasks: Sequence[TaskSpec]) -> list[TaskSpec]:
    """Topologically orders new tasks so each is created after its new dependencies."""
    pending = {task.id: task for task in new_tasks}
    ordered: list[TaskSpec] = []
    while pending:
        ready = [
            task for task in pending.values() if not any(dep in pending for dep in task.depends_on)
        ]
        if not ready:
            raise TeamImportError(
                [f"dependency cycle among new tasks: {', '.join(sorted(pending))}"]
            )
        for task in ready:
            ordered.append(task)
            del pending[task.id]
    return ordered


def plan_import(
    spec: TeamSpec,
    state: OrgState,
    open_ids: Mapping[str, str],
    history: Sequence[OrgEvent],
    *,
    now: datetime,
    digest: str,
) -> ImportPlan:
    """Diffs team.yaml against the projected state and returns the events to write.

    ``open_ids`` maps member alias -> resolved open_id for every member in ``spec``.
    """
    run_id = f"{digest}-{int(now.timestamp() * 1000)}"
    b = _Builder(spec.project, now, f"{IMPORT_SOURCE_PREFIX}{digest}", run_id)
    warnings: list[str] = []
    errors: list[str] = []

    # Members: add-only; name/role drift and removals are reported, never applied.
    yaml_open_ids = set(open_ids.values())
    for alias, member in spec.members.items():
        open_id = open_ids[alias]
        if member.role in FORMER_PRIVILEGED_ROLES:
            warnings.append(
                f"member {alias!r} has role {member.role!r}: only 'pm' starts syncs and "
                "receives approvals (ADR-0009 §7)"
            )
        existing = state.members.get(open_id)
        if existing is None:
            b.events.append(
                MemberRegisteredEvent(
                    **b.stamp(),
                    event_id=f"team-import:{spec.project}:member:{open_id}",
                    event_type="member.registered",
                    source=EventSource.HUMAN,
                    payload=MemberRegisteredPayload(
                        member_id=open_id, display_name=member.name, role=member.role
                    ),
                )
            )
            b.note(ChangeKind.ADD, "member", alias, f"{member.name} ({member.role})")
            continue
        if existing.display_name != member.name:
            b.note(
                ChangeKind.SKIP,
                "member",
                alias,
                f"名字 {existing.display_name} → {member.name}：不支持修改，已跳过",
            )
        if existing.role != member.role:
            b.note(
                ChangeKind.SKIP,
                "member",
                alias,
                f"角色 {existing.role} → {member.role}：不支持修改，已跳过",
            )
    for member_id, existing in sorted(state.members.items()):
        if member_id not in yaml_open_ids:
            b.note(
                ChangeKind.SKIP,
                "member",
                existing.display_name,
                "不在 team.yaml 中：不删除",
            )

    final_roles = [m.role for m in state.members.values()] + [
        m.role for alias, m in spec.members.items() if open_ids[alias] not in state.members
    ]
    if PM_ROLE not in final_roles:
        errors.append("no member has role 'pm': approvals and team syncs would have no owner")

    # Tasks: dependencies must resolve against yaml or existing tasks.
    yaml_task_ids = {task.id for task in spec.tasks}
    for task in spec.tasks:
        unknown = [d for d in task.depends_on if d not in yaml_task_ids and d not in state.tasks]
        if unknown:
            errors.append(f"task {task.id!r} depends on unknown tasks: {', '.join(unknown)}")
    if errors:
        raise TeamImportError(errors)

    new_tasks = _order_new_tasks([t for t in spec.tasks if t.id not in state.tasks])
    for task in new_tasks:
        b.events.append(
            TaskCreatedEvent(
                **b.stamp(),
                event_id=f"team-import:{spec.project}:task:{task.id}",
                event_type="task.created",
                source=EventSource.TASK,
                payload=TaskCreatedPayload(
                    task_id=task.id,
                    title=task.title,
                    owner_id=open_ids[task.owner],
                    workflow_status=task.status,
                    deadline=task.deadline,
                    dependencies=task.depends_on,
                ),
            )
        )
        b.note(ChangeKind.ADD, "task", task.id, f"{task.title}  owner={task.owner}")

    baseline = imported_baseline(history)
    for task in spec.tasks:
        current = state.tasks.get(task.id)
        if current is not None:
            _plan_task_update(b, task, current, open_ids, baseline.get(task.id, {}))

    for task_id in sorted(state.tasks.keys() - yaml_task_ids):
        b.note(ChangeKind.SKIP, "task", task_id, "不在 team.yaml 中：不删除")

    return ImportPlan(tuple(b.events), tuple(b.changes), tuple(warnings))


def _plan_task_update(
    b: _Builder,
    task: TaskSpec,
    current: TaskState,
    open_ids: Mapping[str, str],
    base: Mapping[str, Any],
) -> None:
    desired = {
        "title": task.title,
        "owner_id": open_ids[task.owner],
        "deadline": task.deadline,
        "workflow_status": task.status,
    }
    labels = {
        "title": "标题",
        "owner_id": "负责人",
        "deadline": "截止",
        "workflow_status": "状态",
    }
    applied: dict[str, Any] = {}
    for name, want in desired.items():
        have = getattr(current, name)
        if want == have:
            continue
        was = base.get(name, _MISSING)
        if name == "owner_id":
            alias_by_open_id = {open_id: alias for alias, open_id in open_ids.items()}
            shown_want, shown_have = task.owner, alias_by_open_id.get(have, have)
        else:
            shown_want, shown_have = _fmt(want), _fmt(have)
        if was is not _MISSING and want == was:
            # team.yaml is unchanged since the last import; the runtime value wins.
            b.note(
                ChangeKind.KEEP,
                "task",
                task.id,
                f"{labels[name]}运行时已改为 {shown_have}，team.yaml 仍是 {shown_want}："
                "保留运行时的值",
            )
            continue
        if was is not _MISSING and have != was:
            b.note(
                ChangeKind.SKIP,
                "task",
                task.id,
                f"{labels[name]}冲突：运行时已改为 {shown_have}，team.yaml 改为 {shown_want}："
                "未修改，请核对后更新 team.yaml",
            )
            continue
        if name == "deadline" and want is None:
            b.note(ChangeKind.SKIP, "task", task.id, "不支持清除截止时间，已跳过")
            continue
        applied[name] = want
        b.note(ChangeKind.UPDATE, "task", task.id, f"{labels[name]} {shown_have} → {shown_want}")

    if set(task.depends_on) != set(current.dependencies):
        b.note(
            ChangeKind.SKIP,
            "task",
            task.id,
            f"依赖 {list(current.dependencies)} → {list(task.depends_on)}：不支持修改，已跳过",
        )

    fields = {k: v for k, v in applied.items() if k != "workflow_status"}
    if fields:
        b.events.append(
            TaskUpdatedEvent(
                **b.stamp(),
                event_id=f"team-import:{b.project_id}:{b.run_id}:task:{task.id}:updated",
                event_type="task.updated",
                source=EventSource.TASK,
                payload=TaskUpdatedPayload(task_id=task.id, **fields),
            )
        )
    if "workflow_status" in applied:
        b.events.append(
            TaskWorkflowChangedEvent(
                **b.stamp(),
                event_id=f"team-import:{b.project_id}:{b.run_id}:task:{task.id}:workflow",
                event_type="task.workflow_changed",
                source=EventSource.TASK,
                payload=TaskWorkflowChangedPayload(
                    task_id=task.id,
                    from_status=current.workflow_status,
                    to_status=applied["workflow_status"],
                ),
            )
        )

"""team.yaml parsing, import planning, and application (ADR-0009)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orgpilot.domain.enums import WorkflowStatus
from orgpilot.events.models import (
    EventSource,
    OrgEvent,
    TaskCreatedEvent,
    TaskCreatedPayload,
    TaskUpdatedEvent,
    TaskUpdatedPayload,
)
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.gateway.service import GatewayService
from orgpilot.state.projector import OrgProjector
from orgpilot.storage.database import Database
from orgpilot.team import (
    ChangeKind,
    ImportPlan,
    TeamImportError,
    TeamSpec,
    import_team,
    parse_team_spec,
    plan_import,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=SHANGHAI)
PROJECT = "pay-v2"
OPEN_IDS = {"wang": "ou_wang", "alice": "ou_alice", "bob": "ou_bob"}
DIRECTORY = {
    "wang@example.com": "ou_wang",
    "alice@example.com": "ou_alice",
    "bob@example.com": "ou_bob",
}

BASE_YAML = """
project: pay-v2
members:
  wang: {email: wang@example.com, name: 王鹏飞, role: PM}
  alice: {email: alice@example.com, name: Alice, role: engineer}
  bob: {email: bob@example.com, name: Bob, role: lead}
tasks:
  - id: task-checkout
    title: 收银台前端结账
    owner: bob
    deadline: 2026-10-27 18:00
    depends_on: [task-payment]
  - id: task-payment
    title: 支付SDK接入
    owner: alice
    status: doing
    deadline: 2026-10-24 18:00
"""


def _spec(text: str = BASE_YAML) -> TeamSpec:
    return parse_team_spec(text, SHANGHAI)


def _project(history: list[OrgEvent]) -> OrgProjector:
    projector = OrgProjector(project_id=PROJECT)
    for event in history:
        projector.apply(event)
    return projector


def _plan(spec: TeamSpec, history: list[OrgEvent], now: datetime = NOW) -> ImportPlan:
    state = _project(history).state
    return plan_import(spec, state, OPEN_IDS, history, now=now, digest="abc123def456")


def _runtime_deadline_change(task_id: str, deadline: datetime, event_id: str) -> TaskUpdatedEvent:
    return TaskUpdatedEvent(
        project_id=PROJECT,
        event_id=event_id,
        event_type="task.updated",
        source=EventSource.TASK,
        source_ref="approval:apr-1",
        occurred_at=NOW,
        received_at=NOW,
        payload=TaskUpdatedPayload(task_id=task_id, deadline=deadline),
    )


def _deadline(day: int) -> datetime:
    return datetime(2026, 10, day, 18, 0, tzinfo=SHANGHAI)


# --- team.yaml parsing ---------------------------------------------------------------


def test_parse_localizes_naive_deadlines_and_normalizes_roles() -> None:
    spec = _spec()

    assert spec.members["wang"].role == "pm"
    payment = next(t for t in spec.tasks if t.id == "task-payment")
    assert payment.deadline == _deadline(24)
    assert payment.deadline.tzinfo == SHANGHAI
    assert payment.status is WorkflowStatus.DOING


def test_parse_keeps_explicit_offsets() -> None:
    spec = _spec(BASE_YAML.replace("2026-10-24 18:00", "2026-10-24T10:00:00+00:00"))

    payment = next(t for t in spec.tasks if t.id == "task-payment")
    assert payment.deadline == _deadline(24)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (
            "project: p\nmembers:\n  a: {email: a@x.io, name: A, role: pm}\n"
            "  a: {email: b@x.io, name: B, role: pm}\n",
            "duplicate key 'a'",
        ),
        (
            "project: p\nmembers:\n  a: {email: A@x.io, name: A, role: pm}\n"
            "  b: {email: a@X.io, name: B, role: pm}\n",
            "share email",
        ),
        (
            "project: p\nmembers:\n  a: {email: a@x.io, name: A, role: pm}\ntasks:\n"
            "  - {id: t, title: T, owner: a}\n  - {id: t, title: T2, owner: a}\n",
            "appears more than once",
        ),
        (
            "project: p\nmembers:\n  a: {email: a@x.io, name: A, role: pm}\ntasks:\n"
            "  - {id: t, title: T, owner: ghost}\n",
            "owner 'ghost' is not a member alias",
        ),
        (
            "project: p\nmembers:\n  a: {email: a@x.io, name: A, role: pm}\ntasks:\n"
            "  - {id: t, title: T, owner: a, deadline: 2026-10-24}\n",
            "needs a time of day",
        ),
        (
            "project: p\nmembers:\n  a: {email: a@x.io, name: A, role: pm}\ntasks:\n"
            "  - {id: t, title: T, owner: a, deadline: '2026-10-24'}\n",
            "needs a time of day",
        ),
        (
            "project: p\nmembers:\n  a: {email: a@x.io, name: A, role: pm, team: x}\n",
            "Extra inputs are not permitted",
        ),
        (
            "project: p\nmembers:\n  a: {email: a@x.io, name: A, role: pm}\ntasks:\n"
            "  - {id: t, title: T, owner: a, depends_on: [t]}\n",
            "cannot depend on itself",
        ),
        ("project: p\nmembers:\n  a b: {email: a@x.io, name: A, role: pm}\n", "alias"),
        ("project: p\nmembers:\n  a: {email: not-an-email, name: A, role: pm}\n", "email"),
        ("- just\n- a list\n", "must be a mapping"),
        ("project: [unclosed\n", "invalid YAML"),
    ],
)
def test_parse_rejects_invalid_team_yaml(text: str, fragment: str) -> None:
    with pytest.raises(TeamImportError) as exc_info:
        _spec(text)
    assert fragment in str(exc_info.value)


# --- planning ------------------------------------------------------------------------


def test_fresh_import_registers_members_then_tasks_in_dependency_order() -> None:
    plan = _plan(_spec(), [])

    assert [e.event_type for e in plan.events] == [
        "member.registered",
        "member.registered",
        "member.registered",
        "task.created",
        "task.created",
    ]
    created = [e.payload.task_id for e in plan.events if e.event_type == "task.created"]
    assert created == ["task-payment", "task-checkout"]
    assert plan.events[0].event_id == "team-import:pay-v2:member:ou_wang"
    assert plan.events[3].event_id == "team-import:pay-v2:task:task-payment"
    assert all(e.source_ref == "team-import@abc123def456" for e in plan.events)
    assert all(e.actor_id is None for e in plan.events)
    assert any("'bob' has role 'lead'" in w for w in plan.warnings)
    _project(list(plan.events))  # the batch projects cleanly


def test_reimporting_the_same_file_plans_nothing() -> None:
    history = list(_plan(_spec(), []).events)

    plan = _plan(_spec(), history, now=NOW + timedelta(hours=1))

    assert plan.events == ()
    assert plan.changes == ()


def test_plan_requires_a_pm() -> None:
    with pytest.raises(TeamImportError, match="no member has role 'pm'"):
        _plan(_spec(BASE_YAML.replace("role: PM", "role: engineer")), [])


def test_plan_rejects_unknown_dependencies_and_cycles() -> None:
    unknown = BASE_YAML.replace("[task-payment]", "[task-ghost]")
    with pytest.raises(TeamImportError, match="unknown tasks: task-ghost"):
        _plan(_spec(unknown), [])

    cyclic = BASE_YAML.replace(
        "    status: doing\n", "    status: doing\n    depends_on: [task-checkout]\n"
    )
    with pytest.raises(TeamImportError, match="dependency cycle among new tasks"):
        _plan(_spec(cyclic), [])


def test_changed_fields_become_targeted_update_events() -> None:
    history = list(_plan(_spec(), []).events)
    edited = (
        BASE_YAML.replace("2026-10-24 18:00", "2026-10-25 18:00")
        .replace("status: doing", "status: review")
        .replace("    owner: bob\n", "    owner: alice\n")
    )

    plan = _plan(_spec(edited), history, now=NOW + timedelta(hours=1))

    by_type = {e.event_type: e for e in plan.events}
    assert set(by_type) == {"task.updated", "task.workflow_changed"}
    updates = [e for e in plan.events if e.event_type == "task.updated"]
    assert {u.payload.task_id: u.payload for u in updates}.keys() == {
        "task-payment",
        "task-checkout",
    }
    payment_update = next(u for u in updates if u.payload.task_id == "task-payment")
    assert payment_update.payload.deadline == _deadline(25)
    assert payment_update.payload.title is None
    assert by_type["task.workflow_changed"].payload.from_status is WorkflowStatus.DOING
    assert by_type["task.workflow_changed"].payload.to_status is WorkflowStatus.REVIEW
    assert {c.kind for c in plan.changes} == {ChangeKind.UPDATE}
    _project(history + list(plan.events))


def test_runtime_change_wins_when_team_yaml_is_unchanged() -> None:
    """Three-way merge: re-importing must not revert an approved reschedule."""
    history = list(_plan(_spec(), []).events)
    history.append(_runtime_deadline_change("task-payment", _deadline(26), "evt-approved"))

    plan = _plan(_spec(), history, now=NOW + timedelta(hours=1))

    assert plan.events == ()
    assert [(c.kind, c.key) for c in plan.changes] == [(ChangeKind.KEEP, "task-payment")]


def test_conflicting_runtime_and_yaml_changes_are_skipped() -> None:
    history = list(_plan(_spec(), []).events)
    history.append(_runtime_deadline_change("task-payment", _deadline(26), "evt-approved"))
    edited = BASE_YAML.replace("2026-10-24 18:00", "2026-10-28 18:00")

    plan = _plan(_spec(edited), history, now=NOW + timedelta(hours=1))

    assert plan.events == ()
    assert [c.kind for c in plan.changes] == [ChangeKind.SKIP]
    assert "冲突" in plan.changes[0].detail


def test_yaml_change_applies_to_task_created_outside_imports() -> None:
    demo_task = TaskCreatedEvent(
        project_id=PROJECT,
        event_id="evt-demo-task",
        event_type="task.created",
        source=EventSource.TASK,
        source_ref="auto-bootstrap",
        occurred_at=NOW,
        received_at=NOW,
        payload=TaskCreatedPayload(
            task_id="task-payment", title="旧标题", owner_id="ou_alice", deadline=_deadline(20)
        ),
    )
    members = [e for e in _plan(_spec(), []).events if e.event_type == "member.registered"]

    plan = _plan(_spec(), [*members, demo_task])

    payment_events = [e for e in plan.events if getattr(e.payload, "task_id", "") == "task-payment"]
    assert {e.event_type for e in payment_events} == {"task.updated", "task.workflow_changed"}


def test_alternating_imports_never_collide_on_event_ids() -> None:
    """A→B→A→B through imports: content-derived ids would make the 4th a duplicate."""
    a, b = _spec(), _spec(BASE_YAML.replace("2026-10-24 18:00", "2026-10-25 18:00"))
    history = list(_plan(a, []).events)
    seen_ids = {e.event_id for e in history}
    for step, spec in enumerate((b, a, b), start=1):
        plan = _plan(spec, history, now=NOW + timedelta(minutes=step))
        assert len(plan.events) == 1
        assert plan.events[0].event_id not in seen_ids
        seen_ids.add(plan.events[0].event_id)
        history.extend(plan.events)

    assert _project(history).state.tasks["task-payment"].deadline == _deadline(25)


def test_unsupported_changes_are_reported_not_applied() -> None:
    history = list(_plan(_spec(), []).events)
    edited = (
        BASE_YAML.replace("name: Alice", "name: Alice Z")
        .replace("role: lead", "role: qa")
        .replace("    deadline: 2026-10-27 18:00\n", "")
        .replace("depends_on: [task-payment]", "depends_on: []")
    )
    edited = edited.replace("  bob: {email: bob@example.com, name: Bob, role: qa}\n", "")
    edited = edited.replace("owner: bob", "owner: alice")
    # bob leaves the file; task-checkout is reassigned so the file stays valid.
    open_ids = {k: v for k, v in OPEN_IDS.items() if k != "bob"}
    state = _project(history).state

    plan = plan_import(
        _spec(edited), state, open_ids, history, now=NOW + timedelta(hours=1), digest="d2"
    )

    skips = [c.detail for c in plan.skipped]
    assert any("名字 Alice → Alice Z" in s for s in skips)
    assert any("不支持清除截止时间" in s for s in skips)
    assert any("依赖" in s for s in skips)
    assert any(c.entity == "member" and "不删除" in c.detail for c in plan.skipped)
    assert [e.event_type for e in plan.events] == ["task.updated"]


# --- applying ------------------------------------------------------------------------


@pytest.fixture
async def service() -> GatewayService:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    yield GatewayService(db)
    await db.close()


async def test_import_resolves_emails_and_persists_idempotently(service: GatewayService) -> None:
    directory = MockFeishuClient(email_directory=DIRECTORY)

    dry = await import_team(service, _spec(), directory, digest="d1", now=NOW, dry_run=True)
    assert dry.applied is False
    assert await service.event_store.get_events(PROJECT) == []

    first = await import_team(service, _spec(), directory, digest="d1", now=NOW)
    assert first.applied is True
    assert first.open_ids == OPEN_IDS
    events = await service.event_store.get_events(PROJECT)
    assert len(events) == 5

    second = await import_team(
        service, _spec(), directory, digest="d1", now=NOW + timedelta(hours=1)
    )
    assert second.applied is False
    assert len(await service.event_store.get_events(PROJECT)) == 5

    state = (await service.get_or_replay_agent(PROJECT)).projector.state
    assert state.members["ou_wang"].role == "pm"
    assert state.tasks["task-checkout"].owner_id == "ou_bob"


async def test_import_aborts_when_any_email_is_unresolved(service: GatewayService) -> None:
    directory = MockFeishuClient(email_directory={"wang@example.com": "ou_wang"})

    with pytest.raises(TeamImportError) as exc_info:
        await import_team(service, _spec(), directory, digest="d1", now=NOW)

    message = str(exc_info.value)
    assert "'alice' <alice@example.com>" in message
    assert "'bob' <bob@example.com>" in message
    assert "企业邮箱" in message
    assert await service.event_store.get_events(PROJECT) == []


async def test_import_rejects_two_aliases_for_one_feishu_user(service: GatewayService) -> None:
    directory = MockFeishuClient(email_directory={**DIRECTORY, "bob@example.com": "ou_alice"})

    with pytest.raises(TeamImportError, match="'alice', 'bob' are the same Feishu user"):
        await import_team(service, _spec(), directory, digest="d1", now=NOW)


async def test_import_writes_nothing_when_batch_validation_fails(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import orgpilot.team.importer as importer

    real_plan_import = importer.plan_import

    def plan_with_orphan_task(*args, **kwargs) -> ImportPlan:
        plan = real_plan_import(*args, **kwargs)
        orphan = TaskCreatedEvent(
            project_id=PROJECT,
            event_id="evt-orphan",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="team-import@x",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskCreatedPayload(task_id="task-orphan", title="孤儿", owner_id="ou_ghost"),
        )
        return ImportPlan((*plan.events, orphan), plan.changes, plan.warnings)

    monkeypatch.setattr(importer, "plan_import", plan_with_orphan_task)
    directory = MockFeishuClient(email_directory=DIRECTORY)

    with pytest.raises(TeamImportError, match="未写入任何事件"):
        await import_team(service, _spec(), directory, digest="d1", now=NOW)

    assert await service.event_store.get_events(PROJECT) == []

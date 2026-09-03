"""Tests for NL task creation and reassignment behind approval gates (M3 finale)."""

from datetime import datetime, timedelta

import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.coordination.approval import ApprovalManager
from orgpilot.coordination.tasks import TaskManager
from orgpilot.domain.enums import ApprovalStatus
from orgpilot.domain.errors import DomainInvariantError
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
)
from orgpilot.extraction.models import ExtractionResult, IntentHint, TaskProposal
from orgpilot.state.projector import OrgProjector

NOW = datetime.fromisoformat("2026-09-10T15:00:00+08:00")
PROJECT = "proj-tasks"


def _setup(*task_titles: str) -> OrgProjector:
    projector = OrgProjector(project_id=PROJECT)
    for i, (mid, role) in enumerate(
        [("carol", "pm"), ("alice", "engineer"), ("bob", "engineer"), ("david", "qa")], start=1
    ):
        projector.apply(
            MemberRegisteredEvent(
                project_id=PROJECT,
                event_id=f"evt-m-{i}",
                event_type="member.registered",
                source=EventSource.HUMAN,
                source_ref="setup",
                occurred_at=NOW,
                received_at=NOW,
                payload={"member_id": mid, "display_name": mid.title(), "role": role},
            )
        )
    for i, title in enumerate(task_titles, start=100):
        projector.apply(
            TaskCreatedEvent(
                project_id=PROJECT,
                event_id=f"evt-t-{i}",
                event_type="task.created",
                source=EventSource.TASK,
                source_ref="setup",
                occurred_at=NOW,
                received_at=NOW,
                payload={"task_id": f"task-{i}", "title": title, "owner_id": "alice"},
            )
        )
    return projector


def _manager(projector: OrgProjector) -> TaskManager:
    return TaskManager(
        adapter=MockCollaborationAdapter(project_id=PROJECT),
        approval_manager=ApprovalManager(),
        reference_timezone="Asia/Shanghai",
    )


def _create_result(
    title: str | None = "网关压测脚本",
    owner_name: str | None = "David",
    deadline_expr: str | None = "周五前",
    hints: IntentHint | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        is_actionable=False,
        intent=None,
        task_proposal=TaskProposal(
            operation="create",
            title=title,
            owner_name=owner_name,
            task_ref=None,
            deadline_expr=deadline_expr,
        ),
        hints=hints or IntentHint(),
    )


def test_pm_create_proposal_is_gated_not_executed() -> None:
    """Governance red line: NL creation never lands directly — it proposes."""
    projector = _setup()
    outcome = _manager(projector).handle_task_create_intent(
        message="新增一个任务：网关压测脚本，由David负责，周五前完成",
        actor_id="carol",
        result=_create_result(),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "proposed"
    assert projector.state.tasks == {}  # nothing lands before approval
    assert outcome.approval is not None
    assert outcome.approval.approver_id == "carol"
    assert outcome.approval.status is ApprovalStatus.PENDING
    payload = outcome.approval.proposed_command.payload
    assert payload["proposal_kind"] == "task_create"
    assert payload["task_title"] == "网关压测脚本"
    assert payload["owner_id"] == "david"
    assert payload["deadline"] is not None
    # The card command rides the approval path to the approver.
    assert outcome.outbound[0].action_type.value == "task_create"
    assert outcome.outbound[0].targets == ("carol",)


def test_member_cannot_propose_task_creation() -> None:
    projector = _setup()
    outcome = _manager(projector).handle_task_create_intent(
        message="新增任务：联调环境修复",
        actor_id="bob",
        result=_create_result(title="联调环境修复", owner_name="alice"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "declined"
    assert "无权限" in (outcome.bot_reply or "")
    assert projector.state.tasks == {}


def test_create_with_unresolvable_owner_clarifies() -> None:
    projector = _setup()
    outcome = _manager(projector).handle_task_create_intent(
        message="新增任务：神秘任务，由张三负责",
        actor_id="carol",
        result=_create_result(title="神秘任务", owner_name="张三"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "clarify"
    assert "张三" in (outcome.bot_reply or "")


def test_hallucinated_create_slots_cannot_reach_approval() -> None:
    projector = _setup()
    hallucinated_title = _manager(projector).handle_task_create_intent(
        message="新增任务，由David负责",
        actor_id="carol",
        result=_create_result(title="模型凭空生成的任务", owner_name="David"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert hallucinated_title.kind == "clarify"
    assert hallucinated_title.approval is None

    hallucinated_owner = _manager(projector).handle_task_create_intent(
        message="新增任务：网关压测脚本，由张三负责",
        actor_id="carol",
        result=_create_result(title="网关压测脚本", owner_name="David"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert hallucinated_owner.kind == "clarify"
    assert hallucinated_owner.approval is None


def test_create_with_duplicate_title_clarifies() -> None:
    projector = _setup("收银台前端结账")
    outcome = _manager(projector).handle_task_create_intent(
        message="新增任务：收银台前端结账，由David负责",
        actor_id="carol",
        result=_create_result(title="收银台前端结账", owner_name="David"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "clarify"
    assert "已存在" in (outcome.bot_reply or "")


def test_approved_create_settles_into_task_created_event() -> None:
    projector = _setup()
    manager = _manager(projector)
    proposal = manager.handle_task_create_intent(
        message="新增一个任务：网关压测脚本，由David负责，周五前完成",
        actor_id="carol",
        result=_create_result(),
        state=projector.state,
        occurred_at=NOW,
    )
    request = proposal.approval
    manager.approval_manager.approve(request.approval_id, "carol", NOW)

    settled = manager.settle_approval(projector.state, request, "carol", NOW)
    assert settled is not None and settled.kind == "created"
    for event in settled.events:
        projector.apply(event)

    task_id = request.proposed_command.payload["task_id"]
    task = projector.state.tasks[task_id]
    assert task.title == "网关压测脚本"
    assert task.owner_id == "david"
    assert task.deadline is not None
    # The new owner is notified through the directive transport.
    assert settled.outbound[0].action_type.value == "send_directive"
    assert settled.outbound[0].targets == ("david",)

    # Settling twice is a no-op (consumed).
    again = manager.settle_approval(projector.state, request, "carol", NOW)
    assert again is None


def test_rejected_create_notifies_and_marks_consumed() -> None:
    projector = _setup()
    manager = _manager(projector)
    proposal = manager.handle_task_create_intent(
        message="新增任务：网关压测脚本，由David负责",
        actor_id="carol",
        result=_create_result(),
        state=projector.state,
        occurred_at=NOW,
    )
    request = proposal.approval
    manager.approval_manager.reject(request.approval_id, "carol", "不需要", NOW)

    settled = manager.settle_approval(projector.state, request, "carol", NOW)
    assert settled is not None and settled.kind == "rejected"
    assert settled.events == []
    assert projector.state.tasks == {}
    assert request.consumed is True


def test_reassign_proposal_and_settlement() -> None:
    projector = _setup("收银台前端结账")
    manager = _manager(projector)
    result = ExtractionResult(
        is_actionable=False,
        task_proposal=TaskProposal(
            operation="reassign",
            title=None,
            owner_name="David",
            task_ref="收银台前端结账",
            deadline_expr=None,
        ),
        hints=IntentHint(),
    )
    proposal = manager.handle_task_reassign_intent(
        message="把收银台前端结账转给David",
        actor_id="carol",
        result=result,
        state=projector.state,
        occurred_at=NOW,
    )
    assert proposal.kind == "proposed"
    payload = proposal.approval.proposed_command.payload
    assert payload["proposal_kind"] == "task_reassign"
    assert payload["owner_id"] == "david"
    assert payload["previous_owner_id"] == "alice"

    manager.approval_manager.approve(proposal.approval.approval_id, "carol", NOW)
    settled = manager.settle_approval(projector.state, proposal.approval, "carol", NOW)
    assert settled is not None and settled.kind == "reassigned"
    for event in settled.events:
        projector.apply(event)
    assert projector.state.tasks["task-100"].owner_id == "david"
    # Both the new and the previous owner are notified.
    targets = {cmd.targets[0] for cmd in settled.outbound}
    assert targets == {"david", "alice"}

    # The kernel rejects reassigning to an unknown member at projection time too.
    with pytest.raises(DomainInvariantError):
        projector.apply(
            TaskUpdatedEvent(
                project_id=PROJECT,
                event_id="evt-bad-owner",
                event_type="task.updated",
                source=EventSource.TASK,
                source_ref="t",
                occurred_at=NOW + timedelta(hours=1),
                received_at=NOW + timedelta(hours=1),
                payload={"task_id": "task-100", "owner_id": "ghost"},
            )
        )


def test_deadline_change_proposal_analyzes_dependencies_and_settles() -> None:
    projector = _setup()
    for task_id, title, owner_id, deadline, dependencies in (
        ("task-upstream", "支付SDK接入", "alice", NOW + timedelta(days=1), ()),
        (
            "task-downstream",
            "收银台前端结账",
            "bob",
            NOW + timedelta(days=2),
            ("task-upstream",),
        ),
        (
            "task-qa",
            "支付全链路验收",
            "david",
            NOW + timedelta(days=4),
            ("task-downstream",),
        ),
    ):
        projector.apply(
            TaskCreatedEvent(
                project_id=PROJECT,
                event_id=f"evt-{task_id}",
                event_type="task.created",
                source=EventSource.TASK,
                source_ref="setup",
                occurred_at=NOW,
                received_at=NOW,
                payload={
                    "task_id": task_id,
                    "title": title,
                    "owner_id": owner_id,
                    "deadline": deadline,
                    "dependencies": dependencies,
                },
            )
        )

    manager = _manager(projector)
    result = ExtractionResult(
        is_actionable=False,
        task_proposal=TaskProposal(
            operation="deadline_change",
            title=None,
            owner_name=None,
            task_ref="支付SDK接入",
            deadline_expr="后天下午5点",
        ),
        hints=IntentHint(mentioned_task_ids=("task-upstream",), raw_time_expr="后天下午5点"),
    )
    proposal = manager.handle_deadline_change_intent(
        message="支付SDK接入截止时间改到后天下午5点",
        actor_id="carol",
        result=result,
        state=projector.state,
        occurred_at=NOW,
    )

    assert proposal.kind == "proposed"
    assert proposal.approval is not None
    assert proposal.outbound[0].targets == ("carol",)
    payload = proposal.approval.proposed_command.payload
    assert payload["proposal_kind"] == "deadline_change"
    assert payload["impacted_tasks"] == ["task-downstream", "task-qa"]
    assert payload["conflicting_tasks"] == ["task-downstream"]
    assert payload["risk_level"] == "HIGH"
    assert projector.state.tasks["task-upstream"].deadline == NOW + timedelta(days=1)

    manager.approval_manager.approve(proposal.approval.approval_id, "carol", NOW)
    settled = manager.settle_approval(projector.state, proposal.approval, "carol", NOW)
    assert settled is not None and settled.kind == "deadline_changed"
    for event in settled.events:
        projector.apply(event)
    assert projector.state.tasks["task-upstream"].deadline == datetime.fromisoformat(
        payload["new_deadline"]
    )
    assert {command.targets[0] for command in settled.outbound} == {"alice", "bob", "david"}


def test_deadline_change_requires_task_time_and_privilege() -> None:
    projector = _setup("支付SDK接入")
    manager = _manager(projector)
    missing_task = ExtractionResult(
        is_actionable=False,
        task_proposal=TaskProposal(
            operation="deadline_change",
            task_ref=None,
            deadline_expr="后天下午5点",
        ),
        hints=IntentHint(raw_time_expr="后天下午5点"),
    )
    assert (
        manager.handle_deadline_change_intent(
            "截止时间改到后天下午5点", "carol", missing_task, projector.state, NOW
        ).kind
        == "clarify"
    )

    missing_time = ExtractionResult(
        is_actionable=False,
        task_proposal=TaskProposal(operation="deadline_change", task_ref="支付SDK接入"),
        hints=IntentHint(mentioned_task_ids=("task-100",)),
    )
    assert (
        manager.handle_deadline_change_intent(
            "支付SDK接入需要改期", "carol", missing_time, projector.state, NOW
        ).kind
        == "clarify"
    )
    assert (
        manager.handle_deadline_change_intent(
            "支付SDK接入截止时间改到后天下午5点",
            "alice",
            missing_time,
            projector.state,
            NOW,
        ).kind
        == "declined"
    )


def test_reassign_to_current_owner_is_noop() -> None:
    projector = _setup("收银台前端结账")
    result = ExtractionResult(
        is_actionable=False,
        task_proposal=TaskProposal(
            operation="reassign",
            title=None,
            owner_name="Alice",
            task_ref="收银台前端结账",
            deadline_expr=None,
        ),
        hints=IntentHint(),
    )
    outcome = _manager(projector).handle_task_reassign_intent(
        message="把收银台前端结账转给Alice",
        actor_id="carol",
        result=result,
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "none"
    assert "无需改派" in (outcome.bot_reply or "")


def test_member_reassign_is_declined() -> None:
    projector = _setup("收银台前端结账")
    result = ExtractionResult(
        is_actionable=False,
        task_proposal=TaskProposal(
            operation="reassign",
            title=None,
            owner_name="David",
            task_ref="收银台前端结账",
            deadline_expr=None,
        ),
        hints=IntentHint(),
    )
    outcome = _manager(projector).handle_task_reassign_intent(
        message="把收银台前端结账转给David",
        actor_id="alice",
        result=result,
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "declined"


def test_create_without_title_clarifies() -> None:
    projector = _setup()
    outcome = _manager(projector).handle_task_create_intent(
        message="新增任务",
        actor_id="carol",
        result=_create_result(title=None, owner_name="David"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "clarify"
    assert "名称" in (outcome.bot_reply or "")


def test_reassign_with_unknown_task_clarifies() -> None:
    projector = _setup("收银台前端结账")
    result = ExtractionResult(
        is_actionable=False,
        task_proposal=TaskProposal(
            operation="reassign",
            title=None,
            owner_name="David",
            task_ref="不存在的任务",
            deadline_expr=None,
        ),
        hints=IntentHint(),
    )
    outcome = _manager(projector).handle_task_reassign_intent(
        message="把不存在的任务转给David",
        actor_id="carol",
        result=result,
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "clarify"
    assert "没认出" in (outcome.bot_reply or "")


def test_reassign_with_unknown_owner_clarifies() -> None:
    projector = _setup("收银台前端结账")
    result = ExtractionResult(
        is_actionable=False,
        task_proposal=TaskProposal(
            operation="reassign",
            title=None,
            owner_name="张三",
            task_ref="收银台前端结账",
            deadline_expr=None,
        ),
        hints=IntentHint(),
    )
    outcome = _manager(projector).handle_task_reassign_intent(
        message="把收银台前端结账转给张三",
        actor_id="carol",
        result=result,
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "clarify"
    assert "张三" in (outcome.bot_reply or "")


def test_settle_ignores_non_task_approvals() -> None:
    projector = _setup()
    manager = _manager(projector)
    from orgpilot.domain.enums import ActionType
    from orgpilot.domain.models import ActionCommand, CoordinationAction

    action = CoordinationAction(
        action_id="action:reschedule",
        action_type=ActionType.PROPOSE_RESCHEDULE,
        targets=("carol",),
        reason_refs=(),
        expected_effect="reschedule",
        payload={"task_id": "task-100", "new_deadline": NOW.isoformat()},
    )
    command = ActionCommand(
        command_id="cmd:1",
        action_id="action:reschedule",
        action_type=ActionType.PROPOSE_RESCHEDULE,
        targets=("carol",),
        payload=action.payload,
        idempotency_key="idem:1",
        created_at=NOW,
    )
    request = manager.approval_manager.create_request("case:1", action, command, "carol", NOW)
    manager.approval_manager.approve(request.approval_id, "carol", NOW)
    assert manager.settle_approval(projector.state, request, "carol", NOW) is None

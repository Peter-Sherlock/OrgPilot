"""Tests for FeishuCollaborationAdapter command execution and audit logging."""

from datetime import datetime

import pytest

from orgpilot.domain.enums import ActionType, CommandStatus
from orgpilot.domain.models import ActionCommand
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import MockFeishuClient

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


def test_feishu_adapter_send_private_message() -> None:
    client = MockFeishuClient()
    adapter = FeishuCollaborationAdapter(client=client, project_id="feishu-proj")

    cmd = ActionCommand(
        command_id="cmd:1",
        action_id="act:1",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("ou_alice",),
        payload={"task_id": "backend_api", "title": "Backend API", "reason": "delay risk"},
        idempotency_key="idem:1",
        created_at=NOW,
    )

    result = adapter.execute(cmd)
    assert result.status is CommandStatus.SUCCESS
    assert len(client.sent_cards) == 1
    assert client.sent_cards[0]["receive_id"] == "ou_alice"
    assert len(adapter.audit_log) == 1


def test_feishu_adapter_request_approval() -> None:
    client = MockFeishuClient()
    adapter = FeishuCollaborationAdapter(client=client, project_id="feishu-proj")

    cmd = ActionCommand(
        command_id="cmd:2",
        action_id="act:2",
        action_type=ActionType.PROPOSE_RESCHEDULE,
        targets=("ou_carol_pm",),
        payload={
            "approval_id": "appr:1",
            "case_id": "case:1",
            "task_id": "backend_api",
            "proposed_deadline": "2026-09-11 17:00:00",
            "impacted_tasks": ["frontend_ui"],
        },
        idempotency_key="idem:2",
        created_at=NOW,
    )

    result = adapter.execute(cmd)
    assert result.status is CommandStatus.SUCCESS
    assert len(client.sent_cards) == 1
    assert client.sent_cards[0]["receive_id"] == "ou_carol_pm"


def _update_task_command() -> ActionCommand:
    return ActionCommand(
        command_id="cmd:3",
        action_id="act:3",
        action_type=ActionType.UPDATE_TASK,
        targets=("system",),
        payload={"task_id": "backend_api", "new_deadline": NOW.isoformat()},
        idempotency_key="idem:3",
        created_at=NOW,
    )


def test_feishu_adapter_update_task_stays_local_by_default() -> None:
    """Imported task ids are not Feishu task guids, so the default never patches Feishu."""
    client = MockFeishuClient()
    adapter = FeishuCollaborationAdapter(client=client, project_id="feishu-proj")

    result = adapter.execute(_update_task_command())
    assert result.status is CommandStatus.SUCCESS
    assert result.output["feishu_task_synced"] is False
    assert client.updated_tasks == []

    generated_events = adapter.pop_generated_events()
    assert len(generated_events) == 1
    assert generated_events[0].event_type == "task.updated"
    assert generated_events[0].payload.task_id == "backend_api"


def test_feishu_adapter_update_task_writes_back_when_enabled() -> None:
    client = MockFeishuClient()
    adapter = FeishuCollaborationAdapter(
        client=client, project_id="feishu-proj", task_writeback=True
    )

    result = adapter.execute(_update_task_command())
    assert result.status is CommandStatus.SUCCESS
    assert result.output["feishu_task_synced"] is True
    assert len(client.updated_tasks) == 1
    assert len(adapter.pop_generated_events()) == 1


def test_feishu_adapter_update_task_failure_is_reported_not_raised() -> None:
    """A rejected Feishu Task call returns FAILED and leaves OrgPilot's deadline alone."""

    class RejectingClient(MockFeishuClient):
        async def update_task_deadline(self, task_guid: str, deadline: datetime) -> dict:
            raise RuntimeError("Feishu update task failed: task not found")

    adapter = FeishuCollaborationAdapter(
        client=RejectingClient(), project_id="feishu-proj", task_writeback=True
    )

    result = adapter.execute(_update_task_command())
    assert result.status is CommandStatus.FAILED
    assert "task not found" in (result.error or "")
    assert adapter.pop_generated_events() == []


def test_feishu_adapter_notify_group() -> None:
    client = MockFeishuClient()
    adapter = FeishuCollaborationAdapter(client=client, project_id="feishu-proj")

    cmd = ActionCommand(
        command_id="cmd:4",
        action_id="act:4",
        action_type=ActionType.NOTIFY_GROUP,
        targets=("oc_dev_chat",),
        payload={
            "task_id": "backend_api",
            "new_deadline": "2026-09-11 17:00:00",
            "approved_by": "Carol (PM)",
        },
        idempotency_key="idem:4",
        created_at=NOW,
    )

    result = adapter.execute(cmd)
    assert result.status is CommandStatus.SUCCESS
    assert len(client.sent_cards) == 1
    assert client.sent_cards[0]["receive_id"] == "oc_dev_chat"


async def test_feishu_adapter_propagates_async_failure_before_reporting_success() -> None:
    class FailingClient(MockFeishuClient):
        async def send_card(self, *args, **kwargs):
            raise RuntimeError("Feishu unavailable")

    adapter = FeishuCollaborationAdapter(client=FailingClient(), project_id="feishu-proj")
    command = ActionCommand(
        command_id="cmd:failure",
        action_id="act:failure",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("ou_alice",),
        payload={"task_id": "backend_api"},
        idempotency_key="idem:failure",
        created_at=NOW,
    )

    with pytest.raises(RuntimeError, match="Feishu unavailable"):
        adapter.execute(command)
    assert adapter.audit_log == []

def test_feishu_adapter_sends_probe_and_clarification_text() -> None:
    """F2: probe inquiries and clarifications go out as text messages with {"text": ...}."""
    client = MockFeishuClient()
    adapter = FeishuCollaborationAdapter(client=client, project_id="feishu-proj")

    probe_cmd = ActionCommand(
        command_id="cmd:probe:t1",
        action_id="action:probe:t1",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("ou_alice", "ou_bob"),
        payload={"inquiry_text": "请问您负责的任务目前进展如何？", "is_sync_probe": True},
        idempotency_key="idem:probe:t1",
        created_at=NOW,
    )
    result = adapter.execute(probe_cmd)
    assert result.status is CommandStatus.SUCCESS
    assert [(m["receive_id"], m["msg_type"], m["content"]) for m in client.sent_messages] == [
        ("ou_alice", "text", {"text": "请问您负责的任务目前进展如何？"}),
        ("ou_bob", "text", {"text": "请问您负责的任务目前进展如何？"}),
    ]

    clarify_cmd = ActionCommand(
        command_id="cmd:clarify:t1",
        action_id="action:clarify:t1",
        action_type=ActionType.ASK_CLARIFICATION,
        targets=("ou_alice",),
        payload={"clarification_text": "大概什么时候能恢复？", "is_clarification": True},
        idempotency_key="idem:clarify:t1",
        created_at=NOW,
    )
    result = adapter.execute(clarify_cmd)
    assert result.status is CommandStatus.SUCCESS
    assert client.sent_messages[-1]["content"] == {"text": "大概什么时候能恢复？"}


def test_feishu_adapter_reports_failed_when_text_send_raises() -> None:
    """F2: a failed probe/clarification send must return FAILED, never silent success."""

    class FailingTextClient(MockFeishuClient):
        async def send_message(self, *args, **kwargs):
            raise RuntimeError("Feishu unavailable")

    adapter = FeishuCollaborationAdapter(client=FailingTextClient(), project_id="feishu-proj")
    probe_cmd = ActionCommand(
        command_id="cmd:probe:fail",
        action_id="action:probe:fail",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("ou_alice",),
        payload={"inquiry_text": "请问进展如何？"},
        idempotency_key="idem:probe:fail",
        created_at=NOW,
    )

    result = adapter.execute(probe_cmd)
    assert result.status is CommandStatus.FAILED
    assert result.error is not None
    assert "Feishu unavailable" in result.error
    assert adapter.audit_log[-1][1].status is CommandStatus.FAILED


def test_feishu_adapter_update_task_without_new_deadline_fails_closed() -> None:
    """A payload without new_deadline must not reschedule the task to "now"."""
    adapter = FeishuCollaborationAdapter(client=MockFeishuClient(), project_id="feishu-proj")
    cmd = _update_task_command().model_copy(update={"payload": {"task_id": "backend_api"}})

    result = adapter.execute(cmd)
    assert result.status is CommandStatus.FAILED
    assert "new_deadline" in (result.error or "")
    assert adapter.pop_generated_events() == []

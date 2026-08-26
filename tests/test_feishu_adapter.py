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


def test_feishu_adapter_update_task() -> None:
    client = MockFeishuClient()
    adapter = FeishuCollaborationAdapter(client=client, project_id="feishu-proj")

    cmd = ActionCommand(
        command_id="cmd:3",
        action_id="act:3",
        action_type=ActionType.UPDATE_TASK,
        targets=("system",),
        payload={"task_id": "backend_api", "deadline": NOW.isoformat()},
        idempotency_key="idem:3",
        created_at=NOW,
    )

    result = adapter.execute(cmd)
    assert result.status is CommandStatus.SUCCESS
    assert len(client.updated_tasks) == 1

    generated_events = adapter.pop_generated_events()
    assert len(generated_events) == 1
    assert generated_events[0].event_type == "task.updated"
    assert generated_events[0].payload.task_id == "backend_api"


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

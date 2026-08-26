"""Tests for MockCollaborationAdapter dispatch, auditing, and event simulation."""

from datetime import datetime

import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.domain.enums import ActionType, CommandStatus, HealthStatus
from orgpilot.domain.models import ActionCommand
from orgpilot.events.models import (
    EventSource,
    TaskHealthReportedEvent,
    TaskHealthReportedPayload,
)

NOW = datetime.fromisoformat("2026-09-01T10:00:00+08:00")


def test_mock_adapter_send_private_message_and_scripted_reply() -> None:
    adapter = MockCollaborationAdapter(project_id="test-proj")
    reply_event = TaskHealthReportedEvent(
        project_id="test-proj",
        event_id="evt-reply",
        event_type="task.health_reported",
        source=EventSource.MESSAGE,
        source_ref="ref",
        actor_id="alice",
        occurred_at=NOW,
        received_at=NOW,
        payload=TaskHealthReportedPayload(
            task_id="t1",
            health_status=HealthStatus.ON_TRACK,
            confidence=1.0,
        ),
    )
    adapter.script_reply("alice", reply_event)

    cmd = ActionCommand(
        command_id="cmd-1",
        action_id="act-1",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("alice",),
        idempotency_key="idem-1",
        created_at=NOW,
    )
    result = adapter.execute(cmd)
    assert result.status is CommandStatus.SUCCESS
    assert len(adapter.audit_log) == 1

    generated = adapter.pop_generated_events()
    assert len(generated) == 1
    assert generated[0].event_id == "evt-reply"
    assert len(adapter.pop_generated_events()) == 0


def test_mock_adapter_update_task_requires_approval() -> None:
    adapter = MockCollaborationAdapter(project_id="test-proj")
    unapproved_cmd = ActionCommand(
        command_id="cmd-u",
        action_id="act-u",
        action_type=ActionType.UPDATE_TASK,
        targets=("t1",),
        payload={"task_id": "t1", "new_deadline": "2026-09-15T18:00:00+08:00"},
        idempotency_key="idem-u",
        created_at=NOW,
    )
    res_unapproved = adapter.execute(unapproved_cmd)
    assert res_unapproved.status is CommandStatus.REJECTED
    assert adapter.pop_generated_events() == []

    approved_cmd = unapproved_cmd.model_copy(update={"approved_by": "carol"})
    res_approved = adapter.execute(approved_cmd)
    assert res_approved.status is CommandStatus.SUCCESS
    generated = adapter.pop_generated_events()
    assert len(generated) == 1
    assert generated[0].event_type == "task.updated"
    assert generated[0].actor_id == "carol"


def test_mock_adapter_dynamic_responder() -> None:
    adapter = MockCollaborationAdapter(project_id="test-proj")

    def custom_resp(cmd: ActionCommand):
        if "bob" in cmd.targets:
            return TaskHealthReportedEvent(
                project_id="test-proj",
                event_id="evt-bob-reply",
                event_type="task.health_reported",
                source=EventSource.MESSAGE,
                source_ref="ref",
                actor_id="bob",
                occurred_at=NOW,
                received_at=NOW,
                payload=TaskHealthReportedPayload(
                    task_id="t2",
                    health_status=HealthStatus.ON_TRACK,
                    confidence=1.0,
                ),
            )
        return None

    adapter.add_responder(custom_resp)
    cmd = ActionCommand(
        command_id="cmd-b",
        action_id="act-b",
        action_type=ActionType.ASK_CLARIFICATION,
        targets=("bob",),
        idempotency_key="idem-b",
        created_at=NOW,
    )
    adapter.execute(cmd)
    generated = adapter.pop_generated_events()
    assert len(generated) == 1
    assert generated[0].actor_id == "bob"


def test_mock_adapter_notify_group_and_approval_request() -> None:
    adapter = MockCollaborationAdapter(project_id="test-proj")
    notify_cmd = ActionCommand(
        command_id="cmd-n",
        action_id="act-n",
        action_type=ActionType.NOTIFY_GROUP,
        targets=("team-channel",),
        idempotency_key="idem-n",
        created_at=NOW,
    )
    res_notify = adapter.execute(notify_cmd)
    assert res_notify.status is CommandStatus.SUCCESS

    prop_cmd = ActionCommand(
        command_id="cmd-p",
        action_id="act-p",
        action_type=ActionType.PROPOSE_RESCHEDULE,
        targets=("carol",),
        idempotency_key="idem-p",
        created_at=NOW,
    )
    res_prop = adapter.execute(prop_cmd)
    assert res_prop.status is CommandStatus.SUCCESS


def test_adapter_unsupported_action_type() -> None:
    adapter = MockCollaborationAdapter(project_id="test-proj")
    bad_cmd = ActionCommand.model_construct(
        command_id="cmd-bad",
        action_id="act-bad",
        action_type="unknown_action",
        targets=("alice",),
        idempotency_key="idem-bad",
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="Unsupported action type"):
        adapter.execute(bad_cmd)

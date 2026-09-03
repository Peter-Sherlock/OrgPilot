"""Cross-adapter contract tests: Mock and real Feishu adapters must obey the same
canonical payload contract for every ActionType, and fail closed on missing fields."""

from datetime import datetime

import pytest

from orgpilot.adapter.contracts import (
    DeadlineUpdate,
    PayloadContractError,
    RescheduleProposal,
    parse_text,
)
from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.domain.enums import ActionType, CommandStatus
from orgpilot.domain.models import ActionCommand
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import MockFeishuClient

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")
DEADLINE = datetime.fromisoformat("2026-09-15T18:00:00+08:00")


def make_adapters() -> list[tuple[str, MockCollaborationAdapter | FeishuCollaborationAdapter]]:
    return [
        ("mock", MockCollaborationAdapter(project_id="p")),
        ("feishu", FeishuCollaborationAdapter(client=MockFeishuClient(), project_id="p")),
    ]


def command(action_type: ActionType, payload: dict) -> ActionCommand:
    return ActionCommand(
        command_id=f"cmd:{action_type.value}",
        action_id=f"act:{action_type.value}",
        action_type=action_type,
        targets=("ou_alice",),
        payload=payload,
        approved_by="ou_pm",
        idempotency_key=f"idem:{action_type.value}",
        created_at=NOW,
    )


def feishu_client(adapter: FeishuCollaborationAdapter) -> MockFeishuClient:
    return adapter.client  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("action_type", "payload"),
    [
        (ActionType.SEND_DIRECTIVE, {"text": "请今天内完成联调", "directive_id": "dir-1"}),
        (ActionType.ASK_CLARIFICATION, {"text": "请问具体阻塞在哪一步？"}),
        (ActionType.ASK_RECOVERY_ESTIMATE, {"text": "预计什么时候能恢复？", "task_id": "t1"}),
    ],
)
def test_text_actions_send_feishu_text_contract(action_type: ActionType, payload: dict) -> None:
    """P0 regression: real adapter must pass msg_type='text' and {"text": ...} content."""
    feishu = FeishuCollaborationAdapter(client=MockFeishuClient(), project_id="p")
    result = feishu.execute(command(action_type, payload))

    assert result.status is CommandStatus.SUCCESS
    assert len(feishu_client(feishu).sent_messages) == 1
    sent = feishu_client(feishu).sent_messages[0]
    assert sent["msg_type"] == "text"
    assert sent["content"] == {"text": payload["text"]}
    assert sent["receive_id"] == "ou_alice"

    for _, adapter in make_adapters():
        assert adapter.execute(command(action_type, payload)).status is CommandStatus.SUCCESS


@pytest.mark.parametrize("legacy_key", ["clarification_text", "inquiry_text"])
def test_text_alias_keys_still_parse(legacy_key: str) -> None:
    cmd = command(ActionType.SEND_DIRECTIVE, {legacy_key: "旧键文本"})
    for _, adapter in make_adapters():
        assert adapter.execute(cmd).status is CommandStatus.SUCCESS


def test_propose_reschedule_card_renders_canonical_deadline() -> None:
    payload = {
        "task_id": "backend_api",
        "new_deadline": DEADLINE.isoformat(),
        "task_title": "支付SDK接入",
        "impacted_tasks": ["frontend_ui"],
        "risk_level": "HIGH",
        "approval_id": "appr:1",
        "case_id": "case:1",
    }
    for _, adapter in make_adapters():
        result = adapter.execute(command(ActionType.PROPOSE_RESCHEDULE, payload))
        assert result.status is CommandStatus.SUCCESS

    # The real adapter renders the card with the canonical deadline.
    feishu = FeishuCollaborationAdapter(client=MockFeishuClient(), project_id="p")
    result = feishu.execute(command(ActionType.PROPOSE_RESCHEDULE, payload))
    card = feishu_client(feishu).sent_cards[0]["card"]
    assert "2026-09-15 18:00" in str(card)

    # The mock adapter passes the canonical payload through for the sandbox UI.
    mock = MockCollaborationAdapter(project_id="p")
    result = mock.execute(command(ActionType.PROPOSE_RESCHEDULE, payload))
    assert result.output["payload"]["new_deadline"] == DEADLINE.isoformat()


def test_update_task_deadline_reaches_client_and_event_identically() -> None:
    payload = {"task_id": "backend_api", "new_deadline": DEADLINE.isoformat()}

    mock = MockCollaborationAdapter(project_id="p")
    feishu = FeishuCollaborationAdapter(client=MockFeishuClient(), project_id="p")

    mock_result = mock.execute(command(ActionType.UPDATE_TASK, payload))
    feishu_result = feishu.execute(command(ActionType.UPDATE_TASK, payload))

    assert mock_result.status is CommandStatus.SUCCESS
    assert feishu_result.status is CommandStatus.SUCCESS

    events = mock.pop_generated_events()
    assert len(events) == 1
    assert events[0].payload.deadline == DEADLINE
    assert events[0].payload.task_id == "backend_api"

    updates = feishu_client(feishu).updated_tasks
    assert len(updates) == 1
    assert updates[0]["task_guid"] == "backend_api"
    assert datetime.fromisoformat(updates[0]["deadline"]) == DEADLINE


def test_update_task_missing_deadline_fails_closed_never_now() -> None:
    """P0 regression: a missing deadline must not silently become datetime.now()."""
    payload = {"task_id": "backend_api"}
    for _, adapter in make_adapters():
        result = adapter.execute(command(ActionType.UPDATE_TASK, payload))
        assert result.status is CommandStatus.FAILED
        assert "new_deadline" in (result.error or "")


def test_update_task_unparseable_deadline_fails_closed() -> None:
    with pytest.raises(PayloadContractError):
        DeadlineUpdate.from_payload({"task_id": "t", "new_deadline": "next friday"})


def test_update_task_legacy_deadline_alias_accepted() -> None:
    payload = {"task_id": "backend_api", "deadline": DEADLINE.isoformat()}
    for _, adapter in make_adapters():
        result = adapter.execute(command(ActionType.UPDATE_TASK, payload))
        assert result.status is CommandStatus.SUCCESS


def test_reschedule_proposal_missing_fields_rejected() -> None:
    with pytest.raises(PayloadContractError):
        RescheduleProposal.from_payload({"new_deadline": DEADLINE.isoformat()})
    with pytest.raises(PayloadContractError):
        RescheduleProposal.from_payload({"task_id": "t1"})
    with pytest.raises(PayloadContractError):
        DeadlineUpdate.from_payload({})


def test_parse_text_prefers_canonical_and_ignores_empty() -> None:
    assert parse_text({"text": "a", "inquiry_text": "b"}) == "a"
    assert parse_text({"inquiry_text": "b"}) == "b"
    assert parse_text({"text": "   "}) is None
    assert parse_text({}) is None


def test_notify_group_briefing_and_notification() -> None:
    for _, adapter in make_adapters():
        notification = adapter.execute(
            command(
                ActionType.NOTIFY_GROUP,
                {"task_id": "t1", "new_deadline": DEADLINE.isoformat(), "approved_by": "PM"},
            )
        )
        briefing = adapter.execute(
            command(
                ActionType.NOTIFY_GROUP,
                {"is_executive_briefing": True, "briefing": {"summary": "简报"}},
            )
        )
        assert notification.status is CommandStatus.SUCCESS
        assert briefing.status is CommandStatus.SUCCESS

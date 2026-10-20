"""Demo-mode first-contact bootstrap (ADR-0009 §8)."""

import json
from datetime import UTC, datetime

import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.service import GatewayService
from orgpilot.intent.replies import WELCOME_TEXT
from orgpilot.storage.database import Database

PROJECT = "demo-project"
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)


@pytest.fixture
async def service_and_client() -> tuple[GatewayService, MockFeishuClient]:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    feishu_client = MockFeishuClient()
    service = GatewayService(
        db,
        adapter_factory=lambda project_id: FeishuCollaborationAdapter(
            client=feishu_client, project_id=project_id
        ),
    )
    yield service, feishu_client
    await db.close()


def _message(open_id: str, text: str, message_id: str) -> dict:
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": message_id,
                "message_type": "text",
                "chat_type": "p2p",
                "content": json.dumps({"text": text}),
                "create_time": str(int(NOW.timestamp() * 1000)),
            },
        },
    }


def _member_raw(member_id: str, role: str) -> dict:
    return {
        "schema_version": 1,
        "project_id": PROJECT,
        "event_id": f"evt-setup-{member_id}",
        "event_type": "member.registered",
        "source": "human",
        "source_ref": "setup",
        "occurred_at": NOW.isoformat(),
        "received_at": NOW.isoformat(),
        "payload": {"member_id": member_id, "display_name": member_id, "role": role},
    }


async def test_demo_mode_off_rejects_unregistered_sender_without_events(
    service_and_client: tuple[GatewayService, MockFeishuClient],
) -> None:
    service, feishu_client = service_and_client
    handler = FeishuWebhookHandler(service=service, project_id=PROJECT)

    response = await handler.handle_event(_message("ou_new", "你好", "om_1"))

    assert response["msg"] == "sender_not_registered"
    assert await service.event_store.get_events(PROJECT) == []
    assert not any(m["content"] == {"text": WELCOME_TEXT} for m in feishu_client.sent_messages)


async def test_demo_mode_bootstraps_empty_project_once(
    service_and_client: tuple[GatewayService, MockFeishuClient],
) -> None:
    service, feishu_client = service_and_client
    handler = FeishuWebhookHandler(service=service, project_id=PROJECT, demo_mode=True)

    first = await handler.handle_event(_message("ou_tester", "你好", "om_1"))

    assert first["msg"] != "sender_not_registered"
    state = (await service.get_or_replay_agent(PROJECT)).projector.state
    assert state.members["ou_tester"].role == "pm"
    assert set(state.tasks) == {"task-payment", "task-checkout", "task-qa"}
    welcomes = [m for m in feishu_client.sent_messages if m["content"] == {"text": WELCOME_TEXT}]
    assert [m["receive_id"] for m in welcomes] == ["ou_tester"]

    # The project now has members, so a second person is not bootstrapped as PM.
    second = await handler.handle_event(_message("ou_stranger", "你好", "om_2"))

    assert second["msg"] == "sender_not_registered"
    state = (await service.get_or_replay_agent(PROJECT)).projector.state
    assert set(state.members) == {"ou_tester"}


async def test_demo_mode_never_bootstraps_project_with_members_but_no_tasks(
    service_and_client: tuple[GatewayService, MockFeishuClient],
) -> None:
    """Regression: bootstrap used to key on "no tasks" and append unvalidated, so a
    registered member's first message wrote a duplicate member.registered that broke
    every later replay of the project."""
    service, _ = service_and_client
    await service.ingest_raw_events(PROJECT, [_member_raw("ou_pm", "pm")])
    handler = FeishuWebhookHandler(service=service, project_id=PROJECT, demo_mode=True)

    response = await handler.handle_event(_message("ou_pm", "你好", "om_1"))

    assert response["msg"] != "sender_not_registered"
    events = await service.event_store.get_events(PROJECT)
    assert [e.event_id for e in events] == ["evt-setup-ou_pm"]
    state = (await service.get_or_replay_agent(PROJECT)).projector.state
    assert set(state.members) == {"ou_pm"}
    assert state.tasks == {}


def test_demo_mode_is_off_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORGPILOT_DEMO_MODE", raising=False)
    assert OrgPilotSettings.from_env().demo_mode is False
    monkeypatch.setenv("ORGPILOT_DEMO_MODE", "true")
    assert OrgPilotSettings.from_env().demo_mode is True

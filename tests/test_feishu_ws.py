"""Tests for Feishu WebSocket long connection listener and event dispatching."""

import asyncio
import json
from datetime import UTC, datetime

import lark_oapi as lark
import pytest
from lark_oapi.api.im.v1.model.event_message import EventMessage
from lark_oapi.api.im.v1.model.event_sender import EventSender
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import (
    P2ImMessageReceiveV1,
    P2ImMessageReceiveV1Data,
)
from lark_oapi.api.im.v1.model.user_id import UserId
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackAction,
    CallBackOperator,
    P2CardActionTrigger,
    P2CardActionTriggerData,
    P2CardActionTriggerResponse,
)

from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    TaskCreatedEvent,
    TaskCreatedPayload,
)
from orgpilot.feishu.acceptance import L2_REPLY, L2Acceptance
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.ws import FeishuWebSocketListener
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database

NOW = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)


def _make_setup_events(project_id: str) -> list:
    return [
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"evt-{project_id}-alice",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=MemberRegisteredPayload(
                member_id="ou_alice", display_name="Alice", role="backend"
            ),
        ),
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"evt-{project_id}-carol",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=MemberRegisteredPayload(member_id="ou_carol", display_name="Carol", role="pm"),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id=f"evt-{project_id}-task-api",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskCreatedPayload(
                task_id="task-api",
                title="API 开发",
                owner_id="ou_alice",
                deadline=datetime(2026, 3, 30, 18, 0, tzinfo=UTC),
                dependencies=(),
            ),
        ),
    ]


@pytest.fixture
async def gateway_service() -> GatewayService:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    service = GatewayService(db)
    yield service
    await db.close()


async def test_feishu_ws_listener_message_and_card_flow(
    gateway_service: GatewayService,
) -> None:
    project_id = "ws-proj"
    # Seed setup events
    setup_evts = _make_setup_events(project_id)
    await gateway_service.ingest_raw_events(
        project_id, [evt.model_dump(mode="json") for evt in setup_evts]
    )

    listener = FeishuWebSocketListener(
        app_id="cli_test",
        app_secret="sec_test",
        gateway_service=gateway_service,
        project_id=project_id,
    )

    # 1. Non-text message ignored
    non_text_data = P2ImMessageReceiveV1()
    non_text_data.event = P2ImMessageReceiveV1Data()
    non_text_data.event.message = EventMessage()
    non_text_data.event.message.message_type = "image"
    await listener.handle_message_event_async(non_text_data)

    # 2. Text message processed
    msg_data = P2ImMessageReceiveV1()
    msg_data.event = P2ImMessageReceiveV1Data()
    msg_data.event.message = EventMessage()
    msg_data.event.message.message_type = "text"
    msg_data.event.message.message_id = "om_ws_123"
    msg_data.event.message.content = json.dumps({"text": "支付 SDK 报错，排查需要到明天下午 5 点"})
    msg_data.event.sender = EventSender()
    msg_data.event.sender.sender_id = UserId()
    msg_data.event.sender.sender_id.open_id = "ou_alice"

    await listener.handle_message_event_async(msg_data)

    # 3. Check pending approval created
    agent = await gateway_service.get_or_replay_agent(project_id)
    pending_apprs = agent.approval_manager.get_pending_requests()
    assert len(pending_apprs) == 1
    approval_id = pending_apprs[0].approval_id

    # 4. Valid card approval action
    card_data = P2CardActionTrigger()
    card_data.event = P2CardActionTriggerData()
    card_data.event.action = CallBackAction()
    card_data.event.action.value = {"action": "approved", "approval_id": approval_id}
    card_data.event.operator = CallBackOperator()
    card_data.event.operator.open_id = "ou_carol"

    resp = await listener.handle_card_action_async(card_data)
    assert isinstance(resp, P2CardActionTriggerResponse)
    assert resp.card is not None
    assert "已完成" in resp.card.data.get("header", {}).get("title", {}).get("content", "")
    assert "已批准" in json.dumps(resp.card.data, ensure_ascii=False)

    # 5. Invalid card action returns None
    invalid_card_data = P2CardActionTrigger()
    assert await listener.handle_card_action_async(invalid_card_data) is None

    # 6. Test build_event_handler
    handler = listener.build_event_handler()
    assert handler is not None


async def test_feishu_ws_listener_suppresses_credential_bearing_info_logs(
    gateway_service: GatewayService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def start(self) -> None:
            import lark_oapi.ws.client as ws_module

            ws_module.loop.run_until_complete(asyncio.Event().wait())

        async def _disconnect(self) -> None:
            pass

    monkeypatch.setattr(lark.ws, "Client", FakeClient)
    listener = FeishuWebSocketListener(
        app_id="cli_test",
        app_secret="sec_test",
        acceptance=L2Acceptance("test-long-challenge", MockFeishuClient()),
    )

    listener.start()

    assert captured["log_level"] is lark.LogLevel.ERROR
    assert captured["auto_reconnect"] is False
    assert listener.stop() is True
    with pytest.raises(RuntimeError, match="single-use"):
        listener.start()


def _text_message(message_id: str, actor_id: str, text: str) -> P2ImMessageReceiveV1:
    data = P2ImMessageReceiveV1()
    data.event = P2ImMessageReceiveV1Data()
    data.event.message = EventMessage()
    data.event.message.message_type = "text"
    data.event.message.chat_type = "p2p"
    data.event.message.message_id = message_id
    data.event.message.content = json.dumps({"text": text})
    data.event.sender = EventSender()
    data.event.sender.sender_type = "user"
    data.event.sender.sender_id = UserId()
    data.event.sender.sender_id.open_id = actor_id
    return data


def test_feishu_ws_event_handler_failure_reclaims_service_loop(monkeypatch) -> None:
    listener = FeishuWebSocketListener(
        "cli_offline",
        "unused",
        acceptance=L2Acceptance("offline-long-challenge", MockFeishuClient()),
    )

    def fail_handler():
        raise ValueError("invalid event handler")

    monkeypatch.setattr(listener, "build_event_handler", fail_handler)
    with pytest.raises(ValueError, match="invalid event handler"):
        listener.start()
    assert listener._ws_client is None
    assert not listener._loop_thread.is_alive()
    assert listener._loop.is_closed()
    assert listener.stop() is True


async def test_feishu_l2_acceptance_is_challenge_bound_and_single_reply(
    gateway_service: GatewayService,
) -> None:
    challenge = "你好，L2连通测试：9K7M-4Q2X-830A"
    mock_client = MockFeishuClient()
    acceptance = L2Acceptance(challenge, mock_client)
    acceptance.arm(30)
    listener = FeishuWebSocketListener(
        app_id="cli_test",
        app_secret="sec_test",
        project_id="feishu-l2-test",
        acceptance=acceptance,
    )

    await listener.handle_message_event_async(
        _text_message("om_wrong", "ou_intruder", "你好，但不是挑战码")
    )
    assert mock_client.sent_messages == []
    assert acceptance.snapshot()["filtered"] == {"challenge_mismatch": 1}

    await listener.handle_message_event_async(_text_message("om_match", "ou_live_test", challenge))
    result = acceptance.wait(0)
    assert result["reply_status"] == "api_accepted"
    assert result["user_visible_confirmed"] is False
    assert len(mock_client.sent_messages) == 1
    assert mock_client.sent_messages[0]["receive_id"] == "ou_live_test"
    assert mock_client.sent_messages[0]["content"] == {"text": L2_REPLY}
    assert mock_client.sent_cards == []
    assert mock_client.updated_tasks == []
    assert await gateway_service.event_store.count("feishu-l2-test") == 0

    await listener.handle_message_event_async(
        _text_message("om_duplicate", "ou_live_test", challenge)
    )
    assert len(mock_client.sent_messages) == 1

    card_data = P2CardActionTrigger()
    card_data.event = P2CardActionTriggerData()
    card_data.event.action = CallBackAction()
    card_data.event.action.value = {"action": "approved", "approval_id": "ignored"}
    assert await listener.handle_card_action_async(card_data) is None
    assert mock_client.sent_cards == []
    assert mock_client.updated_tasks == []


async def test_feishu_ws_listener_bridges_sdk_loop_to_service_loop(
    gateway_service: GatewayService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConnectedFakeClient:
        def __init__(self, **_kwargs) -> None:
            self._conn = None
            self.disconnected = False

        def start(self) -> None:
            import lark_oapi.ws.client as ws_module

            async def _connected() -> None:
                self._conn = object()

            ws_module.loop.run_until_complete(_connected())
            ws_module.loop.run_until_complete(asyncio.Event().wait())

        async def _disconnect(self) -> None:
            self.disconnected = True
            self._conn = None

    monkeypatch.setattr(lark.ws, "Client", ConnectedFakeClient)
    service_loop = asyncio.new_event_loop()
    listener = FeishuWebSocketListener(
        app_id="cli_test",
        app_secret="sec_test",
        gateway_service=gateway_service,
        loop=service_loop,
    )

    client = listener.start()
    try:
        assert listener.wait_until_connected(1.0) is True

        async def _value() -> int:
            return 42

        assert listener._run_coroutine(_value()) == 42
    finally:
        assert listener.stop() is True
        assert client.disconnected is True
        assert not listener._ws_thread.is_alive()
        assert listener._ws_loop.is_closed()
        assert not service_loop.is_running()
        service_loop.close()

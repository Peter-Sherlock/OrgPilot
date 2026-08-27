"""Tests for Feishu WebSocket long connection listener and event dispatching."""

import json
from datetime import UTC, datetime

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

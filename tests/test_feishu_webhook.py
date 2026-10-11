"""Tests for Feishu Webhook event handling, messages, and card callback flows."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.domain.enums import CoordinationCaseStatus, HealthStatus
from orgpilot.events.models import EventSource, TaskUpdatedEvent, TaskUpdatedPayload
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.app import create_app
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database

# Anchored to the real clock so approval expiry (created_at + 2 days) never lapses
# between event ingestion and the card/decision callbacks under test.
NOW = datetime.now(UTC)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    settings = OrgPilotSettings(collaboration_adapter="mock", feishu_use_ws=False)
    app = create_app(db, settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await db.close()


@pytest.fixture
async def harness() -> tuple[httpx.AsyncClient, GatewayService]:
    """Yields the webhook HTTP client together with its GatewayService for inspection."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    settings = OrgPilotSettings(collaboration_adapter="mock", feishu_use_ws=False)
    app = create_app(db, settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, app.state.gateway_service
    await db.close()


def _message_payload(
    open_id: str,
    text: str,
    message_id: str,
    chat_type: str = "p2p",
    chat_id: str | None = None,
) -> dict:
    """Builds an im.message.receive_v1 webhook body for a text message."""
    message: dict = {
        "message_id": message_id,
        "message_type": "text",
        "content": json.dumps({"text": text}),
        "create_time": str(int(NOW.timestamp() * 1000)),
        "chat_type": chat_type,
    }
    if chat_id:
        message["chat_id"] = chat_id
    return {
        "header": {
            "event_id": f"evt-{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": str(int(NOW.timestamp() * 1000)),
        },
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
            "message": message,
        },
    }


def _make_setup_events(project_id: str) -> list[dict]:
    return [
        {
            "schema_version": 1,
            "project_id": project_id,
            "event_id": f"evt-{project_id}-alice",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": NOW.isoformat(),
            "received_at": NOW.isoformat(),
            "payload": {"member_id": "ou_alice", "display_name": "Alice", "role": "backend"},
        },
        {
            "schema_version": 1,
            "project_id": project_id,
            "event_id": f"evt-{project_id}-carol",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": NOW.isoformat(),
            "received_at": NOW.isoformat(),
            "payload": {"member_id": "ou_carol", "display_name": "Carol", "role": "pm"},
        },
        {
            "schema_version": 1,
            "project_id": project_id,
            "event_id": f"evt-{project_id}-task-api",
            "event_type": "task.created",
            "source": "task",
            "source_ref": "setup",
            "occurred_at": NOW.isoformat(),
            "received_at": NOW.isoformat(),
            "payload": {
                "task_id": "backend_api",
                "title": "Backend API",
                "owner_id": "ou_alice",
                "workflow_status": "doing",
                "deadline": (NOW + timedelta(hours=3)).isoformat(),
            },
        },
    ]


async def test_feishu_url_verification(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/feishu/events",
        json={"type": "url_verification", "challenge": "test-challenge-12345"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "test-challenge-12345"}


async def test_feishu_message_and_card_approval_flow(client: httpx.AsyncClient) -> None:
    # 1. Ingest setup events for feishu-project
    setup_events = _make_setup_events("feishu-project")
    await client.post("/api/v1/projects/feishu-project/events", json={"events": setup_events})

    # 2. Simulate Feishu im.message.receive_v1 event
    msg_payload = {
        "header": {
            "event_id": "evt_msg_feishu_1",
            "event_type": "im.message.receive_v1",
            "create_time": str(int(NOW.timestamp() * 1000)),
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_alice"},
                "sender_type": "user",
            },
            "message": {
                "message_id": "om_feishu_1",
                "message_type": "text",
                "content": json.dumps({"text": "支付 SDK 报错，排查需要到明天下午 5 点"}),
                "create_time": str(int(NOW.timestamp() * 1000)),
            },
        },
    }
    resp_msg = await client.post("/api/v1/feishu/events", json=msg_payload)
    assert resp_msg.status_code == 200
    res_data = resp_msg.json()
    assert res_data["code"] == 0
    assert res_data["data"]["is_actionable"] is True
    assert res_data["data"]["turn_termination_reason"] == "waiting_approval"

    # 3. Check pending approval
    resp_appr = await client.get("/api/v1/projects/feishu-project/approvals")
    approvals = resp_appr.json()
    assert len(approvals) == 1
    approval_id = approvals[0]["approval_id"]

    # 4. Missing or different operators cannot approve Carol's request.
    missing_operator = await client.post(
        "/api/v1/feishu/events",
        json={
            "open_message_id": "om_card_msg_missing_operator",
            "action": {
                "value": {
                    "action": "approved",
                    "approval_id": approval_id,
                }
            },
        },
    )
    assert missing_operator.json()["code"] == 400

    spoofed_callback = {
        "open_message_id": "om_card_msg_spoofed",
        "operator": {"open_id": "ou_mallory"},
        "action": {
            "value": {
                "action": "approved",
                "approval_id": approval_id,
            }
        },
    }
    spoofed = await client.post("/api/v1/feishu/events", json=spoofed_callback)
    assert spoofed.json()["code"] == 403

    # 5. Simulate Feishu card.action.trigger (PM Carol clicks [Approve])
    card_callback_payload = {
        "open_message_id": "om_card_msg_1",
        "operator": {"open_id": "ou_carol"},
        "action": {
            "value": {
                "action": "approved",
                "approval_id": approval_id,
                "case_id": approvals[0]["case_id"],
            }
        },
    }
    resp_card = await client.post("/api/v1/feishu/events", json=card_callback_payload)
    assert resp_card.status_code == 200
    card_data = resp_card.json()
    assert card_data["code"] == 0
    assert card_data["turn_termination_reason"] == "all_resolved"
    assert "任务改期审批 [已完成]" in card_data["card"]["header"]["title"]["content"]


async def test_feishu_card_rejection_flow(client: httpx.AsyncClient) -> None:
    setup_events = _make_setup_events("feishu-project-rej")
    await client.post("/api/v1/projects/feishu-project-rej/events", json={"events": setup_events})

    # Message triggering approval
    msg_payload = {
        "header": {
            "event_id": "evt_msg_feishu_2",
            "event_type": "im.message.receive_v1",
            "create_time": str(int(NOW.timestamp() * 1000)),
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_alice"}},
            "message": {
                "message_id": "om_feishu_2",
                "message_type": "text",
                "content": json.dumps({"text": "支付 SDK 报错，排查需要到明天下午 5 点"}),
            },
        },
    }
    resp_msg = await client.post(
        "/api/v1/feishu/events",
        json=msg_payload,
        params={"project_id": "feishu-project-rej"},
    )
    assert resp_msg.status_code == 200

    resp_appr = await client.get("/api/v1/projects/feishu-project-rej/approvals")
    approval_id = resp_appr.json()[0]["approval_id"]

    # PM rejects via button click
    card_callback_payload = {
        "open_message_id": "om_card_msg_2",
        "operator": {"open_id": "ou_carol"},
        "action": {
            "value": {
                "action": "rejected",
                "approval_id": approval_id,
            }
        },
    }
    resp_card = await client.post(
        "/api/v1/feishu/events",
        json=card_callback_payload,
        params={"project_id": "feishu-project-rej"},
    )
    assert resp_card.status_code == 200
    card_data = resp_card.json()
    assert card_data["code"] == 0
    assert card_data["turn_termination_reason"] == "escalated"
    assert "任务改期审批 [已拒绝]" in card_data["card"]["header"]["title"]["content"]


async def test_feishu_card_missing_or_invalid_approval(client: httpx.AsyncClient) -> None:
    # Missing approval_id
    resp = await client.post(
        "/api/v1/feishu/events",
        json={"open_message_id": "om_1", "action": {"value": {}}},
    )
    assert resp.json()["code"] == 400

    # Non-existent approval_id
    resp_not_found = await client.post(
        "/api/v1/feishu/events",
        json={
            "open_message_id": "om_1",
            "operator": {"open_id": "ou_carol"},
            "action": {"value": {"action": "approved", "approval_id": "non-existent"}},
        },
    )
    assert resp_not_found.json()["code"] == 404

async def test_sync_probe_reply_persists_health_event_and_replays_consistently(
    harness: tuple[httpx.AsyncClient, GatewayService],
) -> None:
    """T1: a complete probe reply must not crash and must survive a database replay."""
    client, service = harness
    project_id = "feishu-project"
    await client.post(
        f"/api/v1/projects/{project_id}/events",
        json={"events": _make_setup_events(project_id)},
    )

    # PM starts a progress sync; ou_alice owns the only active task and gets probed
    start = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_carol", "我要知道当前的项目进度", "om_sync_start"),
        params={"project_id": project_id},
    )
    assert start.status_code == 200
    assert start.json()["msg"] == "sync_session_started"

    # The probed member answers with a complete risk report
    reply = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_alice", "支付SDK报错，排查需要到明天下午5点", "om_probe_reply"),
        params={"project_id": project_id},
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["code"] == 0

    events = (await client.get(f"/api/v1/projects/{project_id}/events")).json()
    health_events = [e for e in events if e["event_type"] == "task.health_reported"]
    assert any(
        e["actor_id"] == "ou_alice" and e["payload"]["task_id"] == "backend_api"
        for e in health_events
    )

    # A fresh agent replayed from the database agrees on the task's health
    agent = await service.get_or_replay_agent(project_id)
    assert agent.projector.state.tasks["backend_api"].health_status == HealthStatus.DELAYED


async def test_sync_reply_persists_turn_outputs_and_survives_replay() -> None:
    """T6: events, cases, and approvals produced by the sync reply turn survive replay."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    project_id = "feishu-project"
    shared_adapter = MockCollaborationAdapter(project_id=project_id)
    service = GatewayService(db, adapter_factory=lambda project: shared_adapter)
    handler = FeishuWebhookHandler(service=service, project_id=project_id)

    await service.ingest_raw_events(project_id, _make_setup_events(project_id))

    # Every approval request routed to the PM emits a scripted adapter-side event,
    # simulating an outbound system acknowledgment of the reschedule proposal.
    scripted_update = TaskUpdatedEvent(
        project_id=project_id,
        event_id="evt-sync-turn-generated",
        event_type="task.updated",
        source=EventSource.TASK,
        source_ref="adapter:scripted",
        actor_id="ou_carol",
        occurred_at=NOW,
        received_at=NOW,
        payload=TaskUpdatedPayload(task_id="backend_api", deadline=NOW + timedelta(days=3)),
    )
    shared_adapter.script_reply("ou_carol", scripted_update)

    start = await handler.handle_event(
        _message_payload("ou_carol", "我要知道当前的项目进度", "om_t6_start")
    )
    assert start["msg"] == "sync_session_started"

    reply = await handler.handle_event(
        _message_payload("ou_alice", "支付SDK报错，排查需要到明天下午5点", "om_t6_reply")
    )
    assert reply["code"] == 0

    stored_ids = {e.event_id for e in await service.event_store.get_events(project_id)}
    assert "evt-sync-turn-generated" in stored_ids

    agent = await service.get_or_replay_agent(project_id)
    assert agent.projector.state.tasks["backend_api"].deadline == scripted_update.payload.deadline
    cases = agent.case_ledger.get_all_cases()
    assert len(cases) == 1
    assert cases[0].status == CoordinationCaseStatus.WAITING_FOR_APPROVAL
    assert len(agent.approval_manager.get_all_requests()) == 1
    await db.close()

async def test_probe_reply_with_sync_keyword_is_not_treated_as_new_sync(
    harness: tuple[httpx.AsyncClient, GatewayService],
) -> None:
    """T2: inside an active session a probed member's reply never starts a new session."""
    client, service = harness
    project_id = "feishu-project"
    await client.post(
        f"/api/v1/projects/{project_id}/events",
        json={"events": _make_setup_events(project_id)},
    )

    start = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_carol", "我要知道当前的项目进度", "om_t2_start"),
        params={"project_id": project_id},
    )
    assert start.status_code == 200
    session_id = start.json()["data"]["session_id"]

    # "当前进度" is a sync keyword, but the sender is a probed member of the
    # active session, so the message must be handled as that member's reply.
    reply = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_alice", "我这边当前进度正常", "om_t2_reply"),
        params={"project_id": project_id},
    )
    assert reply.status_code == 200
    assert reply.json()["msg"] != "sync_session_started"

    coordinator = await service.get_sync_coordinator(project_id)
    assert len(coordinator._sessions) == 1
    assert coordinator.get_session(session_id) is not None
    assert coordinator.get_session(session_id).member_probes["ou_alice"].turns_count == 1


async def test_non_pm_member_cannot_trigger_progress_sync(
    harness: tuple[httpx.AsyncClient, GatewayService],
) -> None:
    """T3: a registered non-PM member saying "同步进度" must not open a sync session."""
    client, service = harness
    project_id = "feishu-project"
    await client.post(
        f"/api/v1/projects/{project_id}/events",
        json={"events": _make_setup_events(project_id)},
    )

    resp = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_alice", "同步进度", "om_t3_sync"),
        params={"project_id": project_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["msg"] != "sync_session_started"
    assert body["data"]["is_actionable"] is False

    coordinator = await service.get_sync_coordinator(project_id)
    assert len(coordinator._sessions) == 0
    assert coordinator.get_active_session(project_id) is None

async def test_converged_probe_reply_is_idempotent_per_message_id(
    harness: tuple[httpx.AsyncClient, GatewayService],
) -> None:
    """T4: one claim set per reply, and redelivery never double-counts a message."""
    client, service = harness
    project_id = "feishu-project"
    await client.post(
        f"/api/v1/projects/{project_id}/events",
        json={"events": _make_setup_events(project_id)},
    )

    async def health_event_count() -> int:
        events = (await client.get(f"/api/v1/projects/{project_id}/events")).json()
        return sum(1 for e in events if e["event_type"] == "task.health_reported")

    async def probe_state(message_id: str | None = None):
        coordinator = await service.get_sync_coordinator(project_id)
        return coordinator

    start = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_carol", "我要知道当前的项目进度", "om_t4_start"),
        params={"project_id": project_id},
    )
    session_id = start.json()["data"]["session_id"]

    async def turns_count() -> int:
        coordinator = await service.get_sync_coordinator(project_id)
        return coordinator.get_session(session_id).member_probes["ou_alice"].turns_count

    # 1. Vague risk report -> autonomous clarification, one ingested claim
    vague = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_alice", "支付SDK报错卡住了", "om_t4_vague"),
        params={"project_id": project_id},
    )
    assert vague.json()["msg"] == "clarification_sent"
    assert await health_event_count() == 1
    assert await turns_count() == 1

    # 2. Feishu redelivers the same message_id -> no new turn, no new events
    redelivered = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_alice", "支付SDK报错卡住了", "om_t4_vague"),
        params={"project_id": project_id},
    )
    assert redelivered.status_code == 200
    assert await health_event_count() == 1
    assert await turns_count() == 1

    # 3. Complete answer converges the probe and yields exactly one claim event
    full = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_alice", "预计排查需要到明天下午5点", "om_t4_full"),
        params={"project_id": project_id},
    )
    assert full.json()["msg"] == "probe_collected"
    assert await health_event_count() == 2
    assert await turns_count() == 2

    # 4. Redelivery of the converged reply must not add anything
    total_before = len((await client.get(f"/api/v1/projects/{project_id}/events")).json())
    replayed = await client.post(
        "/api/v1/feishu/events",
        json=_message_payload("ou_alice", "预计排查需要到明天下午5点", "om_t4_full"),
        params={"project_id": project_id},
    )
    assert replayed.status_code == 200
    total_after = len((await client.get(f"/api/v1/projects/{project_id}/events")).json())
    assert total_after == total_before
    assert await health_event_count() == 2
    assert await turns_count() == 2

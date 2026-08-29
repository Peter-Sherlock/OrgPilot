"""Tests for Feishu Webhook event handling, messages, and card callback flows."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.gateway.app import create_app
from orgpilot.storage.database import Database

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


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


def _greeting_payload(message_id: str) -> dict:
    """Builds a p2p non-actionable greeting message from a first-time solo user."""
    ts = str(int(NOW.timestamp() * 1000))
    return {
        "header": {
            "event_id": f"evt_{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": ts,
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_solo"}, "sender_type": "user"},
            "message": {
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
                "create_time": ts,
            },
        },
    }


async def test_feishu_demo_bootstrap_disabled_by_default(client: httpx.AsyncClient) -> None:
    # Regression: demo task injection used to run unconditionally on the production
    # webhook path; it must stay off unless ORGPILOT_DEMO_BOOTSTRAP opts in.
    resp = await client.post("/api/v1/feishu/events", json=_greeting_payload("om_greet_default"))
    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    state = (await client.get("/api/v1/projects/feishu-project/state")).json()
    assert state["tasks"] == {}
    events = (await client.get("/api/v1/projects/feishu-project/events")).json()
    assert events == []


async def test_feishu_demo_bootstrap_opt_in() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    settings = OrgPilotSettings(
        collaboration_adapter="mock",
        feishu_use_ws=False,
        demo_bootstrap=True,
    )
    app = create_app(db, settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/v1/feishu/events", json=_greeting_payload("om_greet_optin"))
        assert resp.status_code == 200
        state = (await ac.get("/api/v1/projects/feishu-project/state")).json()

    assert len(state["tasks"]) == 3
    assert "task-payment" in state["tasks"]
    await db.close()


async def test_feishu_sync_intent_starts_progress_sync(client: httpx.AsyncClient) -> None:
    setup_events = _make_setup_events("feishu-project")
    await client.post("/api/v1/projects/feishu-project/events", json={"events": setup_events})

    ts = str(int(NOW.timestamp() * 1000))
    sync_payload = {
        "header": {
            "event_id": "evt_msg_sync_intent",
            "event_type": "im.message.receive_v1",
            "create_time": ts,
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_carol"}, "sender_type": "user"},
            "message": {
                "message_id": "om_sync_intent_1",
                "message_type": "text",
                "content": json.dumps({"text": "我要知道当前的项目进度"}),
                "create_time": ts,
            },
        },
    }
    resp = await client.post("/api/v1/feishu/events", json=sync_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == 0
    assert data["msg"] == "sync_session_started"
    assert data["data"]["probed_members_count"] == 1
    assert data["data"]["session_id"]


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

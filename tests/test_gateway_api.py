"""Async API integration tests for FastAPI event gateway."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.gateway.app import create_app
from orgpilot.storage.database import Database

# Anchored to the real clock so approval expiry (created_at + 2 days) never lapses
# between message ingestion and the approval decision callbacks under test.
NOW = datetime.now(UTC)
# Relative time in messages resolves against the business timezone (B5), so the
# "明天下午5点" deadline the approval flow persists is 17:00 Beijing time.
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


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
            "payload": {"member_id": "alice", "display_name": "Alice", "role": "backend"},
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
            "payload": {"member_id": "carol", "display_name": "Carol", "role": "pm"},
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
                "owner_id": "alice",
                "workflow_status": "doing",
                "deadline": (NOW + timedelta(hours=3)).isoformat(),
            },
        },
    ]


async def test_gateway_event_ingestion_and_query(client: httpx.AsyncClient) -> None:
    events = _make_setup_events("proj-api-test")

    # Ingest events
    resp = await client.post("/api/v1/projects/proj-api-test/events", json={"events": events})
    assert resp.status_code == 200
    data = resp.json()
    assert data["appended"] == 3
    assert data["duplicates"] == 0

    # Ingest duplicate events
    resp_dup = await client.post("/api/v1/projects/proj-api-test/events", json={"events": events})
    assert resp_dup.status_code == 200
    assert resp_dup.json()["duplicates"] == 3

    # Query events
    resp_get = await client.get("/api/v1/projects/proj-api-test/events")
    assert resp_get.status_code == 200
    assert len(resp_get.json()) == 3


async def test_gateway_natural_language_message_and_approval_flow(
    client: httpx.AsyncClient,
) -> None:
    # 1. Setup project events
    setup_events = _make_setup_events("proj-nl-flow")
    await client.post("/api/v1/projects/proj-nl-flow/events", json={"events": setup_events})

    # 2. Ingest natural language message with delay past deadline
    msg_body = {
        "message": "支付 SDK 报错，排查需要到明天下午 5 点",
        "actor_id": "alice",
        "occurred_at": NOW.isoformat(),
        "auto_run_turn": True,
    }
    resp_msg = await client.post("/api/v1/projects/proj-nl-flow/messages", json=msg_body)
    assert resp_msg.status_code == 200, resp_msg.text
    msg_data = resp_msg.json()
    assert msg_data["is_actionable"] is True
    assert msg_data["extracted_events_count"] == 1
    assert msg_data["turn_termination_reason"] == "waiting_approval"

    # 3. Query active cases
    resp_cases = await client.get("/api/v1/projects/proj-nl-flow/cases")
    assert resp_cases.status_code == 200
    cases = resp_cases.json()
    assert len(cases) == 1
    case_id = cases[0]["case_id"]
    assert cases[0]["status"] == "waiting_for_approval"

    # Query single case
    resp_single_case = await client.get(f"/api/v1/projects/proj-nl-flow/cases/{case_id}")
    assert resp_single_case.status_code == 200
    assert resp_single_case.json()["source_task_id"] == "backend_api"

    # 4. Query pending approvals
    resp_appr = await client.get("/api/v1/projects/proj-nl-flow/approvals")
    assert resp_appr.status_code == 200
    approvals = resp_appr.json()
    assert len(approvals) == 1
    approval_id = approvals[0]["approval_id"]
    assert approvals[0]["status"] == "pending"

    # 5. PM Carol submits approval decision (approved)
    decision_body = {
        "decision": "approved",
        "approver_id": "carol",
        "reason": "approved by PM",
    }
    resp_dec = await client.post(
        f"/api/v1/projects/proj-nl-flow/approvals/{approval_id}/decision", json=decision_body
    )
    assert resp_dec.status_code == 200
    dec_data = resp_dec.json()
    assert dec_data["decision"] == "approved"
    assert dec_data["turn_termination_reason"] == "all_resolved"

    # 6. Check state snapshot endpoint
    resp_state = await client.get("/api/v1/projects/proj-nl-flow/state")
    assert resp_state.status_code == 200
    state_data = resp_state.json()
    expected_deadline = datetime.combine(
        NOW.astimezone(BUSINESS_TZ).date() + timedelta(days=1),
        time(17, 0),
        tzinfo=BUSINESS_TZ,
    )
    persisted_deadline = state_data["tasks"]["backend_api"]["deadline"].replace("Z", "+00:00")
    assert datetime.fromisoformat(persisted_deadline) == expected_deadline
    assert len(state_data["active_cases"]) == 0
    assert len(state_data["pending_approvals"]) == 0

    persisted_events = await client.get("/api/v1/projects/proj-nl-flow/events")
    assert [event["event_type"] for event in persisted_events.json()].count("task.updated") == 1


async def test_gateway_rejection_flow(client: httpx.AsyncClient) -> None:
    # 1. Setup project events
    setup_events = _make_setup_events("proj-reject-flow")
    await client.post("/api/v1/projects/proj-reject-flow/events", json={"events": setup_events})

    # 2. Ingest delay message
    msg_body = {
        "message": "支付 SDK 报错，排查需要到明天下午 5 点",
        "actor_id": "alice",
        "occurred_at": NOW.isoformat(),
        "auto_run_turn": True,
    }
    await client.post("/api/v1/projects/proj-reject-flow/messages", json=msg_body)

    # 3. PM Carol rejects
    resp_appr = await client.get("/api/v1/projects/proj-reject-flow/approvals")
    approval_id = resp_appr.json()[0]["approval_id"]

    decision_body = {
        "decision": "rejected",
        "approver_id": "carol",
        "reason": "cannot delay sprint deadline",
    }
    resp_dec = await client.post(
        f"/api/v1/projects/proj-reject-flow/approvals/{approval_id}/decision", json=decision_body
    )
    assert resp_dec.status_code == 200
    assert resp_dec.json()["turn_termination_reason"] == "escalated"


async def test_gateway_run_turn_endpoint(client: httpx.AsyncClient) -> None:
    setup_events = _make_setup_events("proj-turn-test")
    await client.post("/api/v1/projects/proj-turn-test/events", json={"events": setup_events})

    resp = await client.post("/api/v1/projects/proj-turn-test/run-turn", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["round_number"] == 1
    assert data["termination_reason"] == "no_action"


async def test_gateway_error_cases(client: httpx.AsyncClient) -> None:
    # Case not found
    resp = await client.get("/api/v1/projects/proj-err/cases/non-existent-case")
    assert resp.status_code == 404

    # Approval not found
    resp = await client.post(
        "/api/v1/projects/proj-err/approvals/non-existent-approval/decision",
        json={"decision": "approved", "approver_id": "carol"},
    )
    assert resp.status_code == 404

    # Invalid events ingestion
    resp = await client.post(
        "/api/v1/projects/proj-err/events",
        json={"events": [{"invalid": "event"}]},
    )
    assert resp.status_code == 400


async def test_app_lifespan() -> None:
    app = create_app()
    assert app.title == "OrgPilot Event Gateway"

async def test_app_lifespan_runs_database_lifecycle() -> None:
    """The app lifespan must initialize and dispose the configured database."""
    db = Database("sqlite+aiosqlite:///:memory:")
    settings = OrgPilotSettings(collaboration_adapter="mock", feishu_use_ws=False)
    app = create_app(db, settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/projects/lifespan-proj/events")
            assert resp.status_code == 200
            assert resp.json() == []


async def test_message_ingestion_rejects_unknown_actor(client: httpx.AsyncClient) -> None:
    """A message from a non-member is rejected with 400 and persists nothing."""
    await client.post(
        "/api/v1/projects/proj-unknown-actor/events",
        json={"events": _make_setup_events("proj-unknown-actor")},
    )

    resp = await client.post(
        "/api/v1/projects/proj-unknown-actor/messages",
        json={
            "message": "支付SDK报错卡住了",
            "actor_id": "ou_stranger",
            "occurred_at": NOW.isoformat(),
            "auto_run_turn": False,
        },
    )
    assert resp.status_code == 400
    assert "unknown event actor" in resp.json()["detail"]

    events = (await client.get("/api/v1/projects/proj-unknown-actor/events")).json()
    assert len(events) == 3


async def test_approval_decision_error_paths(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/v1/projects/proj-appr-err/events",
        json={"events": _make_setup_events("proj-appr-err")},
    )
    resp_msg = await client.post(
        "/api/v1/projects/proj-appr-err/messages",
        json={
            "message": "支付 SDK 报错，排查需要到明天下午 5 点",
            "actor_id": "alice",
            "occurred_at": NOW.isoformat(),
        },
    )
    assert resp_msg.status_code == 200
    approval = (await client.get("/api/v1/projects/proj-appr-err/approvals")).json()[0]
    decision_url = f"/api/v1/projects/proj-appr-err/approvals/{approval['approval_id']}/decision"

    # A non-designated approver cannot decide
    resp_forbidden = await client.post(
        decision_url,
        json={"decision": "approved", "approver_id": "mallory"},
    )
    assert resp_forbidden.status_code == 403

    # The designated PM approves; a second decision on the consumed request fails
    resp_ok = await client.post(
        decision_url,
        json={"decision": "approved", "approver_id": "carol"},
    )
    assert resp_ok.status_code == 200
    resp_again = await client.post(
        decision_url,
        json={"decision": "approved", "approver_id": "carol"},
    )
    assert resp_again.status_code == 400


async def test_coordination_sandbox_endpoints(client: httpx.AsyncClient) -> None:
    project_id = "sandbox-proj"
    resp = await client.post(f"/api/v1/projects/{project_id}/bootstrap-sandbox")
    assert resp.status_code == 200
    assert resp.json()["status"] == "initialized"

    # REST-triggered sync with a custom intro probes every active task owner
    resp_sync = await client.post(
        f"/api/v1/projects/{project_id}/sync",
        params={"initiated_by": "ou_pm", "custom_intro": "请汇报进度"},
    )
    assert resp_sync.status_code == 200
    rest_session = resp_sync.json()
    assert set(rest_session["member_probes"]) == {"ou_alice", "ou_bob", "ou_david"}

    resp_found = await client.get(
        f"/api/v1/projects/{project_id}/sync-sessions/{rest_session['session_id']}"
    )
    assert resp_found.status_code == 200
    assert resp_found.json()["session_id"] == rest_session["session_id"]

    resp_missing = await client.get(f"/api/v1/projects/{project_id}/sync-sessions/nope")
    assert resp_missing.json() == {"error": "session not found", "session_id": "nope"}

    async def chat(actor_id: str, message: str) -> dict:
        resp_chat = await client.post(
            f"/api/v1/projects/{project_id}/sandbox-chat",
            params={"actor_id": actor_id, "message": message},
        )
        assert resp_chat.status_code == 200
        return resp_chat.json()

    # Sync intent from the PM through the chat surface
    started = await chat("ou_pm", "/同步")
    assert started["type"] == "sync_started"

    # Vague reply -> autonomous clarification; a sync keyword inside a probed
    # member's reply stays a reply instead of starting another session
    clarified = await chat("ou_alice", "支付SDK报错卡住了")
    assert clarified["type"] == "clarification_needed"

    keyword_reply = await chat("ou_bob", "我这边当前进度正常")
    assert keyword_reply["type"] == "member_collected"

    collected = await chat("ou_alice", "预计排查需要到明天下午5点")
    assert collected["type"] == "member_collected"

    # A collected member's unrelated follow-up routes normally instead of re-probing
    followup = await chat("ou_alice", "另外支付SDK的文档还没写")
    assert followup["type"] == "normal_turn"

    final = await chat("ou_david", "压测全部调通，按原计划推进")
    assert final["type"] == "sync_completed"
    assert final["briefing"]["total_active_tasks"] == 3

    normal = await chat("ou_pm", "今天天气真好")
    assert normal["type"] == "normal_turn"
    assert normal["is_actionable"] is False

    rejected = await chat("ou_stranger", "支付SDK报错卡住了")
    assert rejected["type"] == "message_rejected"

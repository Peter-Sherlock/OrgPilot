"""Async API integration tests for FastAPI event gateway."""

from datetime import datetime, timedelta

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.gateway.app import create_app
from orgpilot.gateway.service import GatewayService
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
    assert state_data["tasks"]["backend_api"]["deadline"] == "2026-09-11T17:00:00+08:00"
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


async def test_gateway_bootstrap_sandbox_persists_state(client: httpx.AsyncClient) -> None:
    # Regression: this endpoint crashed with a TypeError on a stale save_state
    # signature; it must initialize the sandbox and serve the projected state.
    resp = await client.post("/api/v1/projects/proj-sandbox/bootstrap-sandbox")
    assert resp.status_code == 200
    assert resp.json() == {"status": "initialized", "members_count": 4, "tasks_count": 3}

    resp_state = await client.get("/api/v1/projects/proj-sandbox/state")
    assert resp_state.status_code == 200
    state_data = resp_state.json()
    assert len(state_data["tasks"]) == 3
    assert len(state_data["members"]) == 4


async def test_gateway_sync_reply_persists_snapshot_and_events() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-sync-persist"
    await service.ingest_raw_events(project_id, _make_setup_events(project_id))

    session = await service.start_progress_sync(project_id, initiated_by="carol")
    assert "alice" in session.member_probes

    converged, clarify_q, _ = await service.handle_sync_member_reply(
        project_id=project_id,
        member_id="alice",
        message="支付 SDK 报错，排查需要到明天下午 5 点",
        occurred_at=NOW,
    )
    assert converged is True
    assert clarify_q is None

    # Regression: the sync reply path must persist the projected snapshot and the
    # extracted health event, not crash on a stale save_state signature.
    snapshot = await service.state_store.load_state(project_id)
    assert snapshot is not None
    persisted = await service.event_store.get_events(project_id)
    assert any(event.event_type == "task.health_reported" for event in persisted)
    await db.close()


async def test_gateway_sandbox_chat_sync_flow_end_to_end(client: httpx.AsyncClient) -> None:
    """Covers the split-screen sandbox chat backend: sync start, autonomous
    clarification, member collection, DAG briefing synthesis, and normal turns."""
    base = "/api/v1/projects/proj-chat"
    await client.post(f"{base}/bootstrap-sandbox")

    start = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "我要知道当前的项目进度"},
    )
    assert start.status_code == 200
    start_data = start.json()
    assert start_data["type"] == "sync_started"
    session_id = start_data["session_id"]
    assert set(start_data["probed_members"]) == {"ou_alice", "ou_bob", "ou_david"}

    detail = await client.get(f"{base}/sync-sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "probing"

    alice = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_alice", "message": "支付SDK接入一切正常，按计划推进"},
    )
    assert alice.json()["type"] == "member_collected"

    bob_vague = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_bob", "message": "收银台前端有点问题"},
    )
    assert bob_vague.json()["type"] == "clarification_needed"
    assert bob_vague.json()["bot_reply"]

    bob_clear = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_bob", "message": "收银台前端搞定，预计明天下午6点恢复"},
    )
    assert bob_clear.json()["type"] == "member_collected"

    david = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_david", "message": "压测一切正常"},
    )
    assert david.json()["type"] == "sync_completed"
    briefing = david.json()["briefing"]
    assert briefing is not None
    assert briefing["total_active_tasks"] == 3
    assert briefing["summary_text"]
    assert briefing["recommended_actions"]

    final_detail = (await client.get(f"{base}/sync-sessions/{session_id}")).json()
    assert final_detail["status"] == "completed"
    assert final_detail["briefing"] is not None

    normal = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "大家好，记得下午两点站会"},
    )
    assert normal.status_code == 200
    assert normal.json()["type"] == "normal_turn"


async def test_app_lifespan() -> None:
    app = create_app()
    assert app.title == "OrgPilot Event Gateway"

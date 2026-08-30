"""Async API integration tests for FastAPI event gateway."""

import asyncio
import json
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


def _health_report_event(project_id: str, task_id: str, health_status: str) -> dict:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "event_id": f"evt-{project_id}-health-{task_id}",
        "event_type": "task.health_reported",
        "source": "message",
        "source_ref": "om-health-1",
        "actor_id": "alice",
        "occurred_at": NOW.isoformat(),
        "received_at": NOW.isoformat(),
        "payload": {
            "task_id": task_id,
            "health_status": health_status,
            "blocker": "SDK 报错卡住",
            "confidence": 0.95,
        },
    }


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


async def test_force_complete_sync_marks_no_response_and_delivers_briefing(
    client: httpx.AsyncClient,
) -> None:
    """Regression for the live-test dead loop: a member who never replies used to
    wedge the session in clarifying forever, so the briefing was never delivered.
    The force-complete endpoint must close the cycle with no_response markers."""
    base = "/api/v1/projects/proj-force-complete"
    await client.post(f"{base}/bootstrap-sandbox")

    start = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "我要知道当前的项目进度"},
    )
    assert start.json()["type"] == "sync_started"

    alice = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_alice", "message": "支付SDK接入一切正常，按计划推进"},
    )
    assert alice.json()["type"] == "member_collected"

    # ou_bob and ou_david stay silent; the PM force-completes the session.
    done = await client.post(f"{base}/sync/complete")
    assert done.status_code == 200
    data = done.json()
    assert data["type"] == "sync_completed"
    briefing = data["briefing"]
    assert briefing is not None

    statuses = {m["member_id"]: m["status"] for m in briefing["member_statuses"]}
    assert statuses["ou_alice"] == "collected"
    assert statuses["ou_bob"] == "no_response"
    assert statuses["ou_david"] == "no_response"
    assert any("未响应" in rec for rec in briefing["recommended_actions"])

    # Completing again with no live session reports the idle state, not a crash.
    again = await client.post(f"{base}/sync/complete")
    assert again.status_code == 200
    assert again.json()["type"] == "no_active_session"


async def test_new_sync_supersedes_previous_active_session(client: httpx.AsyncClient) -> None:
    """Re-initiating a sync must close the previous live session so stale probes
    cannot accumulate as zombie clarifying sessions across restarts."""
    base = "/api/v1/projects/proj-supersede"
    await client.post(f"{base}/bootstrap-sandbox")

    first = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "我要知道当前的项目进度"},
    )
    first_session_id = first.json()["session_id"]

    second = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "我要同步进度"},
    )
    assert second.json()["type"] == "sync_started"
    second_session_id = second.json()["session_id"]
    assert second_session_id != first_session_id

    first_detail = (await client.get(f"{base}/sync-sessions/{first_session_id}")).json()
    assert first_detail["status"] == "superseded"
    second_detail = (await client.get(f"{base}/sync-sessions/{second_session_id}")).json()
    assert second_detail["status"] in {"probing", "clarifying"}

    # A member reply must land in the live session, not the superseded one.
    alice = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_alice", "message": "支付SDK接入一切正常，按计划推进"},
    )
    assert alice.status_code == 200
    alive = (await client.get(f"{base}/sync-sessions/{second_session_id}")).json()
    assert alive["member_probes"]["ou_alice"]["status"] == "collected"


async def test_pm_directive_message_is_routed_as_directive(client: httpx.AsyncClient) -> None:
    """Regression for the live-test failure: a PM directive used to fall into
    'no task state change' silence; it must now be recognized as an directive."""
    base = "/api/v1/projects/proj-intent"
    await client.post(f"{base}/bootstrap-sandbox")

    resp = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "告诉Alice，必须在明天上午12点之前完成"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "normal_turn"
    assert data["intent"] == "directive"
    assert "指令" in data["bot_reply"]

    # A plain status report keeps flowing through the health pipeline.
    report = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_alice", "message": "支付SDK接入一切正常，按原计划推进"},
    )
    assert report.status_code == 200
    report_data = report.json()
    assert report_data["intent"] == "health_report"
    assert report_data["is_actionable"] is True


async def test_directive_full_lifecycle_end_to_end(client: httpx.AsyncClient) -> None:
    """PM order -> relayed to target pane -> ack -> completion -> reminders."""
    base = "/api/v1/projects/proj-directive"
    await client.post(f"{base}/bootstrap-sandbox")

    issue = await client.post(
        f"{base}/sandbox-chat",
        params={
            "actor_id": "ou_pm",
            "message": "告诉Alice，支付SDK必须在明天下午5点之前完成",
        },
    )
    assert issue.status_code == 200
    data = issue.json()
    assert data["type"] == "normal_turn"
    assert data["intent"] == "directive"
    assert data["directive"] == "issued"
    assert "已下达给" in data["bot_reply"]
    assert any(n["actor_id"] == "ou_alice" for n in data["notices"])

    ambiguous = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "告诉Alice，必须在明天上午12点之前完成"},
    )
    amb_data = ambiguous.json()
    assert amb_data["directive"] == "clarify"
    assert "中午 12:00" in amb_data["bot_reply"]

    ack = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_alice", "message": "收到，马上处理"},
    )
    ack_data = ack.json()
    assert ack_data["directive"] == "acknowledged"
    assert any(n["actor_id"] == "ou_pm" for n in ack_data["notices"])

    done = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_alice", "message": "支付SDK已完成，已交付"},
    )
    done_data = done.json()
    assert done_data["directive"] == "completed"
    assert any(n["actor_id"] == "ou_pm" for n in done_data["notices"])

    # With the directive closed, the reminder endpoint reports nothing open.
    remind = await client.post(f"{base}/directives/remind")
    assert remind.status_code == 200
    assert remind.json()["type"] == "none"


async def test_directive_remind_before_ack(client: httpx.AsyncClient) -> None:
    base = "/api/v1/projects/proj-dir-remind"
    await client.post(f"{base}/bootstrap-sandbox")

    issue = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "告诉David，压测方案今天下班前给我"},
    )
    assert issue.json()["directive"] == "issued"

    remind = await client.post(f"{base}/directives/remind")
    remind_data = remind.json()
    assert remind_data["type"] == "reminded"
    assert any(n["actor_id"] == "ou_david" for n in remind_data["notices"])
    events = (await client.get(f"{base}/events")).json()
    reminded = [event for event in events if event["event_type"] == "directive.reminded"]
    assert reminded[-1]["payload"]["reminded_by"] == "ou_pm"

    # A second reminder pass reminds again (reminder_count increments).
    again = await client.post(f"{base}/directives/remind")
    assert again.json()["type"] == "reminded"


async def test_directive_remind_requires_project_operator(
    client: httpx.AsyncClient,
) -> None:
    base = "/api/v1/projects/proj-remind-no-operator"
    event = {
        "schema_version": 1,
        "project_id": "proj-remind-no-operator",
        "event_id": "evt-only-engineer",
        "event_type": "member.registered",
        "source": "human",
        "source_ref": "setup",
        "occurred_at": NOW.isoformat(),
        "received_at": NOW.isoformat(),
        "payload": {
            "member_id": "alice",
            "display_name": "Alice",
            "role": "engineer",
        },
    }
    assert (await client.post(f"{base}/events", json={"events": [event]})).status_code == 200

    remind = await client.post(f"{base}/directives/remind")

    assert remind.status_code == 409
    assert remind.json()["detail"] == "Project has no PM/lead operator"


async def test_directive_state_survives_gateway_restart(tmp_path) -> None:
    db_path = tmp_path / "directive-restart.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-dir-restart"
    await service.ingest_raw_events(project_id, _make_setup_events(project_id))
    result = await service.ingest_message(
        project_id=project_id,
        message="告诉alice，Backend API 必须在明天下午5点之前完成",
        actor_id="carol",
        occurred_at=NOW,
    )
    assert result.directive_kind == "issued"
    await db.close()

    restarted_db = Database(f"sqlite+aiosqlite:///{db_path}")
    await restarted_db.init_db()
    restarted = GatewayService(restarted_db)
    agent = await restarted.get_or_replay_agent(project_id)
    assert len(agent.projector.state.directives) == 1
    directive = next(iter(agent.projector.state.directives.values()))
    assert directive.target_id == "alice"
    assert directive.status.value == "issued"
    await restarted_db.close()


async def test_sync_session_survives_gateway_restart(tmp_path) -> None:
    """A restart mid-collection must restore the active sync session so member
    replies are not orphaned and the scatter-gather cycle can complete."""
    db_path = tmp_path / "restart.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-restart"
    await service.ingest_raw_events(project_id, _make_setup_events(project_id))
    session = await service.start_progress_sync(project_id, initiated_by="carol")
    await db.close()

    restarted_db = Database(f"sqlite+aiosqlite:///{db_path}")
    await restarted_db.init_db()
    restarted = GatewayService(restarted_db)
    coordinator = await restarted.get_sync_coordinator(project_id)
    active = coordinator.get_active_session(project_id)
    assert active is not None
    assert active.session_id == session.session_id

    converged, clarify_q, _ = await restarted.handle_sync_member_reply(
        project_id=project_id,
        member_id="alice",
        message="Backend API 一切正常，按计划推进",
        occurred_at=NOW,
    )
    assert converged is True
    assert clarify_q is None
    completed = coordinator.get_session(session.session_id)
    assert completed is not None
    assert completed.status.value == "completed"
    assert completed.briefing is not None
    await restarted_db.close()


async def test_stale_agent_catches_up_without_duplicate_actions() -> None:
    """An agent materialized before a member's report must catch up on persisted
    events and cases before its turn, so it does not double-coordinate."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-catchup"
    await service.ingest_raw_events(project_id, _make_setup_events(project_id))

    stale_a = await service.get_or_replay_agent(project_id)
    stale_b = await service.get_or_replay_agent(project_id)
    await service.ingest_raw_events(
        project_id, [_health_report_event(project_id, "backend_api", "at_risk")]
    )

    trace_a, _ = await service.run_agent_turn(stale_a, [], NOW)
    trace_b, _ = await service.run_agent_turn(stale_b, [], NOW)

    executed = len(trace_a.executed_command_ids) + len(trace_b.executed_command_ids)
    assert executed == 1
    cases = await service.state_store.load_cases(project_id)
    assert len(cases) == 1
    assert cases[0].status.value == "waiting_for_response"
    await db.close()


async def test_concurrent_member_turns_serialize_without_duplicates() -> None:
    """Two turns racing for the same project must run serialized behind the
    per-project lock and produce exactly one coordination action."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-concurrent"
    await service.ingest_raw_events(project_id, _make_setup_events(project_id))

    agent_a = await service.get_or_replay_agent(project_id)
    agent_b = await service.get_or_replay_agent(project_id)
    await service.ingest_raw_events(
        project_id, [_health_report_event(project_id, "backend_api", "at_risk")]
    )

    results = await asyncio.gather(
        service.run_agent_turn(agent_a, [], NOW),
        service.run_agent_turn(agent_b, [], NOW),
    )
    trace_a, _ = results[0]
    trace_b, _ = results[1]

    executed = len(trace_a.executed_command_ids) + len(trace_b.executed_command_ids)
    assert executed == 1
    cases = await service.state_store.load_cases(project_id)
    assert len(cases) == 1
    await db.close()


async def test_message_from_unregistered_member_auto_registers(
    client: httpx.AsyncClient,
) -> None:
    """Regression: a message from an unseen Feishu account used to persist an
    unprojectable health event, permanently bricking the project replay (500 on
    every endpoint). The sender must be auto-registered and the claim processed."""
    setup_events = _make_setup_events("proj-auto-register")
    await client.post("/api/v1/projects/proj-auto-register/events", json={"events": setup_events})

    resp = await client.post(
        "/api/v1/projects/proj-auto-register/sandbox-chat",
        params={"actor_id": "ou_newcomer", "message": "backend_api 报错，排查需要到明天下午 5 点"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "normal_turn"
    assert body["is_actionable"] is True

    # Project endpoints stay healthy (replay is not bricked) and the member exists.
    state = (await client.get("/api/v1/projects/proj-auto-register/state")).json()
    assert "ou_newcomer" in state["members"]
    assert "backend_api" in state["tasks"]


async def test_gateway_bootstrap_sandbox_is_idempotent(client: httpx.AsyncClient) -> None:
    """Regression: double-clicking the demo bootstrap used to append duplicate
    member registrations, permanently bricking the project replay (500 on every
    endpoint). A repeated bootstrap must be a no-op top-up."""
    first = await client.post("/api/v1/projects/proj-boot-twice/bootstrap-sandbox")
    assert first.status_code == 200
    assert first.json() == {"status": "initialized", "members_count": 4, "tasks_count": 3}

    second = await client.post("/api/v1/projects/proj-boot-twice/bootstrap-sandbox")
    assert second.status_code == 200
    assert second.json()["members_count"] == 0
    assert second.json()["tasks_count"] == 0

    state = await client.get("/api/v1/projects/proj-boot-twice/state")
    assert state.status_code == 200
    assert len(state.json()["tasks"]) == 3


async def test_app_lifespan() -> None:
    app = create_app()
    assert app.title == "OrgPilot Event Gateway"


async def test_directive_clarification_multiturn_closes_loop(client: httpx.AsyncClient) -> None:
    """P1 regression: answering the ambiguity question must issue the original
    directive with restored context, not lose it (ask-only is not a loop)."""
    base = "/api/v1/projects/proj-dir-clarify"
    await client.post(f"{base}/bootstrap-sandbox")

    ask = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "告诉Alice，必须在明天上午12点之前完成"},
    )
    assert ask.json()["directive"] == "clarify"

    answer = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "中午12点"},
    )
    data = answer.json()
    assert data["directive"] == "issued"
    assert "Alice" in data["bot_reply"]

    # The loop truly closed: Alice can now acknowledge the issued directive.
    ack = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_alice", "message": "收到，马上处理"},
    )
    assert ack.json()["directive"] == "acknowledged"


async def test_pending_clarification_survives_gateway_restart(tmp_path) -> None:
    """A restart between the clarification question and the issuer's answer must
    not orphan the draft: the answer still issues the original directive."""
    db_path = tmp_path / "clarify-restart.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-clarify-restart"
    await service.ingest_raw_events(project_id, _make_setup_events(project_id))
    ask = await service.ingest_message(
        project_id=project_id,
        message="告诉alice，必须在明天上午12点之前完成",
        actor_id="carol",
        occurred_at=NOW,
    )
    assert ask.directive_kind == "clarify"
    await db.close()

    restarted_db = Database(f"sqlite+aiosqlite:///{db_path}")
    await restarted_db.init_db()
    restarted = GatewayService(restarted_db)
    answer = await restarted.ingest_message(
        project_id=project_id,
        message="中午12点",
        actor_id="carol",
        occurred_at=NOW,
    )
    assert answer.directive_kind == "issued"
    agent = await restarted.get_or_replay_agent(project_id)
    directive = next(iter(agent.projector.state.directives.values()))
    assert directive.target_id == "alice"
    assert directive.deadline is not None
    assert agent.projector.state.pending_directive_clarifications == {}
    await restarted_db.close()


async def test_nl_task_create_full_approval_loop(client: httpx.AsyncClient) -> None:
    """M3 finale: PM chat -> gated proposal -> approval -> task lands + notify."""
    base = "/api/v1/projects/proj-task-create"
    await client.post(f"{base}/bootstrap-sandbox")

    propose = await client.post(
        f"{base}/sandbox-chat",
        params={
            "actor_id": "ou_pm",
            "message": "新增一个任务：网关压测脚本，由David负责，周五前完成",
        },
    )
    data = propose.json()
    assert data["intent"] == "task_create"
    assert data["directive"] == "proposed"
    assert "提案" in data["bot_reply"]

    approvals = (await client.get(f"{base}/approvals")).json()
    assert len(approvals) == 1
    payload = approvals[0]["proposed_command"]["payload"]
    assert payload["proposal_kind"] == "task_create"
    assert payload["task_title"] == "网关压测脚本"
    assert payload["owner_id"] == "ou_david"
    approval_id = approvals[0]["approval_id"]

    # The task must NOT exist before approval (check the DAG, not the whole
    # state payload whose approvals section quotes the proposed title).
    dag = (await client.get(f"{base}/dag")).json()
    assert "网关压测脚本" not in json.dumps(dag, ensure_ascii=False)

    decision = await client.post(
        f"{base}/approvals/{approval_id}/decision",
        json={"decision": "approved", "approver_id": "ou_pm", "reason": "ok"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "approved"
    assert "已创建" in (decision.json()["bot_reply"] or "")

    state = (await client.get(f"{base}/state")).json()
    titles = json.dumps(state, ensure_ascii=False)
    assert "网关压测脚本" in titles

    # The new owner got the assignment notification through the outbox.
    outbox = (await client.get(f"{base}/outbox")).json()
    assert outbox["pending_count"] == 0
    assert any(row["status"] == "delivered" for row in outbox["rows"])


async def test_nl_task_create_member_declined(client: httpx.AsyncClient) -> None:
    base = "/api/v1/projects/proj-task-member"
    await client.post(f"{base}/bootstrap-sandbox")
    propose = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_alice", "message": "新增任务：部署流水线，由Bob负责"},
    )
    data = propose.json()
    assert data["intent"] == "task_create"
    assert data["directive"] == "declined"
    assert "无权限" in data["bot_reply"]
    approvals = (await client.get(f"{base}/approvals")).json()
    assert approvals == []


async def test_nl_task_reassign_full_approval_loop(client: httpx.AsyncClient) -> None:
    base = "/api/v1/projects/proj-task-reassign"
    await client.post(f"{base}/bootstrap-sandbox")

    propose = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "把收银台前端结账转给Alice"},
    )
    data = propose.json()
    assert data["intent"] == "task_reassign"
    assert data["directive"] == "proposed"

    approvals = (await client.get(f"{base}/approvals")).json()
    assert len(approvals) == 1
    payload = approvals[0]["proposed_command"]["payload"]
    assert payload["proposal_kind"] == "task_reassign"
    assert payload["owner_id"] == "ou_alice"
    assert payload["previous_owner_id"] != "ou_alice"
    approval_id = approvals[0]["approval_id"]

    decision = await client.post(
        f"{base}/approvals/{approval_id}/decision",
        json={"decision": "approved", "approver_id": "ou_pm"},
    )
    assert decision.status_code == 200
    assert "改派" in (decision.json()["bot_reply"] or "")

    dag = (await client.get(f"{base}/dag")).json()
    nodes = json.dumps(dag, ensure_ascii=False)
    assert "收银台前端结账" in nodes


async def test_nl_deadline_change_full_approval_loop(client: httpx.AsyncClient) -> None:
    """PM chat -> dependency-aware approval -> deadline event + notifications."""
    base = "/api/v1/projects/proj-task-deadline"
    await client.post(f"{base}/bootstrap-sandbox")

    propose = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "支付SDK接入截止时间改到后天下午5点"},
    )
    data = propose.json()
    assert data["intent"] == "deadline_change"
    assert data["directive"] == "proposed"
    assert "依赖分析" in data["bot_reply"]

    approvals = (await client.get(f"{base}/approvals")).json()
    assert len(approvals) == 1
    approval = approvals[0]
    payload = approval["proposed_command"]["payload"]
    assert payload["proposal_kind"] == "deadline_change"
    assert payload["task_id"] == "task-payment"
    assert payload["impacted_tasks"] == ["task-checkout", "task-qa"]
    assert approval["proposed_command"]["targets"] == ["ou_pm"]

    decision = await client.post(
        f"{base}/approvals/{approval['approval_id']}/decision",
        json={"decision": "approved", "approver_id": "ou_pm"},
    )
    assert decision.status_code == 200
    assert "截止时间已更新" in (decision.json()["bot_reply"] or "")

    state = (await client.get(f"{base}/state")).json()
    assert state["tasks"]["task-payment"]["deadline"] == payload["new_deadline"]
    outbox = (await client.get(f"{base}/outbox")).json()
    delivered = [row for row in outbox["rows"] if row["status"] == "delivered"]
    assert sum(row["action_type"] == "send_directive" for row in delivered) == 3
    assert any(row["action_type"] == "propose_reschedule" for row in delivered)


async def test_task_approval_survives_gateway_restart(tmp_path) -> None:
    """A pending task proposal persists; approval after a restart still lands."""
    db_path = tmp_path / "task-approval.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.init_db()
    settings = OrgPilotSettings(collaboration_adapter="mock", feishu_use_ws=False)
    app = create_app(db, settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        base = "/api/v1/projects/proj-task-restart"
        await ac.post(f"{base}/bootstrap-sandbox")
        propose = await ac.post(
            f"{base}/sandbox-chat",
            params={"actor_id": "ou_pm", "message": "新增任务：回归测试清单，由David负责"},
        )
        assert propose.json()["directive"] == "proposed"
    await db.close()

    restarted_db = Database(f"sqlite+aiosqlite:///{db_path}")
    await restarted_db.init_db()
    restarted_app = create_app(restarted_db, settings=settings)
    restarted_transport = httpx.ASGITransport(app=restarted_app)
    async with httpx.AsyncClient(transport=restarted_transport, base_url="http://test") as ac:
        approvals = (await ac.get("/api/v1/projects/proj-task-restart/approvals")).json()
        assert len(approvals) == 1
        approval_id = approvals[0]["approval_id"]
        decision = await ac.post(
            f"/api/v1/projects/proj-task-restart/approvals/{approval_id}/decision",
            json={"decision": "approved", "approver_id": "ou_pm"},
        )
        assert decision.status_code == 200
        state = (await ac.get("/api/v1/projects/proj-task-restart/state")).json()
        assert "回归测试清单" in json.dumps(state, ensure_ascii=False)
    await restarted_db.close()

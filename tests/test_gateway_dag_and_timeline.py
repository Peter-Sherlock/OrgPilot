"""Tests for DAG topology, critical path analysis, and explainability timeline routes."""

from datetime import datetime, timedelta

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.enums import WorkflowStatus
from orgpilot.events.models import (
    EventSource,
    HealthStatus,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    TaskCreatedEvent,
    TaskCreatedPayload,
    TaskHealthReportedEvent,
    TaskHealthReportedPayload,
)
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


async def _setup_sample_project_events(client: httpx.AsyncClient, project_id: str) -> None:
    events = [
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"evt-{project_id}-alice",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=MemberRegisteredPayload(
                member_id="alice", display_name="Alice", role="backend"
            ),
        ),
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"evt-{project_id}-bob",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=MemberRegisteredPayload(member_id="bob", display_name="Bob", role="frontend"),
        ),
        MemberRegisteredEvent(
            project_id=project_id,
            event_id=f"evt-{project_id}-carol",
            event_type="member.registered",
            source=EventSource.HUMAN,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=MemberRegisteredPayload(member_id="carol", display_name="Carol", role="qa"),
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
                task_id="backend_api",
                title="后端 API 开发",
                owner_id="alice",
                workflow_status=WorkflowStatus.DOING,
                deadline=NOW + timedelta(hours=4),
            ),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id=f"evt-{project_id}-task-ui",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskCreatedPayload(
                task_id="frontend_ui",
                title="前端界面开发",
                owner_id="bob",
                workflow_status=WorkflowStatus.TODO,
                dependencies=("backend_api",),
                deadline=NOW + timedelta(hours=8),
            ),
        ),
        TaskCreatedEvent(
            project_id=project_id,
            event_id=f"evt-{project_id}-task-qa",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload=TaskCreatedPayload(
                task_id="qa_test",
                title="整体验收测试",
                owner_id="carol",
                workflow_status=WorkflowStatus.TODO,
                dependencies=("frontend_ui",),
                deadline=NOW + timedelta(hours=12),
            ),
        ),
    ]
    raw_dicts = [e.model_dump(mode="json") for e in events]
    await client.post(f"/api/v1/projects/{project_id}/events", json={"events": raw_dicts})


async def test_get_dag_topology_and_layers(client: httpx.AsyncClient) -> None:
    project_id = "test-dag-proj"
    await _setup_sample_project_events(client, project_id)

    # 1. Fetch DAG
    resp = await client.get(f"/api/v1/projects/{project_id}/dag")
    assert resp.status_code == 200
    data = resp.json()

    assert data["project_id"] == project_id
    assert len(data["nodes"]) == 3
    assert len(data["edges"]) == 2

    # Check summary
    summary = data["summary"]
    assert summary["total_tasks"] == 3
    assert summary["critical_path"] == ["backend_api", "frontend_ui", "qa_test"]

    # Check node layers
    nodes_by_id = {n["task_id"]: n for n in data["nodes"]}
    assert nodes_by_id["backend_api"]["layer"] == 0
    assert nodes_by_id["frontend_ui"]["layer"] == 1
    assert nodes_by_id["qa_test"]["layer"] == 2


async def test_get_dag_with_delayed_risk_propagation(client: httpx.AsyncClient) -> None:
    project_id = "test-dag-risk"
    await _setup_sample_project_events(client, project_id)

    # Report backend_api as DELAYED
    delay_event = TaskHealthReportedEvent(
        project_id=project_id,
        event_id=f"evt-{project_id}-delay",
        event_type="task.health_reported",
        source=EventSource.MESSAGE,
        source_ref="chat",
        actor_id="alice",
        occurred_at=NOW + timedelta(minutes=5),
        received_at=NOW + timedelta(minutes=5),
        payload=TaskHealthReportedPayload(
            task_id="backend_api",
            health_status=HealthStatus.DELAYED,
            blocker="第三方 SDK 报错",
            confidence=0.95,
        ),
    )
    await client.post(
        f"/api/v1/projects/{project_id}/events",
        json={"events": [delay_event.model_dump(mode="json")]},
    )

    resp = await client.get(f"/api/v1/projects/{project_id}/dag")
    assert resp.status_code == 200
    data = resp.json()

    nodes_by_id = {n["task_id"]: n for n in data["nodes"]}
    assert nodes_by_id["backend_api"]["is_delayed"] is True
    assert nodes_by_id["frontend_ui"]["is_at_risk"] is True
    assert nodes_by_id["qa_test"]["is_at_risk"] is True

    # Check impacted tasks in summary
    assert "frontend_ui" in data["summary"]["impacted_tasks"]
    assert "qa_test" in data["summary"]["impacted_tasks"]


async def test_get_project_timeline(client: httpx.AsyncClient) -> None:
    project_id = "test-timeline-proj"
    await _setup_sample_project_events(client, project_id)

    resp = await client.get(f"/api/v1/projects/{project_id}/timeline")
    assert resp.status_code == 200
    data = resp.json()

    assert data["project_id"] == project_id
    assert data["total_entries"] >= 6
    assert len(data["entries"]) == data["total_entries"]

    # Verify category entries exist
    categories = {e["category"] for e in data["entries"]}
    assert "event" in categories


async def test_serve_dashboard_html(client: httpx.AsyncClient) -> None:
    # 1. Root /
    resp_root = await client.get("/")
    assert resp_root.status_code == 200
    assert "text/html" in resp_root.headers.get("content-type", "")
    assert "OrgPilot Console" in resp_root.text
    assert "dagSvg" in resp_root.text

    # 2. /dashboard
    resp_dash = await client.get("/dashboard")
    assert resp_dash.status_code == 200
    assert "OrgPilot Console" in resp_dash.text


async def test_dag_with_done_task_and_empty_project(client: httpx.AsyncClient) -> None:
    # Empty project DAG
    resp_empty = await client.get("/api/v1/projects/empty-proj/dag")
    assert resp_empty.status_code == 200
    assert resp_empty.json()["summary"]["total_tasks"] == 0

    # Project with DONE task
    project_id = "test-done-proj"
    member_event = MemberRegisteredEvent(
        project_id=project_id,
        event_id="evt-done-alice",
        event_type="member.registered",
        source=EventSource.HUMAN,
        source_ref="setup",
        occurred_at=NOW,
        received_at=NOW,
        payload=MemberRegisteredPayload(member_id="alice", display_name="Alice", role="backend"),
    )
    done_event = TaskCreatedEvent(
        project_id=project_id,
        event_id="evt-done-task",
        event_type="task.created",
        source=EventSource.TASK,
        source_ref="setup",
        occurred_at=NOW,
        received_at=NOW,
        payload=TaskCreatedPayload(
            task_id="finished_task",
            title="已完成任务",
            owner_id="alice",
            workflow_status=WorkflowStatus.DONE,
            deadline=NOW,
        ),
    )
    await client.post(
        f"/api/v1/projects/{project_id}/events",
        json={"events": [member_event.model_dump(mode="json"), done_event.model_dump(mode="json")]},
    )

    resp = await client.get(f"/api/v1/projects/{project_id}/dag")
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["completed_count"] == 1


async def test_timeline_with_cases_and_approvals(client: httpx.AsyncClient) -> None:
    project_id = "test-timeline-full"
    await _setup_sample_project_events(client, project_id)

    # Ingest message that creates delay and triggers approval
    await client.post(
        f"/api/v1/projects/{project_id}/messages",
        json={
            "actor_id": "alice",
            "message": "支付 SDK 报错，排查需要到明天下午 5 点",
            "occurred_at": NOW.isoformat(),
            "auto_run_turn": True,
        },
    )

    resp = await client.get(f"/api/v1/projects/{project_id}/timeline")
    assert resp.status_code == 200
    data = resp.json()

    categories = {e["category"] for e in data["entries"]}
    assert "event" in categories
    assert "case" in categories
    assert "approval" in categories

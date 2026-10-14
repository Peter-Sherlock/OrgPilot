"""B5 regression tests: relative time must resolve in the business timezone.

Feishu delivers message timestamps as UTC epoch millis. Before the fix the claim
extractor anchored "明天下午5点" to the UTC date, so a Beijing user saying
"明天下午5点" at Beijing 10:05 got 17:00 UTC (= 01:00+08:00 next day) instead of
17:00+08:00.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.app import create_app
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database

PROJECT_ID = "tz-project"
BJ = ZoneInfo("Asia/Shanghai")

UTC_0205_MS = str(int(datetime(2026, 10, 13, 2, 5, tzinfo=UTC).timestamp() * 1000))
UTC_2300_MS = str(int(datetime(2026, 10, 13, 23, 0, tzinfo=UTC).timestamp() * 1000))


async def _seed_single_task_service() -> tuple[GatewayService, Database]:
    """Fresh in-memory service with one registered member and one known task, so
    the mock extractor's single-task fallback matches every message."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    service = GatewayService(db)
    setup = [
        {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "event_id": "evt-tz-alice",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": "2026-10-13T09:00:00+08:00",
            "received_at": "2026-10-13T09:00:00+08:00",
            "payload": {"member_id": "ou_alice", "display_name": "Alice", "role": "backend"},
        },
        {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "event_id": "evt-tz-task",
            "event_type": "task.created",
            "source": "task",
            "source_ref": "setup",
            "occurred_at": "2026-10-13T09:00:00+08:00",
            "received_at": "2026-10-13T09:00:00+08:00",
            "payload": {
                "task_id": "backend_api",
                "title": "后端接口",
                "owner_id": "ou_alice",
                "workflow_status": "doing",
                "deadline": "2026-10-30T18:00:00+08:00",
            },
        },
    ]
    await service.ingest_raw_events(PROJECT_ID, setup)
    return service, db


async def _last_health_claim(service: GatewayService):
    events = await service.event_store.get_events(PROJECT_ID)
    claims = [e for e in events if e.event_type == "task.health_reported"]
    assert claims, "expected one health claim event"
    return claims[-1].payload


async def test_tomorrow_afternoon_resolves_in_business_timezone() -> None:
    service, db = await _seed_single_task_service()
    try:
        handler = FeishuWebhookHandler(service=service, project_id=PROJECT_ID)
        response = await handler.handle_event(
            {
                "header": {"event_id": "evt-tz-1", "event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
                    "message": {
                        "message_id": "om-tz-1",
                        "message_type": "text",
                        "content": '{"text": "支付SDK报错，明天下午5点能修好"}',
                        "create_time": UTC_0205_MS,
                        "chat_type": "p2p",
                    },
                },
            }
        )
        assert response["msg"] == "success"

        payload = await _last_health_claim(service)
        # UTC 02:05 is Beijing 10:05; "明天下午5点" must be next day 17:00+08:00.
        assert payload.expected_completion == datetime(2026, 10, 14, 17, 0, tzinfo=BJ)
    finally:
        await db.close()


async def test_late_evening_tomorrow_uses_business_date() -> None:
    service, db = await _seed_single_task_service()
    try:
        handler = FeishuWebhookHandler(service=service, project_id=PROJECT_ID)
        await handler.handle_event(
            {
                "header": {"event_id": "evt-tz-2", "event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
                    "message": {
                        "message_id": "om-tz-2",
                        "message_type": "text",
                        "content": '{"text": "支付SDK报错，明天能修好"}',
                        # UTC 23:00 on 10-13 is Beijing 07:00 on 10-14: "明天" is 10-15.
                        "create_time": UTC_2300_MS,
                        "chat_type": "p2p",
                    },
                },
            }
        )

        payload = await _last_health_claim(service)
        assert payload.expected_completion == datetime(2026, 10, 15, 18, 0, tzinfo=BJ)
    finally:
        await db.close()


async def test_messages_route_normalizes_occurred_at() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    settings = OrgPilotSettings(collaboration_adapter="mock", feishu_use_ws=False)
    app = create_app(db, settings=settings)
    try:
        service: GatewayService = app.state.gateway_service
        await service.ingest_raw_events(PROJECT_ID, await _setup_events())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(
                f"/api/v1/projects/{PROJECT_ID}/messages",
                json={
                    "message": "支付SDK报错，明天下午5点能修好",
                    "actor_id": "ou_alice",
                    "occurred_at": "2026-10-13T02:05:00+00:00",
                    "message_id": "om-tz-3",
                    "auto_run_turn": False,
                },
            )
            assert res.status_code == 200

        payload = await _last_health_claim(service)
        assert payload.expected_completion == datetime(2026, 10, 14, 17, 0, tzinfo=BJ)
    finally:
        await db.close()


async def _setup_events() -> list[dict]:
    return [
        {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "event_id": "evt-tz-alice-api",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": "2026-10-13T09:00:00+08:00",
            "received_at": "2026-10-13T09:00:00+08:00",
            "payload": {"member_id": "ou_alice", "display_name": "Alice", "role": "backend"},
        },
        {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "event_id": "evt-tz-task-api",
            "event_type": "task.created",
            "source": "task",
            "source_ref": "setup",
            "occurred_at": "2026-10-13T09:00:00+08:00",
            "received_at": "2026-10-13T09:00:00+08:00",
            "payload": {
                "task_id": "backend_api",
                "title": "后端接口",
                "owner_id": "ou_alice",
                "workflow_status": "doing",
                "deadline": "2026-10-30T18:00:00+08:00",
            },
        },
    ]


def test_settings_timezone_default_validation_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = OrgPilotSettings()
    assert settings.timezone == "Asia/Shanghai"
    assert settings.business_timezone == ZoneInfo("Asia/Shanghai")

    parsed = OrgPilotSettings(timezone="UTC")
    assert parsed.business_timezone == ZoneInfo("UTC")

    monkeypatch.setenv("ORGPILOT_TIMEZONE", "Asia/Tokyo")
    assert OrgPilotSettings.from_env().timezone == "Asia/Tokyo"

    with pytest.raises(ValueError, match="ORGPILOT_TIMEZONE"):
        OrgPilotSettings(timezone="Mars/Olympus").validate()


def test_to_business_timezone_handles_naive_input() -> None:
    service = GatewayService(Database("sqlite+aiosqlite:///:memory:"))
    converted = service.to_business_timezone(datetime(2026, 10, 13, 2, 5))
    assert converted.utcoffset() is not None
    assert converted == datetime(2026, 10, 13, 2, 5, tzinfo=UTC).astimezone(BJ)

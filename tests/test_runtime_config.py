"""Runtime configuration, provider wiring, and inbound authentication tests."""

from unittest.mock import AsyncMock

import httpx
import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.extraction.client import AnthropicCompatibleLLMClient, MockLLMClient
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.runtime import build_feishu_adapter_factory
from orgpilot.gateway.app import create_app
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database
from tests.test_gateway_api import NOW, _make_setup_events


def test_settings_from_env_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORGPILOT_DATABASE_URL", "postgresql+asyncpg://db.example/orgpilot")
    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "aihubmix")
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("AIHUBMIX_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("ORGPILOT_LLM_REASONING_EFFORT", "none")

    settings = OrgPilotSettings.from_env()

    assert settings.database_url == "postgresql+asyncpg://db.example/orgpilot"
    assert settings.llm_provider == "aihubmix"
    assert settings.aihubmix_model == "gpt-5.6-luna"
    assert settings.llm_reasoning_effort == "none"
    assert "test-key" not in repr(settings)

    with pytest.raises(ValueError, match="ORGPILOT_LLM_PROVIDER"):
        OrgPilotSettings(llm_provider="unknown").validate()
    with pytest.raises(ValueError, match="AIHUBMIX_API_KEY"):
        OrgPilotSettings(llm_provider="aihubmix").validate()
    with pytest.raises(ValueError, match="Missing required Feishu settings"):
        OrgPilotSettings(collaboration_adapter="feishu").validate()


def test_demo_bootstrap_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert OrgPilotSettings().demo_bootstrap is False
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("ORGPILOT_DEMO_BOOTSTRAP", value)
        assert OrgPilotSettings.from_env().demo_bootstrap is True
    for value in ("false", "0", "no", "off"):
        monkeypatch.setenv("ORGPILOT_DEMO_BOOTSTRAP", value)
        assert OrgPilotSettings.from_env().demo_bootstrap is False


def test_feishu_write_gate_is_closed_by_default_and_explicitly_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert OrgPilotSettings().feishu_allow_writes is False
    monkeypatch.setenv("ORGPILOT_FEISHU_ALLOW_WRITES", "true")
    assert OrgPilotSettings.from_env().feishu_allow_writes is True
    monkeypatch.setenv("ORGPILOT_FEISHU_ALLOW_WRITES", "false")
    assert OrgPilotSettings.from_env().feishu_allow_writes is False


def test_reference_timezone_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORGPILOT_TIMEZONE", "Asia/Shanghai")
    assert OrgPilotSettings.from_env().reference_timezone == "Asia/Shanghai"
    monkeypatch.setenv("ORGPILOT_TIMEZONE", "Not/AZone")
    with pytest.raises(ValueError, match="ORGPILOT_TIMEZONE"):
        OrgPilotSettings.from_env()


def test_aihubmix_client_is_only_enabled_explicitly() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    mock_app = create_app(db, settings=OrgPilotSettings())
    assert isinstance(mock_app.state.gateway_service.extractor.llm_client, MockLLMClient)

    live_app = create_app(
        db,
        settings=OrgPilotSettings(
            llm_provider="aihubmix",
            aihubmix_api_key="test-key",
            llm_reasoning_effort="none",
        ),
    )
    live_client = live_app.state.gateway_service.extractor.llm_client
    assert isinstance(live_client, AnthropicCompatibleLLMClient)
    assert live_client.reasoning_effort == "none"
    live_client.close()


async def test_feishu_adapter_wiring_is_explicit() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    mock_app = create_app(db, settings=OrgPilotSettings())
    mock_agent = await mock_app.state.gateway_service.get_or_replay_agent("project-1")
    assert isinstance(mock_agent.adapter, MockCollaborationAdapter)

    feishu_app = create_app(
        db,
        settings=OrgPilotSettings(
            collaboration_adapter="feishu",
            feishu_app_id="cli_test",
            feishu_app_secret="secret_test",
            feishu_verification_token="verify-test",
        ),
    )
    feishu_agent = await feishu_app.state.gateway_service.get_or_replay_agent("project-1")
    assert isinstance(feishu_agent.adapter, FeishuCollaborationAdapter)
    await db.close()


def test_real_feishu_adapter_factory_preserves_write_gate() -> None:
    closed = build_feishu_adapter_factory(
        OrgPilotSettings(
            collaboration_adapter="feishu",
            feishu_app_id="cli_test",
            feishu_app_secret="secret_test",
        )
    )("project-1")
    opened = build_feishu_adapter_factory(
        OrgPilotSettings(
            collaboration_adapter="feishu",
            feishu_app_id="cli_test",
            feishu_app_secret="secret_test",
            feishu_allow_writes=True,
        )
    )("project-1")

    assert isinstance(closed, FeishuCollaborationAdapter)
    assert closed.client.allow_writes is False
    assert isinstance(opened, FeishuCollaborationAdapter)
    assert opened.client.allow_writes is True


async def test_closed_feishu_write_gate_disables_listener_and_outbox_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = False

    def record_start(_listener) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr("orgpilot.gateway.app.FeishuWebSocketListener.start", record_start)
    db = Database("sqlite+aiosqlite:///:memory:")
    app = create_app(
        db,
        settings=OrgPilotSettings(
            collaboration_adapter="feishu",
            feishu_app_id="cli_test",
            feishu_app_secret="secret_test",
            feishu_use_ws=True,
            feishu_allow_writes=False,
        ),
    )
    sweep = AsyncMock()
    app.state.gateway_service.sweep_outbox = sweep

    async with app.router.lifespan_context(app):
        pass

    assert started is False
    sweep.assert_not_awaited()


async def test_closed_feishu_write_gate_rejects_events_but_allows_url_verification() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    app = create_app(
        db,
        settings=OrgPilotSettings(
            collaboration_adapter="feishu",
            feishu_app_id="cli_test",
            feishu_app_secret="secret_test",
            feishu_verification_token="verify-test",
            feishu_use_ws=False,
            feishu_allow_writes=False,
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        verification = await client.post(
            "/api/v1/feishu/events",
            json={
                "type": "url_verification",
                "challenge": "safe-challenge",
                "token": "verify-test",
            },
        )
        event = await client.post(
            "/api/v1/feishu/events",
            json={
                "header": {"event_type": "im.message.receive_v1", "token": "verify-test"},
                "event": {},
            },
        )

    assert verification.status_code == 200
    assert verification.json() == {"challenge": "safe-challenge"}
    assert event.status_code == 503
    assert "write gate is closed" in event.json()["detail"]
    await db.close()


async def test_project_api_bearer_token_gate() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    app = create_app(db, settings=OrgPilotSettings(api_token="api-test-token"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/projects/project-1/events")
        authorized = await client.get(
            "/api/v1/projects/project-1/events",
            headers={"Authorization": "Bearer api-test-token"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    await db.close()


async def test_feishu_verification_token_gate() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    app = create_app(
        db,
        settings=OrgPilotSettings(feishu_verification_token="verify-test"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rejected = await client.post(
            "/api/v1/feishu/events",
            json={"type": "url_verification", "challenge": "challenge"},
        )
        accepted = await client.post(
            "/api/v1/feishu/events",
            json={
                "type": "url_verification",
                "challenge": "challenge",
                "token": "verify-test",
            },
        )

    assert rejected.status_code == 401
    assert accepted.json() == {"challenge": "challenge"}
    await db.close()


async def test_feishu_card_uses_persisted_approval_id() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    feishu_client = MockFeishuClient()
    service = GatewayService(
        db,
        adapter_factory=lambda project_id: FeishuCollaborationAdapter(
            client=feishu_client,
            project_id=project_id,
        ),
    )
    project_id = "feishu-card-id"
    await service.ingest_raw_events(project_id, _make_setup_events(project_id))

    await service.ingest_message(
        project_id=project_id,
        message="支付 SDK 报错，排查需要到明天下午 5 点",
        actor_id="alice",
        occurred_at=NOW,
        source_ref="om_test_1",
    )

    agent = await service.get_or_replay_agent(project_id)
    request = agent.approval_manager.get_pending_requests()[0]
    approve_button = feishu_client.sent_cards[0]["card"]["elements"][-1]["actions"][0]
    assert approve_button["value"]["approval_id"] == request.approval_id
    await db.close()


async def test_replay_discards_snapshot_with_unpersisted_event() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    service = GatewayService(db)
    project_id = "snapshot-authority"
    await service.ingest_raw_events(project_id, _make_setup_events(project_id))

    corrupted = await service.get_or_replay_agent(project_id)
    corrupted.projector.state.processed_event_ids.add("evt-never-persisted")
    corrupted.projector.state.tasks.clear()
    await service.state_store.save_state(corrupted.projector.state)

    restored = await service.get_or_replay_agent(project_id)

    assert "evt-never-persisted" not in restored.projector.state.processed_event_ids
    assert "backend_api" in restored.projector.state.tasks
    await db.close()

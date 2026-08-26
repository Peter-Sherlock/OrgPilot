"""Runtime configuration, provider wiring, and inbound authentication tests."""

import httpx
import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.extraction.client import AnthropicCompatibleLLMClient, MockLLMClient
from orgpilot.feishu.adapter import FeishuCollaborationAdapter
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.gateway.app import create_app
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database
from tests.test_gateway_api import NOW, _make_setup_events


def test_settings_from_env_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORGPILOT_DATABASE_URL", "postgresql+asyncpg://db.example/orgpilot")
    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "aihubmix")
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    monkeypatch.setenv("AIHUBMIX_MODEL", "gpt-5.6-luna")

    settings = OrgPilotSettings.from_env()

    assert settings.database_url == "postgresql+asyncpg://db.example/orgpilot"
    assert settings.llm_provider == "aihubmix"
    assert settings.aihubmix_model == "gpt-5.6-luna"
    assert "test-key" not in repr(settings)

    with pytest.raises(ValueError, match="ORGPILOT_LLM_PROVIDER"):
        OrgPilotSettings(llm_provider="unknown").validate()
    with pytest.raises(ValueError, match="AIHUBMIX_API_KEY"):
        OrgPilotSettings(llm_provider="aihubmix").validate()
    with pytest.raises(ValueError, match="Missing required Feishu settings"):
        OrgPilotSettings(collaboration_adapter="feishu").validate()


def test_aihubmix_client_is_only_enabled_explicitly() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    mock_app = create_app(db, settings=OrgPilotSettings())
    assert isinstance(mock_app.state.gateway_service.extractor.llm_client, MockLLMClient)

    live_app = create_app(
        db,
        settings=OrgPilotSettings(
            llm_provider="aihubmix",
            aihubmix_api_key="test-key",
        ),
    )
    live_client = live_app.state.gateway_service.extractor.llm_client
    assert isinstance(live_client, AnthropicCompatibleLLMClient)
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

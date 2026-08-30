"""Runtime resilience: LLM retries + circuit breaker + gateway degradation,
server-provided approval identity, and per-project ingest serialization."""

import asyncio
from datetime import datetime

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.extraction.client import (
    AnthropicCompatibleLLMClient,
    CircuitBreaker,
    LLMClient,
    LLMUnavailableError,
)
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import ExtractionResult, MessageContext
from orgpilot.gateway.app import create_app
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database

NOW = datetime.fromisoformat("2026-09-10T15:00:00+08:00")


def _llm_response() -> dict:
    body = ExtractionResult(is_actionable=False, intent="chit_chat")
    return {"content": [{"type": "text", "text": body.model_dump_json()}]}


def _client_with(handler) -> tuple[AnthropicCompatibleLLMClient, list[int]]:
    calls: list[int] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    client = AnthropicCompatibleLLMClient(
        api_key="k",
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(counting_handler)),
        max_retries=1,
        breaker_failure_threshold=2,
        breaker_cooldown_seconds=60.0,
    )
    return client, calls


def _context() -> MessageContext:
    return MessageContext(
        project_id="p",
        actor_id="carol",
        occurred_at=NOW,
        known_tasks={},
        known_members={},
    )


def test_transport_timeout_retries_then_raises() -> None:
    client, calls = _client_with(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("t")))
    with pytest.raises(LLMUnavailableError):
        client.extract("s", "u", "hello", _context())
    # 1 initial attempt + 1 retry, not an unbounded hang.
    assert len(calls) == 2


def test_transient_failure_recovers_on_retry() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json=_llm_response())

    client, calls = _client_with(handler)
    result = client.extract("s", "u", "hello", _context())
    assert result.intent is not None or result.is_actionable is False
    assert len(calls) == 2


def test_circuit_breaker_opens_and_fails_fast() -> None:
    client, calls = _client_with(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("t")))
    for _ in range(2):  # threshold=2
        with pytest.raises(LLMUnavailableError):
            client.extract("s", "u", "hello", _context())
    calls.clear()
    with pytest.raises(LLMUnavailableError, match="circuit breaker open"):
        client.extract("s", "u", "hello", _context())
    assert calls == []  # fail fast: no transport call while open


def test_circuit_breaker_half_open_recovers() -> None:
    now = {"t": 0.0}
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10.0, clock=lambda: now["t"])
    breaker.record_failure()
    assert breaker.allow() is False
    now["t"] = 11.0  # cooldown elapsed
    assert breaker.allow() is True
    breaker.record_success()
    assert breaker.allow() is True


class _DeadLLM(LLMClient):
    """Always-unavailable provider for degradation testing."""

    def extract(self, system_prompt, user_prompt, raw_message, context) -> ExtractionResult:
        raise LLMUnavailableError("provider down")


async def test_ingest_degrades_gracefully_when_llm_unavailable(tmp_path) -> None:
    """A provider outage must not 500 the whole request chain: the gateway
    answers with a degraded notice and persists nothing."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'degrade.db'}")
    await db.init_db()
    service = GatewayService(db, extractor=ClaimExtractor(llm_client=_DeadLLM()))
    project_id = "proj-degrade"
    await service.ingest_raw_events(
        project_id,
        [
            {
                "schema_version": 1,
                "project_id": project_id,
                "event_id": "evt-carol",
                "event_type": "member.registered",
                "source": "human",
                "source_ref": "setup",
                "occurred_at": NOW.isoformat(),
                "received_at": NOW.isoformat(),
                "payload": {"member_id": "carol", "display_name": "Carol", "role": "pm"},
            }
        ],
    )
    result = await service.ingest_message(
        project_id=project_id,
        message="支付SDK报错了，排查需要到明天下午5点",
        actor_id="carol",
        occurred_at=NOW,
    )
    assert result.directive_kind == "llm_unavailable"
    assert result.is_actionable is False
    assert result.events == []
    assert "暂时不可用" in (result.bot_reply or "")
    events = await service.event_store.get_events(project_id)
    assert all(e.event_type != "task.health_reported" for e in events)
    await db.close()


async def test_concurrent_ingest_serialized_by_project_lock(tmp_path) -> None:
    """Two simultaneous member messages must not interleave projections."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'lock.db'}")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-lock"
    await service.ingest_raw_events(
        project_id,
        [
            {
                "schema_version": 1,
                "project_id": project_id,
                "event_id": "evt-carol",
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
                "event_id": "evt-alice",
                "event_type": "member.registered",
                "source": "human",
                "source_ref": "setup",
                "occurred_at": NOW.isoformat(),
                "received_at": NOW.isoformat(),
                "payload": {"member_id": "alice", "display_name": "Alice", "role": "engineer"},
            },
        ],
    )
    results = await asyncio.gather(
        service.ingest_message(
            project_id=project_id,
            message="告诉alice，上午完成联调",
            actor_id="carol",
            occurred_at=NOW,
        ),
        service.ingest_message(
            project_id=project_id,
            message="支付SDK已完成，已交付",
            actor_id="alice",
            occurred_at=NOW,
        ),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == []
    agent = await service.get_or_replay_agent(project_id)
    assert len(agent.projector.state.directives) == 1
    await db.close()


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


async def test_context_endpoint_serves_real_operator_identity(
    client: httpx.AsyncClient,
) -> None:
    """P1 regression: the console operator must be the sandbox PM member id
    (ou_pm), not the hardcoded 'pm_web_operator' that always failed approval."""
    base = "/api/v1/projects/proj-identity"
    await client.post(f"{base}/bootstrap-sandbox")

    context = await client.get(f"{base}/context")
    assert context.status_code == 200
    data = context.json()
    assert data["operator_id"] == "ou_pm"


async def test_approval_decision_requires_matching_approver(
    client: httpx.AsyncClient,
) -> None:
    """Wrong identity stays forbidden; the real PM identity passes the gate."""
    base = "/api/v1/projects/proj-identity-decision"
    await client.post(
        f"{base}/events",
        json={
            "events": [
                {
                    "schema_version": 1,
                    "project_id": "proj-identity-decision",
                    "event_id": "evt-id-carol",
                    "event_type": "member.registered",
                    "source": "human",
                    "source_ref": "setup",
                    "occurred_at": NOW.isoformat(),
                    "received_at": NOW.isoformat(),
                    "payload": {"member_id": "carol", "display_name": "Carol", "role": "pm"},
                },
                {
                    "schema_version": 1,
                    "project_id": "proj-identity-decision",
                    "event_id": "evt-id-alice",
                    "event_type": "member.registered",
                    "source": "human",
                    "source_ref": "setup",
                    "occurred_at": NOW.isoformat(),
                    "received_at": NOW.isoformat(),
                    "payload": {"member_id": "alice", "display_name": "Alice", "role": "engineer"},
                },
                {
                    "schema_version": 1,
                    "project_id": "proj-identity-decision",
                    "event_id": "evt-id-task",
                    "event_type": "task.created",
                    "source": "task",
                    "source_ref": "setup",
                    "occurred_at": NOW.isoformat(),
                    "received_at": NOW.isoformat(),
                    "payload": {
                        "task_id": "backend_api",
                        "title": "Backend API",
                        "owner_id": "alice",
                        "deadline": "2026-09-10T18:00:00+08:00",
                    },
                },
            ]
        },
    )

    report = await client.post(
        f"{base}/messages",
        json={
            "message": "支付 SDK 报错，排查需要到明天下午 5 点",
            "actor_id": "alice",
            "occurred_at": NOW.isoformat(),
        },
    )
    assert report.status_code == 200, report.text
    approvals = (await client.get(f"{base}/approvals")).json()
    assert approvals, "expected a reschedule approval request"
    approval_id = approvals[0]["approval_id"]

    forbidden = await client.post(
        f"{base}/approvals/{approval_id}/decision",
        json={"decision": "approved", "approver_id": "someone_else"},
    )
    assert forbidden.status_code == 403

    approved = await client.post(
        f"{base}/approvals/{approval_id}/decision",
        json={"decision": "approved", "approver_id": "carol", "reason": "ok"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

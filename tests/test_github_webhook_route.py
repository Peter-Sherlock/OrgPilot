"""Webhook route acceptance (ADR-0015 C3): verify → persist → respond.

Same harness style as ``test_gateway_api.py``: the app runs over an
ASGITransport inside the test's event loop, with the database initialized
explicitly. The route exists only when sync is enabled; unsigned or
mis-signed requests are rejected before any persistence; bound-repository
deliveries are ingested and drained inline (no model work); the same
delivery id with a different payload answers ``conflict`` without touching
the original row.
"""

import hashlib
import hmac
import json
import pathlib
from datetime import datetime

import httpx

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import FrozenClock
from orgpilot.gateway.app import create_app
from orgpilot.knowledge.models import InboxStatus, IssueSelector, ProjectSourceBinding
from orgpilot.storage.database import Database
from orgpilot.storage.project_source_store import SqlProjectSourceStore

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "github_snapshot.json"
T0 = datetime.fromisoformat("2026-11-03T10:00:00+08:00")
SECRET = "whsec-test"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_settings(**overrides) -> OrgPilotSettings:
    fields: dict = {
        "github_sync_enabled": True,
        "github_webhook_secret": SECRET,
        "collaboration_adapter": "mock",
        "feishu_use_ws": False,
    }
    fields.update(overrides)
    return OrgPilotSettings(**fields)


async def make_harness(
    settings: OrgPilotSettings | None = None, seed_binding: bool = True
):
    runtime_settings = settings or make_settings()
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    app = create_app(db, settings=runtime_settings, clock=FrozenClock(T0))
    if seed_binding:
        sources = SqlProjectSourceStore(db, FrozenClock(T0))
        await sources.create_binding(
            ProjectSourceBinding(
                project_id="proj-a",
                repository_id="1001",
                binding_version=1,
                branch="main",
                path_allowlist=("docs", "README.md"),
                issue_selector=IssueSelector(milestone="m2", issue_ids=(4,)),
                explicit_pr_ids=(10,),
                maintainers=("m1",),
                sharing_grants=("alice",),
                authority_rules={},
                access_epoch=1,
                created_at=T0,
            )
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, app
    await db.close()


def webhook_request(client: httpx.AsyncClient, body: bytes, delivery_id: str, sig: str | None):
    headers = {"X-GitHub-Event": "push", "X-GitHub-Delivery": delivery_id}
    if sig is not None:
        headers["X-Hub-Signature-256"] = sig
    return client.post("/api/v1/github/webhook", content=body, headers=headers)


async def test_route_absent_when_sync_disabled() -> None:
    settings = make_settings(github_sync_enabled=False, github_webhook_secret=None)
    async for client, _app in make_harness(settings):
        response = await webhook_request(client, b"{}", "d-0", sign(b"{}"))
    assert response.status_code == 404


async def test_missing_and_bad_signatures_rejected() -> None:
    body = json.dumps({"repository": {"id": 1001}}).encode()
    async for client, _app in make_harness():
        missing = await webhook_request(client, body, "d-a", None)
        bad = await webhook_request(client, body, "d-b", "sha256=" + "0" * 64)
    assert missing.status_code == 401
    assert bad.status_code == 401


async def test_missing_delivery_id_rejected() -> None:
    body = json.dumps({"repository": {"id": 1001}}).encode()
    async for client, _app in make_harness():
        headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": sign(body)}
        response = await client.post("/api/v1/github/webhook", content=body, headers=headers)
    assert response.status_code == 400


async def test_invalid_json_rejected() -> None:
    async for client, _app in make_harness():
        response = await webhook_request(client, b"not-json{", "d-c", sign(b"not-json{"))
    assert response.status_code == 400


async def test_bound_repository_ingests_duplicates_and_conflicts() -> None:
    body = json.dumps({"repository": {"id": 1001}, "ref": "refs/heads/main"}).encode()
    async for client, app in make_harness():
        accepted = await webhook_request(client, body, "d-ok", sign(body))
        sync_service = app.state.github_sync
        assert sync_service is not None
        entry = await sync_service.inbox.get_entry("1001", "d-ok")
        # Ingest-only mode (no platform credentials configured): the delivery
        # is persisted but stays pending, never a fake refresh.
        assert entry is not None and entry.status is InboxStatus.PENDING

        again = await webhook_request(client, body, "d-ok", sign(body))
        other = json.dumps({"repository": {"id": 1001}, "ref": "refs/heads/dev"}).encode()
        conflict = await webhook_request(client, other, "d-ok", sign(other))
        stored = await sync_service.inbox.get_entry("1001", "d-ok")
    assert accepted.status_code == 200
    assert accepted.json()["msg"] == "accepted"
    assert again.json()["msg"] == "duplicate"
    assert conflict.status_code == 200
    assert conflict.json()["msg"] == "conflict"
    # The original delivery row survived the conflicting replay untouched.
    assert stored is not None and json.loads(json.dumps(stored.payload))["ref"] == "refs/heads/main"


async def test_unknown_repository_acknowledged_without_persisting() -> None:
    body = json.dumps({"repository": {"id": 9999}}).encode()
    async for client, app in make_harness():
        response = await webhook_request(client, body, "d-x", sign(body))
        sync_service = app.state.github_sync
        assert response.status_code == 200
        assert response.json()["msg"] == "unknown_repository"
        # Nothing persisted for an unbound repository (checked before teardown).
        assert await sync_service.inbox.get_entry("9999", "d-x") is None


async def test_end_to_end_refresh_via_webhook() -> None:
    """A valid push with changed content lands a new revision over HTTP."""
    snapshot = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    from orgpilot.github.client import MockGitHubClient

    async for client, app in make_harness():
        # Swap the sync service's client for the offline mock with our snapshot.
        mock = MockGitHubClient(snapshot)
        app.state.github_sync.client_factory = lambda binding, _mock=mock: _mock
        body = json.dumps({"repository": {"id": 1001}, "ref": "refs/heads/main"}).encode()
        first = await webhook_request(client, body, "d-1", sign(body))
        # Change document content, then deliver a new push with a new delivery id.
        snapshot["blobs"]["b-delivery"] += "\n联调前需登记环境（HTTP 验收）。\n"
        snapshot["branches"]["main"]["sha"] = "sha-http"
        snapshot["trees"]["sha-http"] = snapshot["trees"]["c0ffee1"]
        second = await webhook_request(client, body, "d-2", sign(body))
        sources = app.state.github_sync.sources
        from orgpilot.knowledge.models import SourceKind

        delivery = await sources.find_source_object(
            "proj-a", 1, SourceKind.DOCUMENT, "docs/delivery.md"
        )
        revisions = await sources.list_revisions(delivery.source_id)
    assert first.status_code == 200 and first.json()["msg"] == "accepted"
    assert second.status_code == 200
    assert len(revisions) == 2
    assert revisions[-1].source_version == "sha-http"
    assert "HTTP 验收" in (revisions[-1].raw_content or "")

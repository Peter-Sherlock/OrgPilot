"""R9: actual app lifespan, durable ACK, autonomous retries and restart drain."""

import asyncio
import json
import threading

import httpx

from orgpilot.config import OrgPilotSettings
from orgpilot.gateway.app import create_app
from orgpilot.github.client import GitHubReadError, MockGitHubClient
from orgpilot.github.sync import GitHubSyncService
from orgpilot.knowledge.models import InboxStatus
from orgpilot.storage.database import Database
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import (
    SqlProjectSourceStore,
    SqlSyncInboxStore,
    SqlSyncRunStore,
)
from tests.test_github_sync import load_snapshot, make_binding
from tests.test_github_webhook_route import sign


async def wait_done(sync, delivery_id):
    async def poll():
        while True:
            entry = await sync.inbox.get_entry("1001", delivery_id)
            generation_id = await sync.knowledge.current_generation_id("proj-a")
            if entry is not None and entry.status is InboxStatus.DONE and generation_id:
                generation = await sync.knowledge.get_generation(generation_id)
                if generation.valid:
                    return entry
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout=10)


async def seed_database(path):
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.init_db()
    await SqlProjectSourceStore(db).create_binding(make_binding())
    return db


def make_app(db, client):
    app = create_app(
        db=db,
        settings=OrgPilotSettings(
            github_sync_enabled=True,
            github_webhook_secret="whsec-test",
            feishu_use_ws=False,
        ),
    )
    app.state.github_sync.client_factory = lambda binding: client
    return app


async def test_r9_ack_does_not_wait_for_synchronous_platform_io(tmp_path):
    class Blocked(MockGitHubClient):
        entered = threading.Event()
        released = threading.Event()

        def get_branch_head(self, branch):
            self.entered.set()
            assert self.released.wait(timeout=10)
            return super().get_branch_head(branch)

    client = Blocked(load_snapshot())
    db = await seed_database(tmp_path / "ack.db")
    app = make_app(db, client)
    try:
        async with app.router.lifespan_context(app):
            try:
                assert await asyncio.to_thread(client.entered.wait, 5)
                body = json.dumps({"repository": {"id": 1001}, "ref": "refs/heads/main"}).encode()
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://offline"
                ) as http:
                    response = await asyncio.wait_for(
                        http.post(
                            "/api/v1/github/webhook",
                            content=body,
                            headers={
                                "X-GitHub-Event": "push",
                                "X-GitHub-Delivery": "r9-ack",
                                "X-Hub-Signature-256": sign(body),
                            },
                        ),
                        timeout=5,
                    )
                assert response.status_code == 200
                assert response.json()["msg"] == "accepted"
                assert not client.released.is_set()
                entry = await app.state.github_sync.inbox.get_entry("1001", "r9-ack")
                assert entry.status is InboxStatus.PENDING
            finally:
                client.released.set()
            await wait_done(app.state.github_sync, "r9-ack")
    finally:
        client.released.set()
    assert app.state.github_sync._worker is None


async def test_r9_lifespan_restart_consumes_processing_without_new_notification(tmp_path):
    path = tmp_path / "restart.db"
    db1 = await seed_database(path)
    inbox = SqlSyncInboxStore(db1)
    sync1 = GitHubSyncService(
        db1, SqlProjectSourceStore(db1), inbox, SqlSyncRunStore(db1), SqlKnowledgeStore(db1)
    )
    await sync1.ingest_webhook("1001", "r9-restart", "push", None, {"ref": "refs/heads/main"})
    await inbox.set_status("1001", "r9-restart", InboxStatus.PROCESSING)
    await db1.close()
    db2 = Database(f"sqlite+aiosqlite:///{path}")
    app = make_app(db2, MockGitHubClient(load_snapshot()))
    async with app.router.lifespan_context(app):
        entry = await wait_done(app.state.github_sync, "r9-restart")
        assert entry.attempts == 1
    assert app.state.github_sync._worker is None


async def test_r9_retry_wait_progresses_without_another_webhook(tmp_path):
    class FailOnce(MockGitHubClient):
        failures = 0

        def get_tree(self, sha):
            # Bootstrap succeeds; only the first inbox refresh fails.
            if getattr(self, "fail_next", False):
                self.fail_next = False
                self.failures += 1
                raise GitHubReadError("get_tree", 503, "offline transient")
            return super().get_tree(sha)

    client = FailOnce(load_snapshot())
    db = await seed_database(tmp_path / "retry.db")
    app = make_app(db, client)
    sync = app.state.github_sync
    sync.retry_backoff_seconds = 0.01
    await sync.manual_recheck("proj-a")
    client.fail_next = True
    async with app.router.lifespan_context(app):
        await sync.ingest_webhook("1001", "r9-retry", "push", None, {"ref": "refs/heads/main"})
        entry = await wait_done(sync, "r9-retry")
        assert entry.attempts == 2
        assert client.failures == 1
    assert sync._worker is None


async def test_r9_disabled_sync_creates_no_worker(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'disabled.db'}")
    app = create_app(db=db, settings=OrgPilotSettings(feishu_use_ws=False))
    async with app.router.lifespan_context(app):
        assert app.state.github_sync is None

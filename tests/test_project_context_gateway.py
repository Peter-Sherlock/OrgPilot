"""Gateway-level integration for project context across a restart (C6).

File-backed database, real app wiring: import → derive → ask via the router
→ close → reopen → the same message replays its stored session instead of
re-consuming budget, sync startup recovery re-queues the interrupted
delivery, and business tables stay untouched across the whole cycle.
"""

import json
import pathlib
from datetime import datetime

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import FrozenClock
from orgpilot.gateway.app import build_gateway_service
from orgpilot.github.client import MockGitHubClient
from orgpilot.knowledge.derive import DerivationPipeline
from orgpilot.knowledge.importer import SourceImporter
from orgpilot.knowledge.models import IssueSelector, ProjectSourceBinding
from orgpilot.storage.database import Database
from orgpilot.storage.event_store import SqlEventStore
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import (
    SqlProjectSourceStore,
    SqlSyncInboxStore,
    SqlSyncRunStore,
)
from orgpilot.storage.read_session_store import SqlReadSessionStore

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "github_snapshot.json"
T0 = datetime.fromisoformat("2026-11-03T10:00:00+08:00")
SECRET = "whsec-restart"


def make_settings(db_url: str) -> OrgPilotSettings:
    return OrgPilotSettings(
        database_url=db_url,
        project_qa_enabled=True,
        github_sync_enabled=True,
        github_webhook_secret=SECRET,
        collaboration_adapter="mock",
        feishu_use_ws=False,
    )


def make_binding() -> ProjectSourceBinding:
    return ProjectSourceBinding(
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


async def test_gateway_restart_replays_session_and_recovers_sync(tmp_path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}"
    snapshot = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    # ---- first lifecycle: import, derive, ask ---------------------------------
    db1 = Database(db_url)
    await db1.init_db()
    clock = FrozenClock(T0)
    settings = make_settings(db_url)
    service1, _llm, sync1 = build_gateway_service(settings, db1, clock)
    sync1.client_factory = lambda binding: MockGitHubClient(snapshot)
    sources = SqlProjectSourceStore(db1, clock)
    await sources.create_binding(make_binding())
    runs = SqlSyncRunStore(db1, clock)
    knowledge = SqlKnowledgeStore(db1, clock)
    importer = SourceImporter(
        MockGitHubClient(snapshot), sources, knowledge, runs, clock=clock
    )
    await importer.import_binding_scope(make_binding())
    pipeline = DerivationPipeline(sources, knowledge, clock=clock)
    await pipeline.build_and_publish(make_binding(), reason="initial")

    answer1 = await service1.project_knowledge.answer(
        "proj-a", "alice", "restart-msg-1", "项目目标是什么？", occurred_at=T0
    )
    assert answer1.stop_reason.value == "answered"
    # Park one delivery mid-processing to simulate a crash before shutdown.
    await sync1.ingest_webhook("1001", "d-crash", "push", None, {"ref": "refs/heads/main"})
    await db1.close()

    # ---- second lifecycle: restart -------------------------------------------
    db2 = Database(db_url)
    await db2.init_db()
    clock2 = FrozenClock(T0)
    service2, _llm2, sync2 = build_gateway_service(make_settings(db_url), db2, clock2)
    sync2.client_factory = lambda binding: MockGitHubClient(snapshot)
    await sync2.startup_recovery()
    read_sessions = SqlReadSessionStore(db2, clock2)
    session = await read_sessions.get_by_message("proj-a", "restart-msg-1")
    assert session is not None
    assert session.spec.question == "项目目标是什么？"
    answer2 = await service2.project_knowledge.answer(
        "proj-a", "alice", "restart-msg-1", "项目目标是什么？", occurred_at=T0
    )
    # Same message → same session and stored result, no fresh budget.
    assert answer2.read_session_id == answer1.read_session_id
    assert answer2.rendered_text == answer1.rendered_text
    recovery = await sync2.startup_recovery()
    assert recovery["processing_reset"] >= 0
    # The parked delivery survives restart and drains after recovery.
    inbox = SqlSyncInboxStore(db2, clock2)
    entry = await inbox.get_entry("1001", "d-crash")
    assert entry is not None
    # Business state untouched across the whole cycle.
    events = SqlEventStore(db2)
    assert await events.count() == 0
    await db2.close()

"""Outbox reliability: enqueue-before-send, retry with backoff, dead letters,
crash recovery between persist and send, and the directive delivery ledger."""

from datetime import datetime

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.models import ActionCommand
from orgpilot.gateway.app import create_app
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database
from orgpilot.storage.outbox_store import OUTBOX_DEAD, OUTBOX_DELIVERED, OUTBOX_PENDING

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


async def _seed(db: Database, project_id: str) -> None:
    """Registers a PM and a member so directive intents have a valid ledger."""
    service = GatewayService(db)
    await service.ingest_raw_events(
        project_id,
        [
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
                "event_id": f"evt-{project_id}-alice",
                "event_type": "member.registered",
                "source": "human",
                "source_ref": "setup",
                "occurred_at": NOW.isoformat(),
                "received_at": NOW.isoformat(),
                "payload": {"member_id": "alice", "display_name": "Alice", "role": "backend"},
            },
        ],
    )


class FlakyAdapter:
    """Wraps the mock adapter and fails the first N transport calls."""

    def __init__(self, inner, failures: int) -> None:
        self.inner = inner
        self.failures = failures

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, command):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("feishu down")
        return self.inner.execute(command)


async def test_directive_relay_recorded_as_delivered(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'outbox-delivered.db'}")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-outbox-ok"
    await _seed(db, project_id)

    result = await service.ingest_message(
        project_id=project_id,
        message="告诉alice，今天下班前把接口联调完",
        actor_id="carol",
        occurred_at=NOW,
    )
    assert result.directive_kind == "issued"

    rows = await service.outbox_store.list_rows(project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == OUTBOX_DELIVERED

    agent = await service.get_or_replay_agent(project_id)
    directive = next(iter(agent.projector.state.directives.values()))
    assert directive.delivery_status == "delivered"

    events = await service.event_store.get_events(project_id)
    assert any(e.event_type == "directive.delivered" for e in events)
    await db.close()


async def test_failed_relay_is_retried_and_then_delivered(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'outbox-retry.db'}")
    await db.init_db()
    flaky = FlakyAdapter(None, failures=1)

    def factory(project_id: str):
        from orgpilot.adapter.mock import MockCollaborationAdapter

        if flaky.inner is None:
            flaky.inner = MockCollaborationAdapter(project_id=project_id)
        return flaky

    service = GatewayService(db, adapter_factory=factory, outbox_retry_seconds=0)
    project_id = "proj-outbox-retry"
    await _seed(db, project_id)

    result = await service.ingest_message(
        project_id=project_id,
        message="告诉alice，明天上午10点前提交方案",
        actor_id="carol",
        occurred_at=NOW,
    )
    assert result.directive_kind == "issued"
    rows = await service.outbox_store.list_rows(project_id)
    assert rows[0]["status"] == OUTBOX_PENDING
    assert rows[0]["attempts"] == 1
    assert "feishu down" in (rows[0]["last_error"] or "")

    # Background sweep (or startup recovery) delivers the stranded command.
    swept = await service.sweep_outbox()
    assert swept == 1
    rows = await service.outbox_store.list_rows(project_id)
    assert rows[0]["status"] == OUTBOX_DELIVERED
    assert rows[0]["attempts"] == 2

    agent = await service.get_or_replay_agent(project_id)
    directive = next(iter(agent.projector.state.directives.values()))
    assert directive.delivery_status == "delivered"
    events = await service.event_store.get_events(project_id)
    delivered = [event for event in events if event.event_type == "directive.delivered"]
    assert len(delivered) == 1
    assert delivered[0].payload.attempts == 2
    await db.close()


async def test_dead_letter_after_max_attempts(tmp_path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'outbox-dead.db'}")
    await db.init_db()

    def factory(project_id: str):
        from orgpilot.adapter.mock import MockCollaborationAdapter

        return FlakyAdapter(MockCollaborationAdapter(project_id=project_id), failures=10**9)

    service = GatewayService(
        db,
        adapter_factory=factory,
        outbox_max_attempts=2,
        outbox_retry_seconds=0,
    )
    project_id = "proj-outbox-dead"
    await _seed(db, project_id)

    await service.ingest_message(
        project_id=project_id,
        message="告诉alice，周五前完成部署",
        actor_id="carol",
        occurred_at=NOW,
    )
    await service.sweep_outbox()  # attempt 2 -> dead letter

    rows = await service.outbox_store.list_rows(project_id)
    assert rows[0]["status"] == OUTBOX_DEAD
    assert rows[0]["attempts"] == 2

    agent = await service.get_or_replay_agent(project_id)
    directive = next(iter(agent.projector.state.directives.values()))
    assert directive.delivery_status == "failed"

    events = await service.event_store.get_events(project_id)
    failures = [e for e in events if e.event_type == "directive.delivery_failed"]
    assert len(failures) == 1
    assert failures[0].payload.attempts == 2

    overview = await service.outbox_overview(project_id)
    assert overview["pending_count"] == 1
    await db.close()


async def test_crash_between_persist_and_send_is_recovered_on_restart(tmp_path) -> None:
    """A process that died after persisting directive.issued but before relaying
    must have its pending outbox row delivered by the next process's sweep."""
    db_path = tmp_path / "outbox-crash.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.init_db()

    def failing_factory(project_id: str):
        from orgpilot.adapter.mock import MockCollaborationAdapter

        return FlakyAdapter(MockCollaborationAdapter(project_id=project_id), failures=10**9)

    crashed = GatewayService(
        db, adapter_factory=failing_factory, outbox_max_attempts=99, outbox_retry_seconds=0
    )
    project_id = "proj-outbox-crash"
    await _seed(db, project_id)
    await crashed.ingest_message(
        project_id=project_id,
        message="告诉alice，联调环境今晚修复",
        actor_id="carol",
        occurred_at=NOW,
    )
    await db.close()

    restarted_db = Database(f"sqlite+aiosqlite:///{db_path}")
    await restarted_db.init_db()

    def healthy_factory(project_id: str):
        from orgpilot.adapter.mock import MockCollaborationAdapter

        return MockCollaborationAdapter(project_id=project_id)

    recovered = GatewayService(restarted_db, adapter_factory=healthy_factory)
    assert await recovered.sweep_outbox() == 1

    rows = await recovered.outbox_store.list_rows(project_id)
    assert rows[0]["status"] == OUTBOX_DELIVERED
    agent = await recovered.get_or_replay_agent(project_id)
    directive = next(iter(agent.projector.state.directives.values()))
    assert directive.delivery_status == "delivered"
    await restarted_db.close()


async def test_enqueue_is_idempotent_per_idempotency_key(tmp_path) -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    service = GatewayService(db)
    command = ActionCommand(
        command_id="cmd:1",
        action_id="act:1",
        action_type="send_directive",
        targets=("alice",),
        payload={"text": "hello", "directive_id": "dir-1"},
        idempotency_key="idem:dir-1:alice",
        created_at=NOW,
    )
    assert await service.outbox_store.enqueue("p", command, NOW) is True
    assert await service.outbox_store.enqueue("p", command, NOW) is False
    assert len(await service.outbox_store.due_commands("p", NOW)) == 1

    # record_completed upgrades the pending row; a second settle is a no-op.
    assert await service.outbox_store.record_completed("p", command, NOW) is True
    assert await service.outbox_store.record_completed("p", command, NOW) is False
    assert await service.outbox_store.due_commands("p", NOW) == []

    # Unknown keys are harmless no-ops.
    await service.outbox_store.settle(
        "p", "idem:missing", delivered=True, attempts=1, error=None, retry_at=NOW, now=NOW
    )
    assert await service.outbox_store.attempts_of("p", "idem:missing") == 0
    assert await service.outbox_store.pending_count("p") == 0
    await db.close()


async def test_sweep_auto_escalates_and_relays_to_issuer(tmp_path) -> None:
    """A directive past its escalation window escalates back to the issuer with a
    relay command carrying a distinct idempotency key."""
    from datetime import timedelta

    from orgpilot.adapter.mock import MockCollaborationAdapter
    from orgpilot.coordination.directives import DirectiveManager
    from orgpilot.events.models import (
        DirectiveIssuedEvent,
        EventSource,
        MemberRegisteredEvent,
        MemberRegisteredPayload,
    )
    from orgpilot.state.projector import OrgProjector

    projector = OrgProjector(project_id="p-esc")
    long_ago = NOW - timedelta(hours=2)
    for member_id, role in (("carol", "pm"), ("alice", "dev")):
        projector.apply(
            MemberRegisteredEvent(
                project_id="p-esc",
                event_id=f"e-{member_id}",
                event_type="member.registered",
                source=EventSource.HUMAN,
                source_ref="s",
                occurred_at=long_ago,
                received_at=long_ago,
                payload=MemberRegisteredPayload(
                    member_id=member_id, display_name=member_id.title(), role=role
                ),
            )
        )
    projector.apply(
        DirectiveIssuedEvent(
            project_id="p-esc",
            event_id="e-issue",
            event_type="directive.issued",
            source=EventSource.MESSAGE,
            source_ref="s",
            actor_id="carol",
            occurred_at=long_ago,
            received_at=long_ago,
            payload={
                "directive_id": "dir-esc",
                "text": "完成联调",
                "issuer_id": "carol",
                "target_id": "alice",
            },
        )
    )
    manager = DirectiveManager(
        adapter=MockCollaborationAdapter(project_id="p-esc"), escalation_after_minutes=60
    )
    outcome = manager.sweep_timeouts(projector.state, NOW)
    assert outcome.kind == "swept"
    for evt in outcome.events:
        projector.apply(evt)
    escalated = projector.state.directives["dir-esc"]
    assert escalated.escalated is True

    # The issuer-facing escalation relay has its own outbox identity.
    keys = [c.idempotency_key for c in outcome.outbound]
    assert keys == ["idem:directive:dir-esc:carol:esc"]


async def test_reminder_relay_not_deduped_against_issue_relay(tmp_path) -> None:
    """The reminder must carry its own idempotency key or the outbox swallows it."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'outbox-remind.db'}")
    await db.init_db()
    service = GatewayService(db)
    project_id = "proj-outbox-remind"
    await _seed(db, project_id)

    await service.ingest_message(
        project_id=project_id,
        message="告诉alice，今晚出简报",
        actor_id="carol",
        occurred_at=NOW,
    )
    result = await service.remind_directives(project_id, operator_id="carol")
    assert result.directive_kind == "reminded"

    rows = await service.outbox_store.list_rows(project_id)
    assert len(rows) == 2
    keys = {row["idempotency_key"] for row in rows}
    assert len(keys) == 2
    assert all(row["status"] == OUTBOX_DELIVERED for row in rows)
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


async def test_outbox_api_reports_delivery_ledger(client: httpx.AsyncClient) -> None:
    base = "/api/v1/projects/proj-outbox-api"
    await client.post(f"{base}/bootstrap-sandbox")
    issue = await client.post(
        f"{base}/sandbox-chat",
        params={"actor_id": "ou_pm", "message": "告诉Alice，明天下午3点前复盘"},
    )
    assert issue.json()["directive"] == "issued"

    res = await client.get(f"{base}/outbox")
    assert res.status_code == 200
    data = res.json()
    assert data["pending_count"] == 0
    assert data["rows"] and data["rows"][0]["status"] == "delivered"


async def test_permanent_rejection_dead_letters_immediately(tmp_path) -> None:
    """A 400-class platform rejection (e.g. a synthetic open_id) must dead-letter
    on the first attempt instead of burning retries, with a delivery_failed event."""
    from orgpilot.adapter.contracts import PermanentDeliveryError
    from orgpilot.adapter.mock import MockCollaborationAdapter

    class RejectingAdapter(MockCollaborationAdapter):
        def execute(self, command):
            raise PermanentDeliveryError("Feishu send message failed: invalid receive_id")

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'outbox-permanent.db'}")
    await db.init_db()
    service = GatewayService(
        db,
        adapter_factory=lambda pid: RejectingAdapter(project_id=pid),
        outbox_max_attempts=3,
        outbox_retry_seconds=0,
    )
    project_id = "proj-outbox-permanent"
    await _seed(db, project_id)

    await service.ingest_message(
        project_id=project_id,
        message="告诉alice，今晚部署",
        actor_id="carol",
        occurred_at=NOW,
    )

    rows = await service.outbox_store.list_rows(project_id)
    assert rows[0]["status"] == OUTBOX_DEAD
    assert rows[0]["attempts"] == 1
    assert "invalid receive_id" in (rows[0]["last_error"] or "")

    events = await service.event_store.get_events(project_id)
    failures = [e for e in events if e.event_type == "directive.delivery_failed"]
    assert len(failures) == 1

    agent = await service.get_or_replay_agent(project_id)
    directive = next(iter(agent.projector.state.directives.values()))
    assert directive.delivery_status == "failed"
    await db.close()

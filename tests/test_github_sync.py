"""Sync-service acceptance: inbox, incremental refresh, rechecks, recovery (C3).

Frozen behaviors: verify-before-persist, delivery idempotency and payload
conflicts, stale notifications never rolling material back, sub-object edits
refreshing independently of the parent, atomic refresh+checkpoint+done
commits that survive mid-transaction crashes, bounded retry/backoff with
parked entries, deletion only from complete enumerations, default-off
periodic recheck, and zero business-state writes.
"""

import json
import pathlib
from datetime import timedelta

import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import FrozenClock
from orgpilot.github.client import MockGitHubClient
from orgpilot.github.sync import GitHubSyncService, verify_signature
from orgpilot.knowledge.importer import ImportLimits
from orgpilot.knowledge.models import (
    InboxStatus,
    IssueSelector,
    ProjectSourceBinding,
    SourceAvailability,
    SourceKind,
)
from orgpilot.storage.database import Database
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import (
    SqlProjectSourceStore,
    SqlSyncInboxStore,
    SqlSyncRunStore,
)

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "github_snapshot.json"
T0 = datetime_from_iso = __import__("datetime").datetime.fromisoformat(
    "2026-11-03T10:00:00+08:00"
)


def load_snapshot() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_binding(**overrides) -> ProjectSourceBinding:
    fields: dict = {
        "project_id": "proj-a",
        "repository_id": "1001",
        "binding_version": 1,
        "branch": "main",
        "path_allowlist": ("docs", "README.md"),
        "issue_selector": IssueSelector(milestone="m2", issue_ids=(4,)),
        "explicit_pr_ids": (10,),
        "maintainers": ("maintainer-1",),
        "sharing_grants": ("alice",),
        "authority_rules": {"delivery": "docs/delivery.md"},
        "access_epoch": 1,
        "created_at": T0,
    }
    fields.update(overrides)
    return ProjectSourceBinding(**fields)


@pytest.fixture
async def db() -> Database:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init_db()
    yield database
    await database.close()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
async def setup(db: Database, clock: FrozenClock):
    """A sync service with a bound project and one mutable mock client."""
    snapshot = load_snapshot()
    client = MockGitHubClient(snapshot)
    sources = SqlProjectSourceStore(db, clock)
    service = GitHubSyncService(
        db=db,
        sources=sources,
        inbox_store=SqlSyncInboxStore(db, clock),
        runs=SqlSyncRunStore(db, clock),
        knowledge=SqlKnowledgeStore(db, clock),
        clock=clock,
        client_factory=lambda binding: client,
        import_limits=ImportLimits(),
    )
    await sources.create_binding(make_binding())
    return service, sources, client, snapshot


async def ingest_and_drain(service: GitHubSyncService, delivery_id: str, event: str, payload: dict):
    outcome = await service.ingest_webhook(
        "1001", delivery_id, event, payload.get("action"), payload
    )
    counts = await service.drain_repository("1001")
    return outcome, counts


class TestSignatureVerification:
    def test_valid_signature_accepted(self) -> None:
        import hashlib
        import hmac as hmac_mod

        body = b'{"zen": "ok"}'
        sig = "sha256=" + hmac_mod.new(b"secret", body, hashlib.sha256).hexdigest()
        assert verify_signature("secret", body, sig) is True

    def test_tampered_body_rejected(self) -> None:
        import hashlib
        import hmac as hmac_mod

        sig = "sha256=" + hmac_mod.new(b"secret", b"original", hashlib.sha256).hexdigest()
        assert verify_signature("secret", b"tampered", sig) is False

    def test_missing_secret_or_header_rejected(self) -> None:
        assert verify_signature("", b"{}", "sha256=abc") is False
        assert verify_signature("secret", b"{}", None) is False
        assert verify_signature("secret", b"{}", "md5=abc") is False


class TestIngestAndDrain:
    async def test_push_refreshes_documents_atomically(self, setup) -> None:
        service, sources, client, snapshot = setup
        outcome, counts = await ingest_and_drain(
            service, "d-1", "push", {"ref": "refs/heads/main"}
        )
        assert outcome == "accepted"
        assert counts["processed"] == 1
        assert counts["refreshed"] == 1  # first push materializes the docs
        entry = await service.inbox.get_entry("1001", "d-1")
        assert entry is not None and entry.status is InboxStatus.DONE
        delivery = await sources.find_source_object(
            "proj-a", 1, SourceKind.DOCUMENT, "docs/delivery.md"
        )
        assert delivery is not None
        # An identical push queues no new revision (content dedup).
        await ingest_and_drain(service, "d-1b", "push", {"ref": "refs/heads/main"})
        assert len(await sources.list_revisions(delivery.source_id)) == 1
        # Now change the pinned content and re-push: a new revision appears.
        snapshot["blobs"]["b-delivery"] += "\n补充：联调前需登记环境。\n"
        snapshot["branches"]["main"]["sha"] = "sha-new"
        snapshot["trees"]["sha-new"] = snapshot["trees"]["c0ffee1"]
        await ingest_and_drain(service, "d-2", "push", {"ref": "refs/heads/main"})
        revisions = await sources.list_revisions(delivery.source_id)
        assert len(revisions) == 2
        assert "补充：联调前需登记环境" in (revisions[-1].raw_content or "")
        assert revisions[-1].source_version == "sha-new"

    async def test_delivery_redelivery_100_times_is_one_row(self, setup) -> None:
        service, sources, _, _ = setup
        payload = {"ref": "refs/heads/main"}
        for _ in range(100):
            outcome = await service.ingest_webhook("1001", "d-x", "push", None, payload)
            assert outcome in ("accepted", "duplicate")
        entries_due = await service.inbox.list_due("1001")
        assert len(entries_due) == 1
        counts = await service.drain_repository("1001")
        assert counts["processed"] == 1

    async def test_out_of_order_stale_issue_never_rolls_back(self, setup) -> None:
        service, sources, _, snapshot = setup
        # First: a fresh notification with a newer issue payload.
        newer = load_snapshot()
        newer["issues"][0]["assignees"] = ["carol"]
        client = service.client_factory(make_binding())
        client._snapshot = newer
        await ingest_and_drain(
            service, "d-new", "issues", {"action": "edited", "issue": {"number": 1}}
        )
        issue = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        assert issue is not None
        assert issue.metadata["assignees"] == ["carol"]
        latest_after_new = await sources.latest_revision(issue.source_id)
        # Now: an old notification (older updated_at) replays.
        await ingest_and_drain(
            service, "d-old", "issues", {"action": "edited", "issue": {"number": 1}}
        )
        latest_after_old = await sources.latest_revision(issue.source_id)
        counts_check = await service.inbox.get_entry("1001", "d-old")
        assert counts_check is not None and counts_check.status is InboxStatus.DONE
        assert latest_after_new.change_sequence == latest_after_old.change_sequence
        assert latest_after_old.metadata == latest_after_new.metadata

    async def test_comment_edit_refreshes_with_parent_untouched(self, setup) -> None:
        service, sources, client, _ = setup
        client._snapshot = load_snapshot()
        await ingest_and_drain(
            service, "d-seed", "issues", {"action": "opened", "issue": {"number": 1}}
        )
        parent = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        parent_revs_before = len(await sources.list_revisions(parent.source_id))
        # Parent issue object time does not change; only a comment edit arrives.
        client._snapshot["issue_comments"][0]["body"] = "明天给（已更新）"
        client._snapshot["issue_comments"][0]["updated_at"] = "2026-10-20T12:00:00+08:00"
        outcome, counts = await ingest_and_drain(
            service, "d-comment", "issue_comment", {"action": "edited", "issue": {"number": 1}}
        )
        assert counts["refreshed"] == 1
        parent = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        comment = await sources.find_source_object(
            "proj-a", 1, SourceKind.COMMENT, "7001", parent_source_id=parent.source_id
        )
        assert comment is not None
        revisions = await sources.list_revisions(comment.source_id)
        assert len(revisions) == 1
        assert "已更新" in revisions[0].metadata["body"]
        parent_revs = await sources.list_revisions(parent.source_id)
        assert len(parent_revs) == parent_revs_before  # parent untouched

    async def test_unknown_event_is_done_ignored(self, setup) -> None:
        service, _, _, _ = setup
        await ingest_and_drain(service, "d-u1", "deployments", {"action": "created"})
        entry = await service.inbox.get_entry("1001", "d-u1")
        assert entry is not None and entry.status is InboxStatus.DONE

    async def test_push_to_unbound_branch_ignored(self, setup) -> None:
        service, _, _, _ = setup
        _, counts = await ingest_and_drain(
            service, "d-branch", "push", {"ref": "refs/heads/feature"}
        )
        assert counts["ignored"] == 1

    async def test_out_of_scope_issue_webhook_ignored(self, setup) -> None:
        service, sources, _, _ = setup
        _, counts = await ingest_and_drain(
            service, "d-oos", "issues", {"action": "opened", "issue": {"number": 3}}
        )
        assert counts["ignored"] == 1
        assert await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "3") is None

    async def test_403_drops_availability_never_deletes(self, setup) -> None:
        service, sources, client, _ = setup
        client._snapshot = load_snapshot()
        await ingest_and_drain(
            service, "d-seed", "issues", {"action": "opened", "issue": {"number": 1}}
        )
        client._snapshot["failures"] = [
            {"method": "get_issue", "match": {"number": 1}, "status": 403}
        ]
        _, counts = await ingest_and_drain(
            service, "d-403", "issues", {"action": "edited", "issue": {"number": 1}}
        )
        assert counts["ignored"] == 1
        issue = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        assert issue is not None
        assert issue.availability is SourceAvailability.UNAVAILABLE
        assert issue.availability is not SourceAvailability.DELETED

    async def test_document_deleted_only_via_complete_tree(self, setup) -> None:
        service, sources, client, snapshot = setup
        client._snapshot = snapshot
        await ingest_and_drain(service, "d-seed", "push", {"ref": "refs/heads/main"})
        api = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "docs/spec/api.md")
        assert api is not None
        # A new push whose tree no longer contains api.md → confirmed deletion.
        snapshot["trees"]["c0ffee1"] = [
            entry for entry in snapshot["trees"]["c0ffee1"] if entry["path"] != "docs/spec/api.md"
        ]
        await ingest_and_drain(service, "d-del", "push", {"ref": "refs/heads/main"})
        api = await sources.get_source_object(api.source_id)
        assert api is not None
        assert api.availability is SourceAvailability.DELETED


class TestRetryAndRecovery:
    async def test_transient_failure_parks_entry_with_backoff(self, setup, clock) -> None:
        service, _, client, _ = setup
        client._snapshot = load_snapshot()
        client._snapshot["failures"] = [
            {"method": "get_issue", "match": {"number": 1}, "status": 500}
        ]
        await service.ingest_webhook("1001", "d-f1", "issues", None, {"issue": {"number": 1}})
        counts = await service.drain_repository("1001")
        assert counts["retry_wait"] == 1
        entry = await service.inbox.get_entry("1001", "d-f1")
        assert entry is not None
        assert entry.status is InboxStatus.RETRY_WAIT
        assert entry.attempts == 1
        assert entry.retry_at is not None
        # Not due before retry_at.
        clock.set(entry.retry_at - timedelta(seconds=1))
        assert await service.inbox.list_due("1001") == []
        # Due after retry_at: recover the client and drain again.
        client._snapshot["failures"] = []
        clock.set(entry.retry_at + timedelta(seconds=1))
        counts = await service.drain_repository("1001")
        assert counts["processed"] == 1
        entry = await service.inbox.get_entry("1001", "d-f1")
        assert entry is not None and entry.status is InboxStatus.DONE

    async def test_attempts_exhausted_marks_failed_and_stops(self, setup, clock) -> None:
        service, _, client, _ = setup
        snapshot = load_snapshot()
        snapshot["failures"] = [{"method": "get_issue", "match": {"number": 1}, "status": 503}]
        client._snapshot = snapshot
        await service.ingest_webhook("1001", "d-f2", "issues", None, {"issue": {"number": 1}})
        for _ in range(5):
            counts = await service.drain_repository("1001")
            entry = await service.inbox.get_entry("1001", "d-f2")
            if entry.status is InboxStatus.FAILED:
                break
            assert counts["retry_wait"] == 1
            clock.advance((entry.retry_at - clock.now()) + timedelta(seconds=1))
        entry = await service.inbox.get_entry("1001", "d-f2")
        assert entry is not None and entry.status is InboxStatus.FAILED
        assert entry.last_error is not None
        # Failed entries never loop in the worker.
        assert await service.inbox.list_due("1001") == []

    async def test_crash_mid_transaction_leaves_no_half_write(self, setup, db) -> None:
        service, sources, client, _ = setup
        client._snapshot = load_snapshot()
        client._snapshot["issues"][0]["assignees"] = ["carol"]
        await service.ingest_webhook("1001", "d-crash", "issues", None, {"issue": {"number": 1}})
        # Break the final inbox completion so the shared transaction rolls back.
        original = service.inbox.set_status

        async def broken_set_status(*args, **kwargs):
            if kwargs.get("status") is InboxStatus.DONE or (
                len(args) > 2 and args[2] is InboxStatus.DONE
            ):
                raise RuntimeError("simulated crash before commit")
            return await original(*args, **kwargs)

        service.inbox.set_status = broken_set_status
        with pytest.raises(RuntimeError):
            await service.drain_repository("1001")
        service.inbox.set_status = original
        # Nothing half-written: no assignee revision persisted.
        issue = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        if issue is not None:
            revisions = await sources.list_revisions(issue.source_id)
            assert all(rev.metadata.get("assignees") != ["carol"] for rev in revisions)
        # The claim survived as processing; startup recovery re-queues it.
        entry = await service.inbox.get_entry("1001", "d-crash")
        assert entry is not None and entry.status is InboxStatus.PROCESSING
        recovery = await service.startup_recovery()
        assert recovery["processing_reset"] == 1
        entry = await service.inbox.get_entry("1001", "d-crash")
        assert entry.status is InboxStatus.PENDING
        counts = await service.drain_repository("1001")
        assert counts["processed"] == 1

    async def test_startup_recovery_abandons_dead_runs(self, setup) -> None:
        service, _, _, _ = setup
        from orgpilot.knowledge.models import SyncRunReason

        await service.runs.start_run(
            "run-dead", "proj-a", 1, 1, SyncRunReason.WEBHOOK
        )
        recovery = await service.startup_recovery()
        assert recovery["runs_abandoned"] == 1
        run = await service.runs.get_run("run-dead")
        assert run.state.value == "abandoned"


class TestRecheck:
    async def test_manual_recheck_catches_missed_changes(self, setup) -> None:
        from orgpilot.knowledge.importer import SourceImporter

        service, sources, client, snapshot = setup
        snapshot = load_snapshot()
        client._snapshot = snapshot
        importer = SourceImporter(
            client=client, sources=sources, knowledge=service.knowledge,
            runs=service.runs, clock=service.clock,
        )
        await importer.import_binding_scope(make_binding())
        delivery = await sources.find_source_object(
            "proj-a", 1, SourceKind.DOCUMENT, "docs/delivery.md"
        )
        before = len(await sources.list_revisions(delivery.source_id))
        # Content changes with NO webhook delivered (lost notification).
        snapshot["blobs"]["b-delivery"] += "\n新增交付条件 v2。\n"
        snapshot["branches"]["main"]["sha"] = "sha-v2"
        snapshot["trees"]["sha-v2"] = snapshot["trees"]["c0ffee1"]
        report = await service.manual_recheck("proj-a")
        assert report is not None and report.fully_complete
        after = await sources.list_revisions(delivery.source_id)
        assert len(after) == before + 1
        assert "新增交付条件 v2" in (after[-1].raw_content or "")

    async def test_recheck_takes_down_only_when_enumeration_complete(self, setup) -> None:
        from orgpilot.knowledge.importer import SourceImporter

        service, sources, client, snapshot = setup
        snapshot = load_snapshot()
        client._snapshot = snapshot
        importer = SourceImporter(
            client=client, sources=sources, knowledge=service.knowledge,
            runs=service.runs, clock=service.clock,
        )
        await importer.import_binding_scope(make_binding())
        # Object removed from snapshot → complete enumeration → deleted.
        snapshot["trees"]["c0ffee1"] = [
            e for e in snapshot["trees"]["c0ffee1"] if e["path"] != "docs/spec/api.md"
        ]
        report = await service.manual_recheck("proj-a")
        assert report.fully_complete
        api = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "docs/spec/api.md")
        assert api.availability is SourceAvailability.DELETED
        # Now with a failure injected: missing objects must NOT be deleted.
        snapshot["trees"]["c0ffee1"].append(
            {"path": "docs/extra.md", "blob_sha": "b-extra", "size": 10}
        )
        snapshot["blobs"]["b-extra"] = "extra"
        snapshot["failures"] = [
            {"method": "get_blob", "match": {"blob_sha": "b-extra"}, "status": 500}
        ]
        before_docs = {
            doc.source_id: doc.availability
            for doc in await sources.list_source_objects("proj-a", 1, kind=SourceKind.DOCUMENT)
        }
        report2 = await service.manual_recheck("proj-a")
        assert not report2.fully_complete
        after_docs = {
            doc.source_id: doc.availability
            for doc in await sources.list_source_objects("proj-a", 1, kind=SourceKind.DOCUMENT)
        }
        assert before_docs == after_docs  # partial enumeration → no takedown

    async def test_periodic_recheck_runs_only_when_due(self, setup, clock) -> None:
        from orgpilot.knowledge.importer import SourceImporter

        service, sources, client, snapshot = setup
        service.recheck_interval_seconds = 1800
        snapshot = load_snapshot()
        client._snapshot = snapshot
        importer = SourceImporter(
            client=client, sources=sources, knowledge=service.knowledge,
            runs=service.runs, clock=service.clock,
        )
        await importer.import_binding_scope(make_binding())
        # Not due yet.
        clock.advance(timedelta(seconds=1799))
        assert await service.periodic_recheck_if_due("proj-a") is False
        clock.advance(timedelta(seconds=2))
        snapshot["blobs"]["b-api"] += "\n补充接口约定。\n"
        snapshot["branches"]["main"]["sha"] = "sha-p"
        snapshot["trees"]["sha-p"] = snapshot["trees"]["c0ffee1"]
        assert await service.periodic_recheck_if_due("proj-a") is True
        api = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "docs/spec/api.md")
        revisions = await sources.list_revisions(api.source_id)
        assert "补充接口约定" in (revisions[-1].raw_content or "")


class TestNoBusinessWrites:
    async def test_sync_leaves_events_cases_approvals_untouched(self, setup, db) -> None:
        from orgpilot.storage.event_store import SqlEventStore
        from orgpilot.storage.state_store import SqlStateStore

        service, _, client, _ = setup
        client._snapshot = load_snapshot()
        events = SqlEventStore(db)
        state_store = SqlStateStore(db)
        event = __import__("tests.conftest", fromlist=["make_event"]).make_event(
            "evt-member-1",
            "member.registered",
            {"member_id": "alice", "display_name": "Alice", "role": "pm"},
            project_id="proj-a",
            source="human",
        )
        await events.append(event)
        assert await state_store.load_state("proj-a") is None
        await ingest_and_drain(service, "d-b1", "push", {"ref": "refs/heads/main"})
        await ingest_and_drain(
            service, "d-b2", "issues", {"action": "edited", "issue": {"number": 1}}
        )
        # Sync touched none of the business tables.
        assert await events.count() == 1
        assert await state_store.load_cases("proj-a") == []
        assert await state_store.load_approvals("proj-a") == []
        assert await state_store.load_state("proj-a") is None


class TestIngestOnlyMode:
    async def test_no_client_keeps_entries_pending(self, db, clock) -> None:
        service = GitHubSyncService(
            db=db,
            sources=SqlProjectSourceStore(db, clock),
            inbox_store=SqlSyncInboxStore(db, clock),
            runs=SqlSyncRunStore(db, clock),
            knowledge=SqlKnowledgeStore(db, clock),
            clock=clock,
            client_factory=None,
        )
        await service.sources.create_binding(make_binding())
        outcome = await service.ingest_webhook(
            "1001", "d-io", "push", None, {"ref": "refs/heads/main"}
        )
        assert outcome == "accepted"
        counts = await service.drain_repository("1001")
        assert counts["pending_no_client"] == 1
        entry = await service.inbox.get_entry("1001", "d-io")
        assert entry.status is InboxStatus.PENDING
        assert await service.manual_recheck("proj-a") is None


class TestConfig:
    def test_sync_disabled_by_default(self) -> None:
        settings = OrgPilotSettings()
        assert settings.github_sync_enabled is False
        assert settings.github_recheck_enabled is False
        assert settings.github_recheck_interval_seconds == 1800

    def test_enabled_requires_webhook_secret(self) -> None:
        with pytest.raises(ValueError, match="WEBHOOK_SECRET"):
            OrgPilotSettings(github_sync_enabled=True).validate()

    def test_rejects_malformed_repo_name(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            OrgPilotSettings(
                github_sync_enabled=True,
                github_webhook_secret="s",
                github_repo_full_name="justaname",
            ).validate()

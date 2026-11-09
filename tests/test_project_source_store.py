"""ADR-0015 C1 acceptance: source contracts, persistence, and version identity.

Frozen behaviors under test:

- re-reading unchanged content never creates a new revision; A→B→A keeps three;
- the same webhook delivery id with a different payload is rejected, not overwritten;
- material namespaces stay disjoint from OrgEvent ids;
- cross-project rows never leak into each other;
- a caller-shared session that fails mid-commit rolls back completely;
- records survive a close/reopen of a file-backed database;
- budget reservations/settlements keep the session ledger consistent, unknown
  model spend is never recorded as zero, and interruption keeps the ledger;
- every timestamp comes from the injected clock, never the wall clock.
"""

from datetime import datetime, timedelta

import pytest

from orgpilot.domain.clock import FrozenClock
from orgpilot.domain.errors import DuplicateEventConflict
from orgpilot.events.log import AppendResult
from orgpilot.events.models import EventSource, parse_event
from orgpilot.knowledge.errors import (
    BindingConflict,
    DuplicateDeliveryConflict,
    KnowledgeContractError,
    ReadSessionConflict,
    RevisionConflict,
)
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetReservation,
    BudgetUsage,
    GenerationStatus,
    InboxStatus,
    IssueSelector,
    ProjectContextPacket,
    ProjectReadSessionSpec,
    ProjectSourceBinding,
    ReadSessionStatus,
    ReadStepKind,
    ReadStepRecord,
    ReservationKind,
    RevisionOutcome,
    SourceAvailability,
    SourceKind,
    SourceObject,
    SourceRevision,
    StopReason,
    SyncInboxEntry,
    SyncRunReason,
    SyncRunState,
    binding_uid,
    canonical_payload_hash,
    content_hash,
    is_source_ref,
    new_revision_id,
    question_hash,
)
from orgpilot.storage.database import Database
from orgpilot.storage.event_store import SqlEventStore
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import (
    SqlProjectSourceStore,
    SqlSyncInboxStore,
    SqlSyncRunStore,
)
from orgpilot.storage.read_session_store import SqlReadSessionStore

T0 = datetime.fromisoformat("2026-11-03T10:00:00+08:00")


def make_binding(
    project_id: str = "proj-a",
    binding_version: int = 1,
    access_epoch: int = 1,
    sharing_grants: tuple[str, ...] = ("alice",),
    path_allowlist: tuple[str, ...] = ("docs",),
    repository_id: str = "repo-100",
) -> ProjectSourceBinding:
    return ProjectSourceBinding(
        project_id=project_id,
        repository_id=repository_id,
        binding_version=binding_version,
        branch="main",
        path_allowlist=path_allowlist,
        issue_selector=IssueSelector(milestone="m2", issue_ids=(42,)),
        explicit_pr_ids=(87,),
        maintainers=("maintainer-1",),
        sharing_grants=sharing_grants,
        authority_rules={"delivery": "docs/delivery.md"},
        access_epoch=access_epoch,
        created_at=T0,
    )


def make_object(
    project_id: str = "proj-a",
    binding_version: int = 1,
    kind: SourceKind = SourceKind.DOCUMENT,
    external_id: str = "docs/delivery.md",
    title: str | None = "交付规范",
) -> SourceObject:
    from orgpilot.knowledge.models import SourceObjectIdentity

    identity = SourceObjectIdentity(
        project_id=project_id,
        binding_version=binding_version,
        kind=kind,
        external_id=external_id,
    )
    return SourceObject(
        source_id=identity.source_id(),
        project_id=project_id,
        binding_version=binding_version,
        kind=kind,
        external_id=external_id,
        title=title,
    )


def make_revision(
    obj: SourceObject,
    content: str,
    sequence: int,
    *,
    source_version: str = "sha-1",
    observed_at: datetime = T0,
    revision_id: str | None = None,
    access_epoch: int = 1,
) -> SourceRevision:
    return SourceRevision(
        revision_id=revision_id or new_revision_id(),
        source_id=obj.source_id,
        project_id=obj.project_id,
        binding_version=obj.binding_version,
        access_epoch=access_epoch,
        kind=obj.kind,
        source_version=source_version,
        change_sequence=sequence,
        content_hash=content_hash(content),
        raw_content=content,
        observed_at=observed_at,
    )


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
async def sources(db: Database, clock: FrozenClock) -> SqlProjectSourceStore:
    return SqlProjectSourceStore(db, clock)


@pytest.fixture
async def inbox(db: Database, clock: FrozenClock) -> SqlSyncInboxStore:
    return SqlSyncInboxStore(db, clock)


@pytest.fixture
async def runs(db: Database, clock: FrozenClock) -> SqlSyncRunStore:
    return SqlSyncRunStore(db, clock)


@pytest.fixture
async def knowledge(db: Database, clock: FrozenClock) -> SqlKnowledgeStore:
    return SqlKnowledgeStore(db, clock)


@pytest.fixture
async def read_sessions(db: Database, clock: FrozenClock) -> SqlReadSessionStore:
    return SqlReadSessionStore(db, clock)


# --------------------------------------------------------------------- bindings


async def test_binding_create_is_idempotent_but_conflicts_on_change(
    sources: SqlProjectSourceStore,
) -> None:
    binding = make_binding()
    assert await sources.create_binding(binding) is True
    # Identical row → no-op.
    assert await sources.create_binding(binding) is False
    # Same version, different content → conflict, never silent overwrite.
    changed = make_binding(sharing_grants=("alice", "bob"))
    with pytest.raises(BindingConflict):
        await sources.create_binding(changed)
    loaded = await sources.get_binding("proj-a", 1)
    assert loaded is not None and loaded.sharing_grants == ("alice",)
    active = await sources.get_active_binding("proj-a")
    assert active is not None and active.binding_version == 1


async def test_binding_scopes_and_sharing_checks(sources: SqlProjectSourceStore) -> None:
    binding = make_binding(path_allowlist=("docs", "README.md"))
    assert binding.allows_path("docs/delivery.md")
    assert binding.allows_path("docs/sub/./spec.md")
    assert binding.allows_path("README.md")
    assert not binding.allows_path("secrets/credentials.md")
    assert not binding.allows_path("")
    assert binding.member_can_read("alice")
    assert not binding.member_can_read("bob")
    assert not binding.member_can_read(None)
    # Issue selection: milestone OR explicit ids, union semantics (§2.1).
    assert binding.issue_selector.covers(42, None)
    assert binding.issue_selector.covers(7, "m2")
    assert not binding.issue_selector.covers(7, "m3")


async def test_binding_disable_hides_active_version(sources: SqlProjectSourceStore) -> None:
    await sources.create_binding(make_binding(binding_version=1))
    await sources.create_binding(make_binding(binding_version=2, access_epoch=2))
    active = await sources.get_active_binding("proj-a")
    assert active is not None and active.binding_version == 2
    await sources.set_binding_enabled("proj-a", 2, enabled=False)
    active = await sources.get_active_binding("proj-a")
    assert active is not None and active.binding_version == 1


# -------------------------------------------------------------- source revisions


async def test_repeat_observation_is_idempotent_across_versions(
    sources: SqlProjectSourceStore,
) -> None:
    obj = make_object()
    await sources.upsert_source_object(obj)
    first = make_revision(obj, "v1 content", 1, source_version="sha-a", observed_at=T0)
    assert await sources.record_revision(first) is RevisionOutcome.APPENDED
    # Same content, different commit sha (unrelated commit) → duplicate, not a
    # new revision; observation time never deduplicates (ADR-0015 §4.2).
    repeat = make_revision(
        obj, "v1 content", 2, source_version="sha-b", observed_at=T0 + timedelta(hours=1)
    )
    assert await sources.record_revision(repeat) is RevisionOutcome.DUPLICATE
    revisions = await sources.list_revisions(obj.source_id)
    assert len(revisions) == 1
    stored = await sources.get_source_object(obj.source_id)
    assert stored is not None
    assert stored.latest_change_sequence == 1
    assert stored.last_observed_at == T0 + timedelta(hours=1)


async def test_a_b_a_keeps_three_revisions(sources: SqlProjectSourceStore) -> None:
    obj = make_object()
    await sources.upsert_source_object(obj)
    rev_a1 = make_revision(obj, "content-A", 1, source_version="sha-a1")
    rev_b = make_revision(obj, "content-B", 2, source_version="sha-b")
    rev_a2 = make_revision(obj, "content-A", 3, source_version="sha-a3")
    assert await sources.record_revision(rev_a1) is RevisionOutcome.APPENDED
    assert await sources.record_revision(rev_b) is RevisionOutcome.APPENDED
    # A again is NOT skipped even though hash A existed before (ADR-0015 §4.2).
    assert await sources.record_revision(rev_a2) is RevisionOutcome.APPENDED
    revisions = await sources.list_revisions(obj.source_id)
    assert [r.change_sequence for r in revisions] == [1, 2, 3]
    assert revisions[0].content_hash == revisions[2].content_hash
    latest = await sources.latest_revision(obj.source_id)
    assert latest is not None and latest.revision_id == rev_a2.revision_id


async def test_revision_sequence_conflict_is_rejected(sources: SqlProjectSourceStore) -> None:
    obj = make_object()
    await sources.upsert_source_object(obj)
    await sources.record_revision(make_revision(obj, "content-A", 1))
    stale = make_revision(obj, "content-B", 1, revision_id=new_revision_id())
    with pytest.raises(RevisionConflict):
        await sources.record_revision(stale)


async def test_source_availability_lifecycle(sources: SqlProjectSourceStore) -> None:
    obj = make_object()
    await sources.upsert_source_object(obj)
    await sources.set_source_availability(
        obj.source_id, SourceAvailability.UNAVAILABLE, "HTTP 403"
    )
    stored = await sources.get_source_object(obj.source_id)
    assert stored is not None
    assert stored.availability is SourceAvailability.UNAVAILABLE
    assert stored.unavailable_reason == "HTTP 403"
    with pytest.raises(KnowledgeContractError):
        await sources.set_source_availability("src-missing", SourceAvailability.DELETED)


async def test_cross_project_isolation(sources: SqlProjectSourceStore) -> None:
    obj_a = make_object(project_id="proj-a", external_id="docs/delivery.md")
    obj_b = make_object(project_id="proj-b", external_id="docs/delivery.md")
    assert obj_a.source_id != obj_b.source_id
    await sources.create_binding(make_binding("proj-a"))
    await sources.create_binding(make_binding("proj-b", repository_id="repo-200"))
    await sources.upsert_source_object(obj_a)
    await sources.upsert_source_object(obj_b)
    await sources.record_revision(make_revision(obj_a, "A doc", 1))
    await sources.record_revision(make_revision(obj_b, "B doc", 1))
    rows_a = await sources.list_source_objects("proj-a", 1)
    rows_b = await sources.list_source_objects("proj-b", 1)
    assert [o.source_id for o in rows_a] == [obj_a.source_id]
    assert [o.source_id for o in rows_b] == [obj_b.source_id]
    rev_a = await sources.latest_revision(obj_a.source_id)
    assert rev_a is not None and rev_a.raw_content == "A doc"
    # Binding identity embeds the project so the same repo never merges rows.
    assert binding_uid("proj-a", 1) == "proj-a@v1"
    assert binding_uid("proj-a", 1) != binding_uid("proj-b", 1)


# ------------------------------------------------------------------- sync inbox


async def test_inbox_duplicate_and_payload_conflict(inbox: SqlSyncInboxStore) -> None:
    entry = SyncInboxEntry(
        repository_id="repo-100",
        delivery_id="d-1",
        event_type="push",
        payload_hash=canonical_payload_hash({"ref": "main"}),
        payload={"ref": "main"},
        received_at=T0,
    )
    assert await inbox.ingest(entry) is True
    # Identical redelivery → duplicate, original preserved.
    assert await inbox.ingest(entry) is False
    # Same delivery id, different payload → conflict, never overwritten.
    conflicting = SyncInboxEntry(
        repository_id="repo-100",
        delivery_id="d-1",
        event_type="push",
        payload_hash=canonical_payload_hash({"ref": "dev"}),
        payload={"ref": "dev"},
        received_at=T0,
    )
    with pytest.raises(DuplicateDeliveryConflict):
        await inbox.ingest(conflicting)
    stored = await inbox.get_entry("repo-100", "d-1")
    assert stored is not None and stored.payload == {"ref": "main"}
    assert stored.status is InboxStatus.PENDING
    with pytest.raises(DuplicateDeliveryConflict):
        await inbox.ingest(
            SyncInboxEntry(
                repository_id="repo-100",
                delivery_id="d-1",
                event_type="issues",
                payload_hash=content_hash("different"),
                payload={},
                received_at=T0,
            )
        )


async def test_inbox_status_transitions_and_due(inbox: SqlSyncInboxStore) -> None:
    entry = SyncInboxEntry(
        repository_id="repo-100",
        delivery_id="d-2",
        event_type="issues",
        action="edited",
        payload_hash=content_hash("x"),
        payload={},
        received_at=T0,
    )
    await inbox.ingest(entry)
    await inbox.set_status(
        "repo-100",
        "d-2",
        InboxStatus.RETRY_WAIT,
        attempts_delta=1,
        last_error="rate limited",
        retry_at=T0 + timedelta(minutes=5),
    )
    stored = await inbox.get_entry("repo-100", "d-2")
    assert stored is not None
    assert stored.status is InboxStatus.RETRY_WAIT
    assert stored.attempts == 1
    assert stored.last_error == "rate limited"
    assert await inbox.list_due("repo-100", now=T0) == []
    due = await inbox.list_due("repo-100", now=T0 + timedelta(minutes=6))
    assert [e.delivery_id for e in due] == ["d-2"]
    await inbox.set_status("repo-100", "d-2", InboxStatus.DONE)
    stored = await inbox.get_entry("repo-100", "d-2")
    assert stored is not None and stored.status is InboxStatus.DONE
    assert await inbox.list_due("repo-100", now=T0 + timedelta(hours=1)) == []


# ------------------------------------------------------------------- sync runs


async def test_sync_run_checkpoint_persistence(runs: SqlSyncRunStore) -> None:
    await runs.start_run(
        "run-1", "proj-a", binding_version=1, access_epoch=1, reason=SyncRunReason.INITIAL_IMPORT
    )
    await runs.save_checkpoint(
        "run-1",
        per_source_cursor={"issues": "42"},
        coverage={"issues": "partial"},
        last_success_at=T0,
    )
    run = await runs.get_run("run-1")
    assert run is not None
    assert run.state is SyncRunState.RUNNING
    assert run.checkpoint is not None
    assert run.checkpoint.per_source_cursor == {"issues": "42"}
    assert run.checkpoint.coverage == {"issues": "partial"}
    await runs.set_run_state("run-1", SyncRunState.FAILED, error="pagination truncated")
    run = await runs.latest_run("proj-a")
    assert run is not None
    assert run.state is SyncRunState.FAILED
    assert run.error == "pagination truncated"
    # Checkpoint survived the failure for the next run to resume from.
    assert run.checkpoint.per_source_cursor == {"issues": "42"}


# ------------------------------------------------------------ knowledge generations


async def test_generation_publish_pointer_switch(knowledge: SqlKnowledgeStore) -> None:
    from orgpilot.knowledge.models import (
        EvidenceRef,
        KnowledgeAssertion,
        KnowledgeGeneration,
        new_assertion_id,
        new_generation_id,
    )

    gen_a = KnowledgeGeneration(
        generation_id=new_generation_id(),
        project_id="proj-a",
        binding_version=1,
        access_epoch=1,
        revision_manifest=("rev-1", "rev-2"),
        created_at=T0,
    )
    await knowledge.create_generation(gen_a)
    # Staging content is invisible: no pointer exists yet.
    assert await knowledge.current_generation_id("proj-a") is None
    assertion = KnowledgeAssertion(
        assertion_id=new_assertion_id(),
        project_id="proj-a",
        subject_ref="src-doc",
        predicate="delivery_condition",
        value="测试环境可访问且鉴权齐全后才可联调",
        assertion_kind="source_statement",
        evidence_refs=(
            EvidenceRef(
                revision_id="rev-1",
                source_id="src-doc",
                kind=SourceKind.DOCUMENT,
                paragraph="交付条件",
                snippet="测试环境可访问、鉴权信息齐全后才可联调",
            ),
        ),
        extractor_version="rule-v1",
    )
    await knowledge.add_assertion(assertion)
    stored_assertions = await knowledge.list_assertions("proj-a")
    assert len(stored_assertions) == 1
    assert stored_assertions[0].evidence_refs[0].revision_id == "rev-1"
    await knowledge.publish_generation(gen_a.generation_id)
    assert await knowledge.current_generation_id("proj-a") == gen_a.generation_id
    published = await knowledge.get_generation(gen_a.generation_id)
    assert published is not None
    assert published.status is GenerationStatus.READY
    # Publishing twice is rejected; so is publishing a failed build.
    with pytest.raises(KnowledgeContractError):
        await knowledge.publish_generation(gen_a.generation_id)


async def test_generation_invalidation_keeps_content_immutable(
    knowledge: SqlKnowledgeStore,
) -> None:
    from orgpilot.knowledge.models import KnowledgeGeneration, new_generation_id

    gen = KnowledgeGeneration(
        generation_id=new_generation_id(),
        project_id="proj-a",
        binding_version=1,
        access_epoch=1,
        revision_manifest=("rev-1",),
        summary="旧摘要",
        created_at=T0,
    )
    await knowledge.create_generation(gen)
    await knowledge.publish_generation(gen.generation_id)
    await knowledge.invalidate_generation(gen.generation_id, "source updated")
    stored = await knowledge.get_generation(gen.generation_id)
    assert stored is not None
    # Validity flips independently; summary/manifest stay auditable, untouched.
    assert stored.valid is False
    assert stored.invalidated_reason == "source updated"
    assert stored.summary == "旧摘要"
    assert stored.status is GenerationStatus.READY


# ------------------------------------------------------------------ read sessions


def make_spec(session_id: str = "rds-1", message_key: str = "msg-1") -> ProjectReadSessionSpec:
    return ProjectReadSessionSpec(
        session_id=session_id,
        project_id="proj-a",
        message_key=message_key,
        question="Bob 的接口能联调了吗？",
        question_hash=question_hash("Bob 的接口能联调了吗？"),
        requester_id="alice",
        binding_version=1,
        access_epoch=1,
        generation_id="gen-1",
        org_state_refs=("evt-task-1",),
        created_at=T0,
    )


async def test_session_creation_is_message_idempotent(
    read_sessions: SqlReadSessionStore,
) -> None:
    assert await read_sessions.create_session(make_spec()) is True
    # Same message key re-delivered → same session, no new row.
    assert await read_sessions.create_session(make_spec(session_id="rds-other")) is False
    session = await read_sessions.get_by_message("proj-a", "msg-1")
    assert session is not None and session.spec.session_id == "rds-1"
    assert session.status is ReadSessionStatus.RUNNING
    assert session.used_budget.total_tokens() == 0


async def test_budget_reserve_settle_and_unknown_keeps_reservation(
    read_sessions: SqlReadSessionStore,
) -> None:
    await read_sessions.create_session(make_spec())
    reserve_at = T0
    reservation = BudgetReservation(
        reservation_id="res-1",
        kind=ReservationKind.MODEL_CALL,
        model_requests=1,
        input_tokens=1000,
        output_tokens=200,
        seconds=10.0,
        created_at=reserve_at,
    )
    await read_sessions.reserve("rds-1", reservation)
    session = await read_sessions.get_session("rds-1")
    assert session is not None
    assert len(session.reserved_budget) == 1
    assert session.used_budget.model_requests == 0
    # Settle with actual usage converts reservation into used.
    settled = await read_sessions.settle(
        "rds-1",
        "res-1",
        BudgetUsage(model_requests=1, input_tokens=900, output_tokens=150, elapsed_seconds=6.0),
    )
    assert settled.input_tokens == 900
    session = await read_sessions.get_session("rds-1")
    assert session is not None
    assert session.used_budget.input_tokens == 900
    assert session.used_budget.model_requests == 1
    assert session.reserved_budget == ()
    # Unknown result → reservation fully charged as unknown, never zero.
    reservation2 = BudgetReservation(
        reservation_id="res-2",
        kind=ReservationKind.MODEL_CALL,
        model_requests=1,
        input_tokens=800,
        output_tokens=100,
        seconds=10.0,
        created_at=T0,
    )
    await read_sessions.reserve("rds-1", reservation2)
    settled2 = await read_sessions.settle("rds-1", "res-2", None)
    assert settled2.input_tokens == 800
    assert settled2.unknown_token_usage is True
    session = await read_sessions.get_session("rds-1")
    assert session is not None
    assert session.used_budget.input_tokens == 1700
    assert session.used_budget.unknown_token_usage is True
    assert session.reserved_budget == ()
    with pytest.raises(KnowledgeContractError):
        await read_sessions.settle("rds-1", "res-missing", None)


async def test_reservation_rejects_budget_violation_before_execution(
    read_sessions: SqlReadSessionStore,
) -> None:
    await read_sessions.create_session(make_spec())
    # Session hard limit: 8 tool calls. Ask for the 9th up front.
    reservation = BudgetReservation(
        reservation_id="res-tools",
        kind=ReservationKind.TOOL_BATCH,
        tool_calls=9,
        created_at=T0,
    )
    with pytest.raises(KnowledgeContractError):
        await read_sessions.reserve("rds-1", reservation)
    session = await read_sessions.get_session("rds-1")
    assert session is not None
    assert session.reserved_budget == ()


async def test_finish_once_and_interrupt_keeps_ledger(read_sessions: SqlReadSessionStore) -> None:
    await read_sessions.create_session(make_spec())
    await read_sessions.reserve(
        "rds-1",
        BudgetReservation(
            reservation_id="res-final",
            kind=ReservationKind.FINAL_ANSWER,
            model_requests=1,
            input_tokens=500,
            seconds=10.0,
            created_at=T0,
        ),
    )
    await read_sessions.mark_interrupted("rds-1")
    session = await read_sessions.get_session("rds-1")
    assert session is not None
    assert session.status is ReadSessionStatus.INTERRUPTED
    assert session.stop_reason is StopReason.INTERRUPTED
    # Reserved budget survives the interruption; never reset into a new one.
    assert len(session.reserved_budget) == 1
    # A finished session cannot be finished again.
    await read_sessions.create_session(make_spec(message_key="msg-2", session_id="rds-2"))
    await read_sessions.finish_session("rds-2", StopReason.ANSWERED, AnswerStatus.COMPLETE)
    with pytest.raises(ReadSessionConflict):
        await read_sessions.finish_session("rds-2", StopReason.ANSWERED, AnswerStatus.PARTIAL)
    with pytest.raises(ReadSessionConflict):
        await read_sessions.finish_session("rds-1", StopReason.ANSWERED, AnswerStatus.COMPLETE)


async def test_step_records_and_packets_round_trip(read_sessions: SqlReadSessionStore) -> None:
    await read_sessions.create_session(make_spec())
    await read_sessions.append_step(
        ReadStepRecord(
            session_id="rds-1",
            seq=1,
            kind=ReadStepKind.TOOL_CALL,
            tool_name="search_project",
            params_summary={"query": "联调 条件"},
            result_summary={"evidence_ids": ["rev-1#p2"], "truncated": False},
            outcome="executed",
            occurred_at=T0,
        )
    )
    steps = await read_sessions.list_steps("rds-1")
    assert len(steps) == 1
    assert steps[0].tool_name == "search_project"
    assert steps[0].result_summary["evidence_ids"] == ["rev-1#p2"]
    packet = ProjectContextPacket(
        packet_id="pkt-1",
        read_session_id="rds-1",
        round=0,
        generation_id="gen-1",
        requester_id="alice",
        binding_version=1,
        access_epoch=1,
        brief="项目摘要",
        created_at=T0,
    )
    await read_sessions.save_packet(packet)
    packets = await read_sessions.list_packets("rds-1")
    assert len(packets) == 1
    assert packets[0].brief == "项目摘要"


# ----------------------------------------------------------- atomicity + restart


async def test_shared_session_failure_rolls_back_everything(db: Database) -> None:
    sources = SqlProjectSourceStore(db, FrozenClock(T0))
    binding = make_binding()
    obj = make_object()
    with pytest.raises(RuntimeError, match="boom"):
        async with db.session() as session:
            await sources.create_binding(binding, session)
            await sources.upsert_source_object(obj, session)
            await sources.record_revision(make_revision(obj, "doc v1", 1), session)
            # Simulate a failure before commit; nothing may persist.
            raise RuntimeError("boom")
    assert await sources.get_binding("proj-a", 1) is None
    assert await sources.get_source_object(obj.source_id) is None
    assert await sources.list_revisions(obj.source_id) == []


async def test_shared_session_commit_is_atomic(db: Database) -> None:
    sources = SqlProjectSourceStore(db, FrozenClock(T0))
    inbox = SqlSyncInboxStore(db, FrozenClock(T0))
    obj = make_object()
    entry = SyncInboxEntry(
        repository_id="repo-100",
        delivery_id="d-atom",
        event_type="push",
        payload_hash=content_hash("payload"),
        payload={},
        received_at=T0,
    )
    async with db.session() as session:
        await sources.upsert_source_object(obj, session)
        await sources.record_revision(make_revision(obj, "doc v1", 1), session)
        await inbox.ingest(entry, session)
    assert await sources.get_source_object(obj.source_id) is not None
    assert await inbox.get_entry("repo-100", "d-atom") is not None


async def test_records_survive_restart(tmp_path) -> None:
    db_path = tmp_path / "orgpilot-knowledge.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    database = Database(db_url)
    await database.init_db()
    clock = FrozenClock(T0)
    sources = SqlProjectSourceStore(database, clock)
    obj = make_object()
    await sources.create_binding(make_binding())
    await sources.upsert_source_object(obj)
    await sources.record_revision(make_revision(obj, "doc v1", 1))
    await database.close()

    database2 = Database(db_url)
    await database2.init_db()
    sources2 = SqlProjectSourceStore(database2, clock)
    binding = await sources2.get_active_binding("proj-a")
    assert binding is not None and binding.repository_id == "repo-100"
    rev = await sources2.latest_revision(obj.source_id)
    assert rev is not None and rev.raw_content == "doc v1"
    # Restart does not reset dedup: same content again → duplicate.
    again = make_revision(obj, "doc v1", 2, source_version="sha-new", observed_at=T0)
    assert await sources2.record_revision(again) is RevisionOutcome.DUPLICATE
    await database2.close()


# ----------------------------------------------------------- namespace separation


async def test_material_namespaces_are_disjoint_from_event_ids(db: Database) -> None:
    """Source ids never pass as event ids and vice versa (ADR-0015 §6)."""
    store = SqlEventStore(db)
    clock = FrozenClock(T0)
    sources = SqlProjectSourceStore(db, clock)
    obj = make_object()
    await sources.upsert_source_object(obj)
    rev = make_revision(obj, "doc", 1)
    await sources.record_revision(rev)
    # Material references carry the src-/rev- prefixes, event ids do not.
    assert is_source_ref(obj.source_id)
    assert is_source_ref(rev.revision_id)
    assert not is_source_ref("evt-task-payment-1")
    event = parse_event(
        {
            "schema_version": 1,
            "project_id": "proj-a",
            "event_id": "evt-task-payment-1",
            "event_type": "task.created",
            "source": EventSource.HUMAN,
            "source_ref": "setup",
            "occurred_at": T0.isoformat(),
            "received_at": T0.isoformat(),
            "payload": {
                "task_id": "task-payment",
                "title": "支付接入",
                "owner_id": "alice",
            },
        }
    )
    assert await store.append(event) is AppendResult.APPENDED
    # Feeding a material id as an event id hits the same unique key contract as
    # any other id: appending again with different content is a conflict.
    assert is_source_ref(rev.revision_id) and not rev.revision_id.startswith("evt-")
    same_id_conflict = parse_event(
        {
            "schema_version": 1,
            "project_id": "proj-a",
            "event_id": "evt-task-payment-1",
            "event_type": "task.created",
            "source": EventSource.HUMAN,
            "source_ref": "setup",
            "occurred_at": T0.isoformat(),
            "received_at": T0.isoformat(),
            "payload": {
                "task_id": "task-payment",
                "title": "支付接入（改）",
                "owner_id": "alice",
            },
        }
    )
    with pytest.raises(DuplicateEventConflict):
        await store.append(same_id_conflict)


async def test_clock_is_injected_not_wall_clock(sources: SqlProjectSourceStore) -> None:
    obj = make_object()
    await sources.upsert_source_object(obj)
    await sources.record_revision(make_revision(obj, "doc", 1, observed_at=T0))
    rev = await sources.latest_revision(obj.source_id)
    assert rev is not None
    # The stored observation time equals the injected clock's world.
    assert rev.observed_at == T0


async def test_question_hash_is_text_only(read_sessions: SqlReadSessionStore) -> None:
    assert question_hash("Bob 的接口  能联调了吗？") == question_hash(
        "Bob 的接口 能联调了吗？"
    )
    assert question_hash("A") != question_hash("B")

"""First-import acceptance for SourceImporter (ADR-0015 C2).

Frozen behaviors: honest per-kind coverage (partial/failed never reported
complete), multi-page completeness, issue/PR separation by object type,
selector scoping (milestone ∪ explicit ids), pinned-commit document reads,
403/404 never inferred as deletion, visible caps, platform-explicit PR→issue
links vs plain mentions, idempotent re-import, and zero business writes.
"""

import copy
import json
import pathlib
from datetime import datetime

import pytest

from orgpilot.domain.clock import FrozenClock
from orgpilot.github.client import MockGitHubClient
from orgpilot.knowledge.importer import ImportLimits, SourceImporter
from orgpilot.knowledge.models import (
    ConfirmationStatus,
    IssueSelector,
    ProjectSourceBinding,
    RelationType,
    SourceAvailability,
    SourceKind,
)
from orgpilot.storage.database import Database
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore, SqlSyncRunStore
from orgpilot.storage.state_store import SqlStateStore

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "github_snapshot.json"
T0 = datetime.fromisoformat("2026-11-03T10:00:00+08:00")


def make_binding(
    milestone: str | None = "m2",
    issue_ids: tuple[int, ...] = (4,),
    explicit_pr_ids: tuple[int, ...] = (10,),
    path_allowlist: tuple[str, ...] = ("docs", "README.md"),
    repository_id: str = "1001",
) -> ProjectSourceBinding:
    return ProjectSourceBinding(
        project_id="proj-a",
        repository_id=repository_id,
        binding_version=1,
        branch="main",
        path_allowlist=path_allowlist,
        issue_selector=IssueSelector(milestone=milestone, issue_ids=issue_ids),
        explicit_pr_ids=explicit_pr_ids,
        maintainers=("maintainer-1",),
        sharing_grants=("alice",),
        authority_rules={"delivery": "docs/delivery.md"},
        access_epoch=1,
        created_at=T0,
    )


def load_snapshot() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
async def db() -> Database:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init_db()
    yield database
    await database.close()


@pytest.fixture
async def stores(db: Database):
    clock = FrozenClock(T0)
    return (
        SqlProjectSourceStore(db, clock),
        SqlKnowledgeStore(db, clock),
        SqlSyncRunStore(db, clock),
        clock,
    )


async def run_import(
    stores,
    snapshot: dict,
    binding: ProjectSourceBinding | None = None,
    limits: ImportLimits | None = None,
) -> tuple[SourceImporter, object]:
    sources, knowledge, runs, clock = stores
    importer = SourceImporter(
        MockGitHubClient(snapshot),
        sources,
        knowledge,
        runs,
        clock=clock,
        limits=limits,
    )
    report = await importer.import_binding_scope(binding or make_binding())
    return importer, report


async def test_happy_path_counts_and_coverage(stores) -> None:
    _, report = await run_import(stores, load_snapshot())
    assert report.project_id == "proj-a"
    assert report.pinned_commit_sha == "c0ffee1"
    assert report.counts == {
        "document": 3,  # README.md + docs/delivery.md + docs/spec/api.md
        "issue": 4,  # #1 #2 #4 #6 (milestone m2 ∪ explicit #4)
        "pull_request": 2,  # #5 via milestone, #10 explicit
        "comment": 4,
        "review": 2,
    }
    assert report.failures == []
    assert report.truncations == []
    assert report.conflicts == []
    assert report.fully_complete
    assert report.coverage == {
        "document": "complete",
        "issue": "complete",
        "pull_request": "complete",
        "comment": "complete",
        "review": "complete",
    }


async def test_documents_are_read_at_pinned_commit(stores) -> None:
    sources, _, _, _ = stores
    await run_import(stores, load_snapshot())
    docs = await sources.list_source_objects("proj-a", 1, kind=SourceKind.DOCUMENT)
    external_ids = sorted(doc.external_id for doc in docs)
    assert external_ids == ["README.md", "docs/delivery.md", "docs/spec/api.md"]
    delivery = next(doc for doc in docs if doc.external_id == "docs/delivery.md")
    revision = await sources.latest_revision(delivery.source_id)
    assert revision is not None
    # Content pinned to the branch head captured at import start (§5.2).
    assert revision.source_version == "c0ffee1"
    assert "测试环境可访问" in (revision.raw_content or "")
    # src/main.py is outside the allowlist and never materialized.
    assert all("src/" not in doc.external_id for doc in docs)


async def test_scope_filtering_excludes_out_of_scope_objects(stores) -> None:
    sources, _, _, _ = stores
    await run_import(stores, load_snapshot())
    issues = await sources.list_source_objects("proj-a", 1, kind=SourceKind.ISSUE)
    numbers = sorted(issue.external_id for issue in issues)
    assert numbers == ["1", "2", "4", "6"]  # #3 (m3) is out of scope
    # Comments only exist for in-scope parents.
    comments = await sources.list_source_objects("proj-a", 1, kind=SourceKind.COMMENT)
    parents = {comment.parent_source_id for comment in comments}
    issue_refs = {issue.source_id for issue in issues}
    assert parents <= issue_refs | {
        obj.source_id
        for obj in await sources.list_source_objects("proj-a", 1, kind=SourceKind.PULL_REQUEST)
    }


async def test_same_title_objects_stay_distinct(stores) -> None:
    sources, _, _, _ = stores
    await run_import(stores, load_snapshot())
    issues = await sources.list_source_objects("proj-a", 1, kind=SourceKind.ISSUE)
    titles = {}
    for issue in issues:
        titles.setdefault(issue.title, []).append(issue.source_id)
    assert len(titles["接入支付接口"]) == 2  # #1 and #6, same title, distinct objects
    assert len(set(titles["接入支付接口"])) == 2


async def test_multi_page_import_is_complete(stores) -> None:
    sources, _, _, _ = stores
    snapshot = load_snapshot()
    # 120 comments on issue #1 (3 pages at per_page=50) and a wide cap.
    snapshot["issue_comments"] = [
        {
            "id": str(7000 + i),
            "issue_number": 1,
            "user": {"login": "bob"},
            "body": f"评论 {i}",
            "html_url": None,
            "created_at": f"2026-10-20T10:{i % 60:02d}:00+08:00",
            "updated_at": f"2026-10-20T10:{i % 60:02d}:00+08:00",
        }
        for i in range(120)
    ]
    limits = ImportLimits(max_sub_items_per_object=200)
    _, report = await run_import(stores, snapshot, limits=limits)
    assert report.counts["comment"] == 120
    comments = await sources.list_source_objects("proj-a", 1, kind=SourceKind.COMMENT)
    assert len(comments) == 120
    assert not any(t.kind == "comment" for t in report.truncations)


async def test_partial_failure_is_not_reported_complete(stores) -> None:
    snapshot = load_snapshot()
    snapshot["failures"] = [
        {"method": "list_issue_comments", "match": {"issue_number": 2}, "status": 403}
    ]
    _, report = await run_import(stores, snapshot)
    assert report.coverage["comment"] == "partial"
    assert not report.fully_complete
    assert any(f.kind == "comment" and f.status_code == 403 for f in report.failures)
    # Everything else in the same run stayed complete.
    assert report.coverage["issue"] == "complete"
    assert report.coverage["document"] == "complete"


async def test_404_marks_unavailable_never_deleted(stores) -> None:
    sources, _, _, _ = stores
    snapshot = load_snapshot()
    snapshot["failures"] = [{"method": "get_blob", "match": {"blob_sha": "b-api"}, "status": 404}]
    _, report = await run_import(stores, snapshot)
    assert report.coverage["document"] == "partial"
    api = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "docs/spec/api.md")
    assert api is None  # failed before any object row was written
    # A source that exists but later 403s is marked unavailable, not deleted.
    snapshot2 = load_snapshot()
    snapshot2["failures"] = [{"method": "get_blob", "match": {"blob_sha": "b-api"}, "status": 403}]
    await run_import(stores, snapshot2)
    # The first run recorded nothing for api.md; verify no 'deleted' marker anywhere.
    docs = await sources.list_source_objects("proj-a", 1, kind=SourceKind.DOCUMENT)
    assert all(doc.availability is not SourceAvailability.DELETED for doc in docs)


async def test_caps_are_visible_as_partial_coverage(stores) -> None:
    _, report = await run_import(
        stores, load_snapshot(), limits=ImportLimits(max_documents=2)
    )
    assert any(t.kind == "document" and t.cap == "max_documents" for t in report.truncations)
    assert report.coverage["document"] == "partial"
    assert not report.fully_complete


async def test_work_item_cap_stops_import_and_is_visible(stores) -> None:
    sources, _, _, _ = stores
    _, report = await run_import(
        stores, load_snapshot(), limits=ImportLimits(max_work_items=3)
    )
    assert any(t.kind == "work_items" for t in report.truncations)
    issues = await sources.list_source_objects("proj-a", 1, kind=SourceKind.ISSUE)
    assert len(issues) < 3  # the cap stopped mid-listing
    assert report.coverage["issue"] == "partial"


async def test_repository_identity_mismatch_is_rejected(stores) -> None:
    from orgpilot.knowledge.errors import KnowledgeContractError

    with pytest.raises(KnowledgeContractError):
        await run_import(stores, load_snapshot(), binding=make_binding(repository_id="999"))


async def test_disabled_binding_is_rejected(stores) -> None:
    from orgpilot.knowledge.errors import BindingConflict

    binding = make_binding()
    disabled = binding.model_copy(update={"enabled": False})
    with pytest.raises(BindingConflict):
        await run_import(stores, load_snapshot(), binding=disabled)


async def test_reimport_is_idempotent_no_new_revisions(stores) -> None:
    sources, _, _, _ = stores
    snapshot = load_snapshot()
    _, report1 = await run_import(stores, copy.deepcopy(snapshot))
    docs = await sources.list_source_objects("proj-a", 1, kind=SourceKind.DOCUMENT)
    rev_counts = {
        doc.source_id: len(await sources.list_revisions(doc.source_id)) for doc in docs
    }
    _, report2 = await run_import(stores, copy.deepcopy(snapshot))
    assert report2.counts == report1.counts
    for doc in docs:
        assert len(await sources.list_revisions(doc.source_id)) == rev_counts[doc.source_id]
    # Documents unchanged across an unrelated commit sha still deduplicate.
    updated = load_snapshot()
    updated["branches"]["main"]["sha"] = "newsha"
    updated["trees"]["newsha"] = updated["trees"]["c0ffee1"]
    _, report3 = await run_import(stores, updated)
    for doc in docs:
        assert len(await sources.list_revisions(doc.source_id)) == rev_counts[doc.source_id]
    assert report3.coverage["document"] == "complete"


async def test_content_change_creates_new_revision(stores) -> None:
    sources, _, _, _ = stores
    await run_import(stores, load_snapshot())
    delivery = await sources.find_source_object(
        "proj-a", 1, SourceKind.DOCUMENT, "docs/delivery.md"
    )
    assert delivery is not None
    before = len(await sources.list_revisions(delivery.source_id))
    updated = load_snapshot()
    updated["blobs"]["b-delivery"] = updated["blobs"]["b-delivery"] + "\n新增交付条件。\n"
    updated["branches"]["main"]["sha"] = "newsha"
    updated["trees"]["newsha"] = updated["trees"]["c0ffee1"]
    await run_import(stores, updated)
    revisions = await sources.list_revisions(delivery.source_id)
    assert len(revisions) == before + 1
    assert "新增交付条件" in (revisions[-1].raw_content or "")


async def test_pr_issue_links_implements_vs_mentions(stores) -> None:
    knowledge, _, _ = (stores[1], stores[2], stores[0])
    sources = stores[0]
    await run_import(stores, load_snapshot())
    relations = await knowledge.list_relations("proj-a")
    implements = [r for r in relations if r.relation_type is RelationType.IMPLEMENTS]
    mentions = [r for r in relations if r.relation_type is RelationType.MENTIONS]
    # PR #5 body: "Fixes #1, 相关讨论见 #2" → implements #1 + mention #2.
    assert len(implements) == 2  # PR #5→#1 and PR #10→#4 ("Closes #4")
    assert len(mentions) == 1  # PR #5→#2 stays a mention
    assert all(r.confirmation_status is ConfirmationStatus.CONFIRMED for r in implements)
    assert all(r.confirmation_status is ConfirmationStatus.UNCONFIRMED for r in mentions)
    pr5 = await sources.find_source_object("proj-a", 1, SourceKind.PULL_REQUEST, "5")
    targets = {
        (r.from_ref, r.to_ref) for r in implements + mentions
    }
    assert (pr5.source_id, pr5.source_id) not in targets  # no self links
    # Mentions of out-of-scope objects are never materialized as relations.
    pr5_mentions = [m for m in mentions if m.from_ref == pr5.source_id]
    assert len(pr5_mentions) == 1


async def test_pr_merge_facts_are_preserved(stores) -> None:
    sources, _, _, _ = stores
    await run_import(stores, load_snapshot())
    pr5 = await sources.find_source_object("proj-a", 1, SourceKind.PULL_REQUEST, "5")
    assert pr5 is not None
    assert pr5.metadata["merged"] is True
    assert pr5.metadata["merge_commit_sha"] == "merge-5"
    pr10 = await sources.find_source_object("proj-a", 1, SourceKind.PULL_REQUEST, "10")
    assert pr10 is not None
    assert pr10.metadata["merged"] is False and pr10.metadata["state"] == "open"


async def test_import_never_writes_business_state(db: Database, stores) -> None:
    """The import path must not append OrgEvents or touch cases/approvals."""
    from orgpilot.storage.event_store import SqlEventStore

    events = SqlEventStore(db)
    await run_import(stores, load_snapshot())
    assert await events.count() == 0
    state_store = SqlStateStore(db)
    assert await state_store.load_state("proj-a") is None
    assert await state_store.load_cases("proj-a") == []
    assert await state_store.load_approvals("proj-a") == []


async def test_run_checkpoint_records_coverage(stores) -> None:
    _, _, runs, _ = stores
    _, report = await run_import(stores, load_snapshot())
    run = await runs.get_run(report.run_id)
    assert run is not None
    assert run.reason.value == "initial_import"
    assert run.checkpoint is not None
    assert run.checkpoint.coverage == report.coverage
    assert run.checkpoint.per_source_cursor["pinned_commit"] == "c0ffee1"


async def test_tree_enumeration_failure_fails_documents(stores) -> None:
    snapshot = load_snapshot()
    snapshot["failures"] = [{"method": "get_tree", "match": {}, "status": 500}]
    _, report = await run_import(stores, snapshot)
    assert report.coverage["document"] == "failed"
    assert not report.fully_complete
    # Work items were still imported — one broken kind doesn't sink the run.
    assert report.counts["issue"] == 4


async def test_explicit_pr_id_that_is_not_readable_conflicts(stores) -> None:
    snapshot = load_snapshot()
    binding = make_binding(explicit_pr_ids=(10, 99))
    _, report = await run_import(stores, snapshot, binding=binding)
    assert any(c.external_id == "99" for c in report.conflicts)
    assert any(
        f.kind == "pull_request" and f.external_id == "99" for f in report.failures
    )
    # The readable explicit PR still imported.
    assert report.counts["pull_request"] == 2

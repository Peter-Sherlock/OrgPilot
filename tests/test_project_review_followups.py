"""Regressions for the second PR #20 review (15 findings, 2026-11-04).

Each test names the finding it pins. Sync tests reuse the C3 harness from
``test_github_sync``; read-loop tests reuse the material fixture.
"""

import asyncio
import base64
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, select

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import FrozenClock
from orgpilot.domain.enums import HealthStatus
from orgpilot.domain.models import MemberState
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.app import build_gateway_service
from orgpilot.github.client import GitHubReadError, HttpGitHubClient, is_access_failure
from orgpilot.intent.replies import ReplyKind, RouterOutcome
from orgpilot.knowledge.access import check_generation
from orgpilot.knowledge.answerer import (
    LLMProjectAnswerer,
    ProjectAnswerDecision,
    ReadLoopView,
    ScriptedAnswerGenerator,
)
from orgpilot.knowledge.context import ProjectContextBuilder
from orgpilot.knowledge.derive import DerivationPipeline
from orgpilot.knowledge.importer import SourceImporter
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetUsage,
    EvidenceRef,
    GroundedStatement,
    InboxStatus,
    ReadStepKind,
    SourceAvailability,
    SourceKind,
    StopReason,
    content_hash,
    new_revision_id,
)
from orgpilot.knowledge.read_loop import ProjectReadOrchestrator, _returned_reference
from orgpilot.knowledge.read_tools import guard_text
from orgpilot.knowledge.retrieval import IndexEntry, MaterialIndex
from orgpilot.storage.models import ProjectReadStepRecord
from tests import test_github_sync as sync_harness
from tests.test_github_sync import T0, ingest_and_drain, load_snapshot, make_binding
from tests.test_knowledge_context import make_org_state
from tests.test_project_review_regressions import make_loop, material_fixture  # noqa: F401

# The C3 sync harness fixtures (``setup`` needs ``db`` and ``clock``).
db = sync_harness.db
clock = sync_harness.clock
setup = sync_harness.setup

# ---------------------------------------------------------------- 1: availability


class TestAvailabilityRecovers:
    async def test_issue_readable_again_after_403_is_available(self, setup) -> None:
        service, sources, client, _ = setup
        client._snapshot = load_snapshot()
        await ingest_and_drain(
            service, "d-seed", "issues", {"action": "opened", "issue": {"number": 1}}
        )
        client._snapshot["failures"] = [
            {"method": "get_issue", "match": {"number": 1}, "status": 403}
        ]
        await ingest_and_drain(
            service, "d-403", "issues", {"action": "edited", "issue": {"number": 1}}
        )
        issue = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        assert issue.availability is SourceAvailability.UNAVAILABLE

        client._snapshot["failures"] = []
        _, counts = await ingest_and_drain(
            service, "d-ok", "issues", {"action": "edited", "issue": {"number": 1}}
        )
        issue = await sources.get_source_object(issue.source_id)
        assert issue.availability is SourceAvailability.AVAILABLE
        assert issue.unavailable_reason is None
        assert counts["refreshed"] == 1
        # Unchanged content: availability recovered without a new revision.
        assert len(await sources.list_revisions(issue.source_id)) == 1

    async def test_document_restored_at_same_path_is_available(self, setup) -> None:
        service, sources, client, snapshot = setup
        client._snapshot = snapshot
        await ingest_and_drain(service, "d-seed", "push", {"ref": "refs/heads/main"})
        full_tree = list(snapshot["trees"]["c0ffee1"])
        snapshot["trees"]["c0ffee1"] = [e for e in full_tree if e["path"] != "docs/spec/api.md"]
        await ingest_and_drain(service, "d-del", "push", {"ref": "refs/heads/main"})
        api = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "docs/spec/api.md")
        assert api.availability is SourceAvailability.DELETED

        snapshot["trees"]["c0ffee1"] = full_tree
        await ingest_and_drain(service, "d-back", "push", {"ref": "refs/heads/main"})
        api = await sources.get_source_object(api.source_id)
        assert api.availability is SourceAvailability.AVAILABLE

    async def test_full_recheck_restores_unavailable_issue(self, setup) -> None:
        service, sources, client, _ = setup
        client._snapshot = load_snapshot()
        await service.manual_recheck("proj-a")
        issue = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        await sources.set_source_availability(
            issue.source_id, SourceAvailability.UNAVAILABLE, "platform status 404"
        )
        await service.manual_recheck("proj-a")
        issue = await sources.get_source_object(issue.source_id)
        assert issue.availability is SourceAvailability.AVAILABLE

    async def test_revoked_source_is_not_resurrected_by_a_read(self, setup) -> None:
        service, sources, client, _ = setup
        client._snapshot = load_snapshot()
        await ingest_and_drain(
            service, "d-seed", "issues", {"action": "opened", "issue": {"number": 1}}
        )
        issue = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        await sources.set_source_availability(
            issue.source_id, SourceAvailability.REVOKED, "admin revoked"
        )
        await ingest_and_drain(
            service, "d-again", "issues", {"action": "edited", "issue": {"number": 1}}
        )
        issue = await sources.get_source_object(issue.source_id)
        assert issue.availability is SourceAvailability.REVOKED

    @pytest.mark.parametrize(
        "protected", [SourceAvailability.REVOKED, SourceAvailability.OUT_OF_SCOPE]
    )
    async def test_platform_failure_does_not_clear_admin_protection(self, setup, protected) -> None:
        service, sources, client, _ = setup
        client._snapshot = load_snapshot()
        await service.manual_recheck("proj-a")
        issue = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
        await sources.set_source_availability(issue.source_id, protected, "admin decision")
        client._snapshot["failures"] = [
            {"method": "get_issue", "match": {"number": 1}, "status": 403}
        ]
        payload = {"action": "edited", "issue": {"number": 1}}
        await ingest_and_drain(service, "protected-403", "issues", payload)
        after_failure = await sources.get_source_object(issue.source_id)
        assert after_failure.availability is protected
        assert after_failure.unavailable_reason == "admin decision"

        client._snapshot["failures"] = []
        await ingest_and_drain(service, "protected-200", "issues", payload)
        assert (await sources.get_source_object(issue.source_id)).availability is protected
        current_id = await service.knowledge.current_generation_id("proj-a")
        current = await service.knowledge.get_generation(current_id)
        assert issue.latest_revision_id not in current.revision_manifest


# ------------------------------------------------- 2 + 3: blobs and rate limits


def test_non_utf8_blob_is_a_read_error_not_a_crash() -> None:
    payload = {
        "encoding": "base64",
        "content": base64.b64encode(b"\x89PNG\r\n\x1a\n\xff\xfe").decode(),
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as transport:
        client = HttpGitHubClient("org", "demo", client=transport)
        with pytest.raises(GitHubReadError) as caught:
            client.get_blob("b-png")
    assert caught.value.status_code == 415
    assert not is_access_failure(caught.value.status_code)


async def test_images_are_not_documents_and_a_bad_blob_is_partial(setup) -> None:
    service, sources, client, snapshot = setup
    snapshot["trees"]["c0ffee1"] += [
        {"path": "docs/arch.png", "blob_sha": "b-png", "size": 10},
        {"path": "docs/legacy.md", "blob_sha": "b-legacy", "size": 10},
    ]
    snapshot["blobs"]["b-legacy"] = "legacy"
    snapshot["failures"] = [
        {"method": "get_blob", "match": {"blob_sha": "b-legacy"}, "status": 415}
    ]
    client._snapshot = snapshot
    report = await service.manual_recheck("proj-a")
    assert report is not None
    assert report.coverage["document"] == "partial"
    assert report.counts["issue"] > 0  # the import carried on past the bad file
    assert ("get_blob", {"blob_sha": "b-png"}) not in client.calls
    await ingest_and_drain(service, "d-push", "push", {"ref": "refs/heads/main"})
    assert ("get_blob", {"blob_sha": "b-png"}) not in client.calls
    assert (
        await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "docs/arch.png") is None
    )


@pytest.mark.parametrize("already_imported", [False, True])
async def test_bad_markdown_does_not_block_other_push_revisions(setup, already_imported) -> None:
    service, sources, client, snapshot = setup
    bad_path = "docs/legacy.md"
    bad_blob = {"path": bad_path, "blob_sha": "b-legacy", "size": 10}
    snapshot["blobs"]["b-legacy"] = "# Legacy"
    if already_imported:
        snapshot["trees"]["c0ffee1"].append(bad_blob)
    client._snapshot = snapshot
    await service.manual_recheck("proj-a")
    readme = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "README.md")
    if not already_imported:
        snapshot["trees"]["c0ffee1"].append(bad_blob)
    snapshot["blobs"]["b-readme"] += "\nNew revision from the push.\n"
    snapshot["failures"] = [
        {"method": "get_blob", "match": {"blob_sha": "b-legacy"}, "status": 415}
    ]
    _, counts = await ingest_and_drain(service, "push-415", "push", {"ref": "refs/heads/main"})
    assert counts["refreshed"] == 1
    entry = await service.inbox.get_entry("1001", "push-415")
    assert entry.status is InboxStatus.DONE
    assert bad_path in entry.last_error
    current_readme = await sources.get_source_object(readme.source_id)
    assert current_readme.latest_revision_id != readme.latest_revision_id
    bad = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, bad_path)
    assert bad.availability is SourceAvailability.UNAVAILABLE
    current_id = await service.knowledge.current_generation_id("proj-a")
    current = await service.knowledge.get_generation(current_id)
    assert current.coverage["document"] == "partial"
    assert current_readme.latest_revision_id in current.revision_manifest
    assert bad.latest_revision_id not in current.revision_manifest

    snapshot["failures"] = []
    await ingest_and_drain(service, "push-fixed", "push", {"ref": "refs/heads/main"})
    recovered = await sources.get_source_object(bad.source_id)
    assert recovered.availability is SourceAvailability.AVAILABLE
    assert recovered.latest_revision_id is not None


def _rate_limited(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        headers={"x-ratelimit-remaining": "0"},
        json={"message": "API rate limit exceeded"},
    )


def test_rate_limited_403_retries_and_reports_429(monkeypatch) -> None:
    monkeypatch.setattr("orgpilot.github.client.time.sleep", lambda seconds: None)
    calls = []

    def handler(request):
        calls.append(request)
        return _rate_limited(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = HttpGitHubClient("org", "demo", client=transport)
        with pytest.raises(GitHubReadError) as caught:
            client.get_issue(1)
    assert len(calls) == 3
    assert caught.value.status_code == 429
    assert not is_access_failure(caught.value.status_code)


def test_permission_403_is_still_an_access_failure(monkeypatch) -> None:
    monkeypatch.setattr("orgpilot.github.client.time.sleep", lambda seconds: None)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = HttpGitHubClient("org", "demo", client=transport)
        with pytest.raises(GitHubReadError) as caught:
            client.get_issue(1)
    assert len(calls) == 1
    assert is_access_failure(caught.value.status_code)


async def test_rate_limited_refresh_retries_instead_of_hiding_material(setup) -> None:
    service, sources, client, _ = setup
    client._snapshot = load_snapshot()
    await ingest_and_drain(
        service, "d-seed", "issues", {"action": "opened", "issue": {"number": 1}}
    )
    client._snapshot["failures"] = [{"method": "get_issue", "match": {"number": 1}, "status": 429}]
    _, counts = await ingest_and_drain(
        service, "d-limit", "issues", {"action": "edited", "issue": {"number": 1}}
    )
    assert counts["retry_wait"] == 1
    entry = await service.inbox.get_entry("1001", "d-limit")
    assert entry.status is InboxStatus.RETRY_WAIT
    issue = await sources.find_source_object("proj-a", 1, SourceKind.ISSUE, "1")
    assert issue.availability is SourceAvailability.AVAILABLE


# ------------------------------------------------- 9: comments on milestone PRs


@pytest.mark.parametrize("issue_payload", [{"number": 5, "pull_request": {}}, {"number": 5}])
async def test_comment_on_milestone_selected_pr_is_synced(setup, issue_payload) -> None:
    service, sources, client, _ = setup
    snapshot = load_snapshot()
    snapshot["issue_comments"].append(
        {
            "id": 7100,
            "issue_number": 5,
            "user": {"login": "carol"},
            "body": "PR 5 的联调评论",
            "html_url": "https://github.com/org/demo/pull/5#issuecomment-7100",
            "created_at": "2026-10-21T09:00:00+08:00",
            "updated_at": "2026-10-21T09:00:00+08:00",
        }
    )
    client._snapshot = snapshot
    # PR #5 is selected by the m2 milestone, not by explicit_pr_ids=(10,).
    await ingest_and_drain(
        service, "d-pr5", "pull_request", {"action": "opened", "pull_request": {"number": 5}}
    )
    pr = await sources.find_source_object("proj-a", 1, SourceKind.PULL_REQUEST, "5")
    assert pr is not None
    _, counts = await ingest_and_drain(
        service, "d-c5", "issue_comment", {"action": "created", "issue": issue_payload}
    )
    assert counts["refreshed"] == 1
    comment = await sources.find_source_object(
        "proj-a", 1, SourceKind.COMMENT, "7100", parent_source_id=pr.source_id
    )
    assert comment is not None
    assert comment.metadata["parent_kind"] == "pull_request"


# ------------------------------------------------------ 10: import robustness


async def test_issue_listing_failure_is_recorded_as_coverage(setup) -> None:
    service, _, client, _ = setup
    snapshot = load_snapshot()
    snapshot["failures"] = [{"method": "list_issues", "match": {}, "status": 403}]
    client._snapshot = snapshot
    report = await service.manual_recheck("proj-a")
    assert report is not None and not report.fully_complete
    assert report.coverage["issue"] == "failed"
    assert report.coverage["document"] == "complete"
    run = await service.runs.get_run(report.run_id)
    assert run.state.value == "completed"


async def test_unexpected_import_crash_marks_the_run_failed(setup, monkeypatch) -> None:
    service, sources, client, _ = setup
    client._snapshot = load_snapshot()

    def boom(commit_sha):
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "get_tree", boom)
    importer = SourceImporter(
        client=client,
        sources=sources,
        knowledge=service.knowledge,
        runs=service.runs,
        clock=service.clock,
    )
    with pytest.raises(RuntimeError):
        await importer.import_binding_scope(make_binding(), run_id="run-boom")
    run = await service.runs.get_run("run-boom")
    assert run.state.value == "failed"


async def test_import_checkpoint_exception_marks_the_run_failed(setup, monkeypatch) -> None:
    service, sources, client, _ = setup
    client._snapshot = load_snapshot()
    save_checkpoint = service.runs.save_checkpoint

    async def fail_after_write(*args, **kwargs):
        await save_checkpoint(*args, **kwargs)
        raise RuntimeError("checkpoint interrupted")

    monkeypatch.setattr(service.runs, "save_checkpoint", fail_after_write)
    importer = SourceImporter(client, sources, service.knowledge, service.runs, clock=service.clock)
    with pytest.raises(RuntimeError, match="checkpoint interrupted"):
        await importer.import_binding_scope(make_binding(), run_id="run-checkpoint")
    run = await service.runs.get_run("run-checkpoint")
    assert run.state.value == "failed"
    assert run.checkpoint.last_success_at is None


# --------------------------------------------------------- 12: periodic recheck


async def test_empty_webhook_drain_does_not_suppress_periodic_recheck(setup, clock) -> None:
    service, _, client, snapshot = setup
    service.recheck_interval_seconds = 1800
    client._snapshot = snapshot
    await service.manual_recheck("proj-a")
    clock.advance(timedelta(seconds=1801))
    # A ping writes nothing, so its run has no success time.
    await ingest_and_drain(service, "d-ping", "ping", {"zen": "hi"})
    assert await service.periodic_recheck_if_due("proj-a") is True


@pytest.mark.parametrize("legacy_timestamp", [False, True])
async def test_partial_read_does_not_postpone_full_recheck(setup, clock, legacy_timestamp) -> None:
    service, _, client, snapshot = setup
    service.recheck_interval_seconds = 1800
    client._snapshot = snapshot
    assert (await service.manual_recheck("proj-a")).fully_complete
    clock.advance(timedelta(seconds=1700))
    snapshot["failures"] = [{"method": "list_issues", "match": {}, "status": 429}]
    partial = await service.manual_recheck("proj-a")
    assert not partial.fully_complete
    checkpoint = (await service.runs.get_run(partial.run_id)).checkpoint
    assert checkpoint.last_success_at is None
    if legacy_timestamp:
        await service.runs.save_checkpoint(
            partial.run_id,
            per_source_cursor=checkpoint.per_source_cursor,
            coverage=partial.coverage,
            last_success_at=clock.now(),
        )
    snapshot["failures"] = []
    assert await service.periodic_recheck_if_due("proj-a") is False
    clock.advance(timedelta(seconds=101))
    assert await service.periodic_recheck_if_due("proj-a") is True


async def test_first_incomplete_read_does_not_disable_periodic_rechecks(setup) -> None:
    service, _, client, snapshot = setup
    client._snapshot = snapshot
    snapshot["failures"] = [{"method": "list_issues", "match": {}, "status": 429}]
    assert not (await service.manual_recheck("proj-a")).fully_complete
    snapshot["failures"] = []
    assert await service.periodic_recheck_if_due("proj-a") is True


async def test_periodic_rechecks_only_active_binding_once_per_project(setup, clock) -> None:
    service, sources, client, snapshot = setup
    client._snapshot = snapshot
    assert (await service.manual_recheck("proj-a")).fully_complete
    clock.advance(timedelta(seconds=1))
    newer = make_binding().model_copy(update={"binding_version": 2, "access_epoch": 2})
    await sources.create_binding(newer)
    snapshot["failures"] = [{"method": "list_issues", "match": {}, "status": 429}]
    assert await service.recheck_all_due() == ["proj-a"]
    latest = await service.runs.latest_run("proj-a")
    assert latest.binding_version == 2
    assert latest.checkpoint.last_success_at is None


async def test_route_does_not_store_unsupported_events() -> None:
    from tests.test_github_webhook_route import make_harness, sign

    body = json.dumps({"repository": {"id": 1001}, "action": "completed"}).encode()
    async for http, app in make_harness():
        response = await http.post(
            "/api/v1/github/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "check_run",
                "X-GitHub-Delivery": "d-ci",
                "X-Hub-Signature-256": sign(body),
            },
        )
        entry = await app.state.github_sync.inbox.get_entry("1001", "d-ci")
    assert response.json()["msg"] == "ignored_event"
    assert entry is None


# ------------------------------------------------------------ 15: derivation


async def _change_document(sources, path: str, extra: str) -> None:
    obj = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, path)
    latest = await sources.latest_revision(obj.source_id)
    content = (latest.raw_content or "") + extra
    await sources.record_revision(
        latest.model_copy(
            update={
                "revision_id": new_revision_id(),
                "change_sequence": latest.change_sequence + 1,
                "raw_content": content,
                "content_hash": content_hash(content),
            }
        )
    )


async def test_aborted_build_leaves_no_duplicate_assertions(env) -> None:
    sources, knowledge, binding = env["sources"], env["knowledge"], env["binding"]
    pipeline = DerivationPipeline(sources, knowledge, env["clock"])
    await _change_document(sources, "docs/delivery.md", "\n补充：联调前登记环境。\n")
    base = env["generation"]
    # An aborted build: assertion rows persisted, nothing published.
    await pipeline._derive(binding, base)
    rebuilt = await pipeline.build_and_publish(binding, base_generation=base)
    await _change_document(sources, "README.md", "\n补充说明。\n")
    latest = await pipeline.build_and_publish(binding, base_generation=rebuilt)
    by_id = {a.assertion_id: a for a in await knowledge.list_assertions("proj-a")}
    facts = [
        (by_id[i].subject_ref, by_id[i].predicate, by_id[i].value) for i in latest.assertion_refs
    ]
    assert facts and len(facts) == len(set(facts))


# ------------------------------------------------- 4 + 5: context check cost/scope


async def test_generation_check_cost_does_not_grow_with_manifest(env) -> None:
    statements: list[str] = []
    engine = env["db"].engine.sync_engine

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert await check_generation(env["sources"], env["knowledge"], env["generation"]) is None
    finally:
        event.remove(engine, "before_cursor_execute", record)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(env["generation"].revision_manifest) > 5
    assert len(selects) <= 4


async def test_document_answer_replays_formal_state_only_once(env) -> None:
    calls = []

    def provider(project_id):
        calls.append(project_id)
        return make_org_state()

    answer = await make_loop(env, org_state_provider=provider).run(
        "proj-a", "alice", "q-replays", "项目目标是什么？"
    )
    assert answer.statements
    assert len(calls) == 1  # pinned at start; no task was read afterwards


class TaskReadAnswer:
    generator_version = "review-task-read-set"

    def __init__(self, *, read_task=True, cite_task=False):
        self.read_task = read_task
        self.cite_task = cite_task
        self.calls = 0

    async def decide(self, view):
        self.calls += 1
        if self.read_task and self.calls == 1:
            return ProjectAnswerDecision.lookup(
                "read_task_state", {"task_ids": ["task-payment", "not-yet-registered"]}
            )
        ref = next(
            ref for ref in view.evidence if ref.source_id.startswith("task:") == self.cite_task
        )
        return ProjectAnswerDecision.finish(
            (GroundedStatement(text="本轮答复内容", evidence_refs=(ref,)),), AnswerStatus.COMPLETE
        )


async def test_read_set_survives_recreating_the_orchestrator(env):
    state = make_org_state()
    generator = TaskReadAnswer()
    args = ("proj-a", "alice", "persisted-task-read-set", "项目目标是什么？")
    first = await make_loop(env, generator=generator, org_state_provider=lambda _: state).run(*args)
    assert first.stop_reason is StopReason.ANSWERED
    assert all(
        not ref.source_id.startswith("task:")
        for statement in first.statements
        for ref in statement.evidence_refs
    )
    steps = await env["read_sessions"].list_steps(first.read_session_id)
    step = next(
        step for step in steps
        if step.tool_name == "read_task_state" and step.kind is ReadStepKind.TOOL_CALL
    )
    assert step.result_summary["returned_task_ids"] == ["task-payment"]

    replay = make_loop(env, generator=generator, org_state_provider=lambda _: state)
    task = state.tasks["task-payment"]
    state.tasks["not-yet-registered"] = task.model_copy(update={"task_id": "not-yet-registered"})
    assert (await replay.run(*args)) == first
    state.tasks["task-payment"] = task.model_copy(update={"health_status": HealthStatus.DELAYED})
    stale = await replay.run(*args)
    assert stale.stop_reason is StopReason.CONTEXT_CHANGED
    assert stale.budget_usage == first.budget_usage
    assert not stale.statements
    assert generator.calls == 2


async def test_legacy_tool_result_without_returned_ids_cannot_be_replayed(env):
    state = make_org_state()
    generator = TaskReadAnswer()
    loop = make_loop(env, generator=generator, org_state_provider=lambda _: state)
    args = ("proj-a", "alice", "legacy-task-read-set", "项目目标是什么？")
    first = await loop.run(*args)
    assert first.stop_reason is StopReason.ANSWERED
    async with env["db"].session() as session:
        step = (
            await session.execute(
                select(ProjectReadStepRecord).where(
                    ProjectReadStepRecord.session_id == first.read_session_id,
                    ProjectReadStepRecord.kind == ReadStepKind.TOOL_CALL.value,
                    ProjectReadStepRecord.tool_name == "read_task_state",
                )
            )
        ).scalar_one()
        summary = json.loads(step.result_summary_json)
        summary.pop("returned_task_ids")
        step.result_summary_json = json.dumps(summary)
    stale = await loop.run(*args)
    assert stale.stop_reason is StopReason.CONTEXT_CHANGED
    assert not stale.statements
    assert generator.calls == 2


@pytest.mark.skipif(
    not hasattr(ProjectReadOrchestrator, "revalidate"),
    reason="gateway delivery hook is supplied by PR #20 cf0c0e7",
)
@pytest.mark.parametrize(
    "read_task,cite_task,phase,changed_task,delivery_reason,replay_reason",
    [
        (True, True, "checkpoint", "task-payment", "context_changed", "context_changed"),
        (True, False, "record", "task-payment", "context_changed", "context_changed"),
        (True, False, "after_send", "task-payment", "answered", "context_changed"),
        (True, True, "record", "task-unrelated", "answered", "answered"),
        (False, False, "record", "task-payment", "answered", "answered"),
    ],
)
async def test_gateway_revalidates_persisted_task_read_set(
    env, monkeypatch, read_task, cite_task, phase, changed_task, delivery_reason, replay_reason
):
    state = make_org_state()
    clock = env["clock"]
    state.members["alice"] = MemberState(
        member_id="alice",
        display_name="Alice",
        role="pm",
        source_event_id="member-alice",
        last_update_at=clock.now(),
    )
    state.tasks["task-unrelated"] = state.tasks["task-payment"].model_copy(
        update={"task_id": "task-unrelated"}
    )
    generator = TaskReadAnswer(read_task=read_task, cite_task=cite_task)
    settings = OrgPilotSettings(project_qa_enabled=True)
    sent = []

    async def agent(project_id):
        return SimpleNamespace(projector=SimpleNamespace(state=state))

    class Messenger:
        async def send_text(self, recipient, text):
            sent.append(text)

    def make_router():
        service, _, _ = build_gateway_service(settings, env["db"], clock)
        monkeypatch.setattr(service, "get_or_replay_agent", agent)
        monkeypatch.setattr(service.project_knowledge.orchestrator, "generator", generator)
        router = service.intent_router
        monkeypatch.setattr(router, "messenger_for", lambda _: Messenger())
        return router

    def change_task():
        task = state.tasks[changed_task]
        state.tasks[changed_task] = task.model_copy(update={"health_status": HealthStatus.DELAYED})

    router = make_router()
    if phase == "checkpoint":
        checkpoint = router.checkpoint

        async def after_checkpoint(project_id):
            await checkpoint(project_id)
            change_task()

        monkeypatch.setattr(router, "checkpoint", after_checkpoint)
    elif phase == "record":
        record = router.messages.record_outcome

        async def after_record(**fields):
            await record(**fields)
            change_task()

        monkeypatch.setattr(router.messages, "record_outcome", after_record)

    args = {
        "project_id": "proj-a",
        "sender_id": "alice",
        "message_id": "task-state-dispatch",
        "text": "/项目 项目目标是什么？",
        "occurred_at": clock.now(),
    }
    outcome = await router.handle_message(**args)
    assert outcome.data["stop_reason"] == delivery_reason
    assert len(sent) == 1
    assert ("本轮答复内容" in sent[0]) == (delivery_reason == "answered")
    recorded = await router.messages.get_outcome("proj-a", args["message_id"])
    assert recorded.reply_text == sent[0]

    if phase == "after_send":
        change_task()
    replay = await make_router().handle_message(**args)
    assert replay.duplicate and replay.suppress_reply_send
    assert replay.data["stop_reason"] == replay_reason
    assert ("本轮答复内容" in replay.reply_text) == (replay_reason == "answered")
    assert replay.data["budget_usage"] == outcome.data["budget_usage"]
    assert len(sent) == 1
    assert generator.calls == (2 if read_task else 1)


# ------------------------------------------------------- 6: citations and prompt


def test_title_or_empty_locator_is_not_a_location_claim() -> None:
    known = EvidenceRef(
        revision_id="rev-1",
        source_id="src-1",
        kind=SourceKind.DOCUMENT,
        title="交付约定",
        paragraph="交付条件#L3-L5",
        line_start=3,
        line_end=5,
        snippet="测试环境可访问、鉴权信息齐全",
    )
    evidence = {"k": known}
    for locator in ("交付约定", "", None):
        ref = EvidenceRef(
            revision_id="rev-1",
            source_id="src-1",
            kind=SourceKind.DOCUMENT,
            paragraph=locator,
            snippet="测试环境可访问",
        )
        rebuilt = _returned_reference(ref, evidence)
        assert rebuilt is not None and rebuilt.paragraph == "交付条件#L3-L5", locator
    forged = known.model_copy(update={"paragraph": "L999999", "snippet": "测试环境可访问"})
    assert _returned_reference(forged, evidence) is None


def test_prompt_names_every_tool_argument_and_the_locator_rule() -> None:
    prompt = LLMProjectAnswerer.SYSTEM_PROMPT
    for name in ("query", "source_id", "revision_id", "relation_type", "task_ids", "cursor"):
        assert name in prompt
    assert "paragraph" in prompt and "null" in prompt


# ------------------------------------------------------------------ 7: ranking


def test_rare_term_outranks_common_fragments() -> None:
    def entry(source_id: str, text: str) -> IndexEntry:
        return IndexEntry(
            source_id=source_id,
            revision_id=f"rev-{source_id}",
            kind=SourceKind.DOCUMENT,
            external_id=f"{source_id}.md",
            title=None,
            paragraph=f"p#{source_id}",
            line_start=1,
            line_end=1,
            text=text,
        )

    index = MaterialIndex()
    # The src-a* ids sort first; only src-z is about payment callbacks.
    index._entries = [entry(f"src-a{i}", f"项目说明第{i}段：项目进度正常。") for i in range(8)]
    index._entries.append(entry("src-z", "项目的支付回调需要重试三次。"))
    hits = index.search("项目支付回调的重试策略")
    assert hits[0].source_id == "src-z"
    assert len(hits) == 9  # ranking changed, the hit set did not


# --------------------------------------------------------- 8: scripted policy


async def test_scripted_generator_reports_gap_when_nothing_matches() -> None:
    unrelated = EvidenceRef(
        revision_id="rev-x",
        source_id="src-x",
        kind=SourceKind.DOCUMENT,
        title="交付",
        paragraph="交付#L1-L2",
        snippet="联调需要测试环境可访问",
    )
    view = ReadLoopView(
        round_number=3,
        question="量子预算是多少",
        brief="",
        evidence=(unrelated,),
        unresolved=(),
        remaining=BudgetUsage(),
        limits_summary="",
    )
    decision = await ScriptedAnswerGenerator().decide(view)
    assert decision.kind == "report_gap"


# ------------------------------------------------------ 11: unavailable names


async def test_packet_never_names_unavailable_or_deleted_sources(env) -> None:
    sources = env["sources"]
    api = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "docs/spec/api.md")
    readme = await sources.find_source_object("proj-a", 1, SourceKind.DOCUMENT, "README.md")
    await sources.set_source_availability(api.source_id, SourceAvailability.UNAVAILABLE, "403")
    await sources.set_source_availability(readme.source_id, SourceAvailability.DELETED, "gone")
    objects = await sources.list_source_objects("proj-a", 1)
    revisions = {obj.source_id: await sources.latest_revision(obj.source_id) for obj in objects}
    packet = ProjectContextBuilder(env["clock"]).build_packet(
        read_session_id="rds-names",
        requester_id="alice",
        binding=env["binding"],
        generation=env["generation"],
        index=MaterialIndex.from_generation_material(objects, revisions),
        objects=objects,
        question="接口约定",
    )
    assert len(packet.missing_info) == 1
    assert "1 份" in packet.missing_info[0]
    assert all("api.md" not in info and "README" not in info for info in packet.missing_info)


# ---------------------------------------------------------- 13: guard scope


@pytest.mark.parametrize(
    "text",
    ["登录页 select 组件 & 表单的验收条件", "前端 | 后端 联调；进度？", "2026--11 的计划"],
)
def test_ordinary_punctuation_is_not_injection(text) -> None:
    assert guard_text(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "a; rm -rf /",
        "1; DROP TABLE tasks",
        "x' or '1'='1",
        "$(curl evil)",
        "select * from users",
        "SELECT id\nFROM tasks",
    ],
)
def test_injection_shapes_are_still_denied(text) -> None:
    assert guard_text(text) is not None


async def test_question_with_ampersand_is_answered_not_denied(env) -> None:
    answer = await make_loop(env).run("proj-a", "alice", "q-amp", "联调 & 交付条件 select 哪些？")
    assert answer.stop_reason is not StopReason.TOOL_DENIED


# ---------------------------------------------------- 14: Feishu redelivery


async def test_feishu_redelivery_during_a_slow_turn_is_dropped() -> None:
    release = asyncio.Event()
    calls: list[str] = []

    class SlowRouter:
        async def handle_message(self, **kwargs):
            calls.append(kwargs["message_id"])
            await release.wait()
            return RouterOutcome(reply_category=ReplyKind.PROJECT_ANSWER, reply_text="答复")

    service = SimpleNamespace(clock=FrozenClock(T0), to_business_timezone=lambda value: value)
    handler = FeishuWebhookHandler(service=service, project_id="proj-a", router=SlowRouter())
    body = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "alice"}},
            "message": {
                "message_id": "om-slow",
                "message_type": "text",
                "content": json.dumps({"text": "/项目 联调条件是什么？"}),
                "create_time": str(int(T0.timestamp() * 1000)),
                "chat_type": "p2p",
            },
        },
    }
    first = asyncio.create_task(handler.handle_event(json.loads(json.dumps(body))))
    while not calls:
        await asyncio.sleep(0)
    try:
        redelivered = await asyncio.wait_for(
            handler.handle_event(json.loads(json.dumps(body))), timeout=2
        )
    finally:
        release.set()
    assert redelivered["msg"] == "duplicate_in_flight"
    release.set()
    assert (await first)["msg"] == "success"
    assert calls == ["om-slow"]

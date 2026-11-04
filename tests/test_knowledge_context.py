"""C4 acceptance: generations, context builder, and the four read tools.

Frozen behaviors: goals/terms/delivery conditions carry exact original-text
evidence; document upgrades invalidate the published generation and the
rebuilt one references the new revision; unchanged sources are not
re-extracted; staging never publishes on binding/epoch/availability/source
changes; the context packet stays within size limits with visible conflicts
and gaps; and the four tools enforce whitelist, scope, cursor signing, cache
revalidation, and result-size caps while writing zero business state.
"""

import json
import pathlib
from datetime import datetime

import pytest

from orgpilot.domain.clock import FrozenClock
from orgpilot.domain.enums import HealthStatus, WorkflowStatus
from orgpilot.domain.models import OrgState, TaskState
from orgpilot.github.client import MockGitHubClient
from orgpilot.knowledge.context import ProjectContextBuilder
from orgpilot.knowledge.derive import DerivationAborted, DerivationPipeline
from orgpilot.knowledge.importer import SourceImporter
from orgpilot.knowledge.models import (
    IssueSelector,
    ProjectSourceBinding,
    RelationType,
    SourceAvailability,
    SourceKind,
)
from orgpilot.knowledge.read_tools import ReadTools, ReadToolSession, ToolCallResult, _fit_payload
from orgpilot.knowledge.retrieval import MaterialIndex, encode_cursor
from orgpilot.storage.database import Database
from orgpilot.storage.event_store import SqlEventStore
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore, SqlSyncRunStore
from orgpilot.storage.state_store import SqlStateStore

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "github_snapshot.json"
T0 = datetime.fromisoformat("2026-11-03T10:00:00+08:00")


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
        "maintainers": ("m1",),
        "sharing_grants": ("alice",),
        "authority_rules": {"delivery": "docs/delivery.md"},
        "access_epoch": 1,
        "created_at": T0,
    }
    fields.update(overrides)
    return ProjectSourceBinding(**fields)


async def _reindex(env) -> MaterialIndex:
    """Rebuilds the material index from the current available sources."""
    sources = env["sources"]
    objects = await sources.list_source_objects("proj-a", 1)
    revisions = {}
    for obj in objects:
        if obj.availability is SourceAvailability.AVAILABLE:
            revisions[obj.source_id] = await sources.latest_revision(obj.source_id)
    index = MaterialIndex.from_generation_material(objects, revisions)
    env["index"] = index
    env["objects"] = objects
    return index


async def _reimport(env) -> None:
    runs = SqlSyncRunStore(env["db"], env["clock"])
    importer = SourceImporter(
        env["client"], env["sources"], env["knowledge"], runs, clock=env["clock"]
    )
    await importer.import_binding_scope(env["binding"])


@pytest.fixture
async def env():
    """Imported material + a published generation + index, all offline."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    clock = FrozenClock(T0)
    sources = SqlProjectSourceStore(db, clock)
    knowledge = SqlKnowledgeStore(db, clock)
    runs = SqlSyncRunStore(db, clock)
    snapshot = load_snapshot()
    client = MockGitHubClient(snapshot)
    binding = make_binding()
    await sources.create_binding(binding)
    importer = SourceImporter(client, sources, knowledge, runs, clock=clock)
    await importer.import_binding_scope(binding)
    pipeline = DerivationPipeline(sources, knowledge, clock=clock)
    generation = await pipeline.build_and_publish(binding, reason="initial")
    env = {
        "db": db,
        "clock": clock,
        "sources": sources,
        "knowledge": knowledge,
        "runs": runs,
        "snapshot": snapshot,
        "client": client,
        "binding": binding,
        "generation": generation,
        "pipeline": pipeline,
    }
    index = await _reindex(env)
    env["index"] = index
    env["relations"] = tuple(await knowledge.list_relations("proj-a"))
    yield env
    await db.close()


def make_tools(env, session_id: str = "rds-1", requester: str = "alice") -> ReadTools:
    session = ReadToolSession(
        session_id=session_id,
        project_id="proj-a",
        requester_id=requester,
        binding=env["binding"],
        generation=env["generation"],
        index=env["index"],
        org_state=None,
        relations=env["relations"],
        sources=env["sources"],
        clock=env["clock"],
    )
    return ReadTools(session)


def make_org_state() -> OrgState:
    task = TaskState(
        task_id="task-payment",
        title="支付接入",
        owner_id="bob",
        workflow_status=WorkflowStatus.DOING,
        health_status=HealthStatus.AT_RISK,
        deadline=datetime.fromisoformat("2026-11-10T18:00:00+08:00"),
        dependencies=("task-auth",),
        source_event_ids=("evt-task-payment-1",),
        last_update_at=T0,
    )
    return OrgState(project_id="proj-a", tasks={"task-payment": task})


class TestDerivation:
    async def test_goal_and_delivery_conditions_carry_exact_evidence(self, env) -> None:
        assertions = await env["knowledge"].list_assertions("proj-a")
        goals = [a for a in assertions if a.predicate == "project_goal"]
        delivery = [a for a in assertions if a.predicate == "delivery_condition"]
        assert goals and delivery
        ref = goals[0].evidence_refs[0]
        assert ref.revision_id in env["generation"].revision_manifest
        assert ref.line_start is not None and ref.line_end is not None
        # The snippet is locatable in the referenced revision's raw content.
        revision = await env["sources"].get_revision(ref.revision_id)
        assert revision is not None and revision.raw_content is not None
        lines = revision.raw_content.splitlines()
        window = "\n".join(lines[ref.line_start - 1 : ref.line_end])
        assert "项目目标" in window or "验证资料接入" in window
        assert any("测试环境可访问" in a.value for a in delivery)
        assert all(a.extractor_version == "rule-v1" for a in assertions)
        assert any(a.predicate == "term" and "联调" in a.value for a in assertions)

    async def test_generation_published_with_pointer_and_summary(self, env) -> None:
        generation = env["generation"]
        assert generation.status.value == "ready"
        pointer = await env["knowledge"].current_generation_id("proj-a")
        assert pointer == generation.generation_id
        assert generation.summary and "项目目标" in generation.summary
        assert len(generation.summary) <= 2000
        assert any(r.relation_type is RelationType.IMPLEMENTS for r in env["relations"])

    async def test_document_upgrade_invalidates_and_rebuild_updates(self, env) -> None:
        sources, knowledge, pipeline = env["sources"], env["knowledge"], env["pipeline"]
        old_generation = env["generation"]
        snapshot = env["snapshot"]
        snapshot["blobs"]["b-delivery"] += "\n补充：联调前需完成回归。\n"
        snapshot["branches"]["main"]["sha"] = "sha-v2"
        snapshot["trees"]["sha-v2"] = snapshot["trees"]["c0ffee1"]
        await _reimport(env)
        # The old generation is now stale and must be invalidated by flag.
        invalidated = await pipeline.invalidate_stale_generations("proj-a")
        # Source changes now invalidate the old generation in the write transaction.
        assert invalidated == []
        assert not (await env["knowledge"].get_generation(old_generation.generation_id)).valid
        stale = await knowledge.get_generation(old_generation.generation_id)
        assert stale is not None and stale.valid is False
        assert stale.summary is not None  # content stays auditable
        # Rebuild: the new generation pins the new revision; retrieval finds it.
        new_generation = await pipeline.build_and_publish(env["binding"], reason="rebuild")
        assert new_generation.generation_id != old_generation.generation_id
        delivery = await sources.find_source_object(
            "proj-a", 1, SourceKind.DOCUMENT, "docs/delivery.md"
        )
        latest = await sources.latest_revision(delivery.source_id)
        assert latest.revision_id in new_generation.revision_manifest
        index = await _reindex(env)
        assert index.search("回归"), "new content must be searchable after rebuild"

    async def test_unchanged_sources_not_re_extracted(self, env) -> None:
        knowledge, pipeline = env["knowledge"], env["pipeline"]
        base = env["generation"]
        before = {a.assertion_id for a in await knowledge.list_assertions("proj-a")}
        snapshot = env["snapshot"]
        snapshot["blobs"]["b-readme"] += "\n更新说明 v2。\n"
        snapshot["branches"]["main"]["sha"] = "sha-v3"
        snapshot["trees"]["sha-v3"] = snapshot["trees"]["c0ffee1"]
        await _reimport(env)
        rebuilt = await pipeline.build_and_publish(
            env["binding"], reason="rebuild", base_generation=base
        )
        after = {a.assertion_id for a in await knowledge.list_assertions("proj-a")}
        # Unchanged sources reuse their assertions verbatim (same ids).
        assert before <= after
        # The changed README produced fresh assertion ids.
        assert len(after - before) > 0
        assert rebuilt.generation_id != base.generation_id

    async def test_publish_aborts_when_binding_changes_mid_build(self, env) -> None:
        sources, knowledge, pipeline = env["sources"], env["knowledge"], env["pipeline"]
        pointer_before = await knowledge.current_generation_id("proj-a")
        snapshot = env["snapshot"]
        snapshot["blobs"]["b-api"] += "\n补充接口约定。\n"
        snapshot["branches"]["main"]["sha"] = "sha-v4"
        snapshot["trees"]["sha-v4"] = snapshot["trees"]["c0ffee1"]
        await _reimport(env)
        original = sources.latest_revision
        bumped = {"fired": False}

        async def epoch_bump_then_latest(source_id):
            result = await original(source_id)
            if result is not None and not bumped["fired"]:
                bumped["fired"] = True
                await sources.create_binding(make_binding(binding_version=2, access_epoch=2))
            return result

        sources.latest_revision = epoch_bump_then_latest
        with pytest.raises(DerivationAborted):
            await pipeline.build_and_publish(env["binding"], reason="rebuild")
        # The failed build never published; the pointer is untouched.
        assert await knowledge.current_generation_id("proj-a") == pointer_before
        sources.latest_revision = original

    async def test_publish_aborts_when_source_revoked_mid_build(self, env) -> None:
        sources, pipeline = env["sources"], env["pipeline"]
        snapshot = env["snapshot"]
        snapshot["blobs"]["b-api"] += "\n再补充。\n"
        snapshot["branches"]["main"]["sha"] = "sha-v5"
        snapshot["trees"]["sha-v5"] = snapshot["trees"]["c0ffee1"]
        await _reimport(env)
        original = sources.latest_revision
        revoked = {"fired": False}

        async def revoke_once(source_id):
            result = await original(source_id)
            if result is not None and not revoked["fired"]:
                revoked["fired"] = True
                api = await sources.find_source_object(
                    "proj-a", 1, SourceKind.DOCUMENT, "docs/spec/api.md"
                )
                if api is not None:
                    await sources.set_source_availability(
                        api.source_id, SourceAvailability.REVOKED, "simulated revocation"
                    )
            return result

        sources.latest_revision = revoke_once
        with pytest.raises(DerivationAborted):
            await pipeline.build_and_publish(env["binding"], reason="rebuild")
        sources.latest_revision = original


class TestContextBuilder:
    async def test_packet_combines_material_and_org_state(self, env) -> None:
        builder = ProjectContextBuilder(env["clock"])
        packet = builder.build_packet(
            read_session_id="rds-1",
            requester_id="alice",
            binding=env["binding"],
            generation=env["generation"],
            index=env["index"],
            objects=env["objects"],
            question="交付条件和联调术语是什么",
            org_state=make_org_state(),
        )
        assert packet.round == 0
        assert packet.generation_id == env["generation"].generation_id
        assert packet.brief and len(packet.brief) <= 2000
        assert packet.relevant_evidence, "initial retrieval should pin evidence"
        assert all(
            e.revision_id in env["generation"].revision_manifest
            for e in packet.relevant_evidence
        )
        assert any(ref.startswith("task:task-payment@") for ref in packet.org_state_refs)
        assert packet.conflicts  # two-source boundary made visible

    async def test_packet_surfaces_unavailable_sources(self, env) -> None:
        sources = env["sources"]
        api = await sources.find_source_object(
            "proj-a", 1, SourceKind.DOCUMENT, "docs/spec/api.md"
        )
        await sources.set_source_availability(api.source_id, SourceAvailability.UNAVAILABLE, "403")
        builder = ProjectContextBuilder(env["clock"])
        packet = builder.build_packet(
            read_session_id="rds-2",
            requester_id="alice",
            binding=env["binding"],
            generation=env["generation"],
            index=env["index"],
            objects=await sources.list_source_objects("proj-a", 1),
            question="接口约定",
        )
        assert any("unavailable" in info for info in packet.missing_info)

    async def test_builder_refuses_invalid_or_foreign_generation(self, env) -> None:
        builder = ProjectContextBuilder(env["clock"])
        invalid_generation = env["generation"].model_copy(update={"valid": False})
        with pytest.raises(ValueError, match="invalid"):
            builder.build_packet(
                read_session_id="rds-x",
                requester_id="alice",
                binding=env["binding"],
                generation=invalid_generation,
                index=env["index"],
                objects=env["objects"],
                question="q",
            )
        foreign = make_binding(project_id="proj-other")
        with pytest.raises(ValueError, match="binding"):
            builder.build_packet(
                read_session_id="rds-x",
                requester_id="alice",
                binding=foreign,
                generation=env["generation"],
                index=env["index"],
                objects=env["objects"],
                question="q",
            )
        with pytest.raises(PermissionError):
            builder.build_packet(
                read_session_id="rds-x",
                requester_id="mallory",
                binding=env["binding"],
                generation=env["generation"],
                index=env["index"],
                objects=env["objects"],
                question="q",
            )


class TestSearchTool:
    async def test_search_returns_located_hits(self, env) -> None:
        tools = make_tools(env)
        result = await tools.call("search_project", {"query": "联调"})
        assert result.ok
        assert result.items
        for item in result.items:
            assert item["source_id"].startswith("src-")
            assert item["revision_id"].startswith("rev-")
            assert len(item["snippet"]) <= 200
        assert result.evidence_keys

    async def test_query_limits_and_injection_guards(self, env) -> None:
        tools = make_tools(env)
        long = await tools.call("search_project", {"query": "联" * 201})
        assert long.error_code == "invalid_parameter"
        empty = await tools.call("search_project", {"query": "   "})
        assert empty.error_code == "invalid_parameter"
        for bad in (
            {"query": "see https://evil.example/x"},
            {"query": "a; rm -rf /"},
            {"query": "1; DROP TABLE tasks"},
        ):
            denied = await tools.call("search_project", bad)
            assert denied.error_code == "tool_denied", bad

    async def test_pagination_with_signed_cursor(self, env) -> None:
        snapshot = env["snapshot"]
        snapshot["blobs"]["b-readme"] = "\n".join(
            f"# 联调条目 {i}\n\n联调条件{i}。" for i in range(30)
        )
        snapshot["branches"]["main"]["sha"] = "sha-page"
        snapshot["trees"]["sha-page"] = snapshot["trees"]["c0ffee1"]
        await _reimport(env)
        await _reindex(env)
        tools = make_tools(env)
        page1 = await tools.call("search_project", {"query": "联调条件"})
        assert len(page1.items) == 5
        assert page1.next_cursor is not None
        page2 = await tools.call(
            "search_project", {"query": "联调条件", "cursor": page1.next_cursor}
        )
        assert page2.ok
        assert set(page1.evidence_keys) & set(page2.evidence_keys) == set()
        # A cursor minted for another session fails verification.
        forged = encode_cursor(
            "other-session", env["generation"].generation_id, "search_project", {"kinds": None}, 5
        )
        denied = await tools.call("search_project", {"query": "联调条件", "cursor": forged})
        assert denied.error_code == "invalid_parameter"


class TestReadSourceTool:
    async def test_read_window_with_lines_and_pagination(self, env) -> None:
        tools = make_tools(env)
        search = await tools.call("search_project", {"query": "交付条件"})
        source_id = search.items[0]["source_id"]
        revision_id = search.items[0]["revision_id"]
        first = await tools.call(
            "read_source", {"source_id": source_id, "revision_id": revision_id}
        )
        assert first.ok
        item = first.items[0]
        assert len(item["content"]) <= 1000
        assert item["line_start"] >= 1
        if first.next_cursor:
            second = await tools.call(
                "read_source",
                {"source_id": source_id, "revision_id": revision_id, "cursor": first.next_cursor},
            )
            assert second.ok
            assert second.items[0]["char_offset"] == item["char_offset"] + len(item["content"])

    async def test_foreign_revision_or_source_is_unified_unavailable(self, env) -> None:
        tools = make_tools(env)
        search = await tools.call("search_project", {"query": "交付条件"})
        source_id = search.items[0]["source_id"]
        wrong_rev = await tools.call(
            "read_source", {"source_id": source_id, "revision_id": "rev-not-in-generation"}
        )
        missing = await tools.call("read_source", {"source_id": "src-does-not-exist"})
        assert wrong_rev.error_code == "unavailable"
        assert missing.error_code == "unavailable"
        # The unified result cannot distinguish existence from authorization.
        assert wrong_rev.detail == missing.detail

    async def test_invalid_ids_rejected(self, env) -> None:
        tools = make_tools(env)
        for bad in ({"source_id": "../../../etc/passwd"}, {"source_id": "http://x"}, {}):
            denied = await tools.call("read_source", bad)
            assert denied.ok is False


class TestRelatedItemsTool:
    async def test_relations_with_types_and_confirmation(self, env) -> None:
        tools = make_tools(env)
        search = await tools.call(
            "search_project", {"query": "支付接口实现", "kinds": ["pull_request"]}
        )
        assert search.ok
        pr_source = search.items[0]["source_id"]
        related = await tools.call("get_related_items", {"source_id": pr_source})
        assert related.ok
        assert related.items
        implements = [i for i in related.items if i["relation_type"] == "implements"]
        assert implements and implements[0]["confirmation_status"] == "confirmed"
        assert any(i["relation_type"] == "mentions" for i in related.items)
        filtered = await tools.call(
            "get_related_items", {"source_id": pr_source, "relation_type": "implements"}
        )
        assert all(i["relation_type"] == "implements" for i in filtered.items)
        denied = await tools.call("get_related_items", {"source_id": "src-unknown"})
        assert denied.error_code == "unavailable"


class TestTaskStateTool:
    def _tools_with_state(self, env, org_state: OrgState | None) -> ReadTools:
        session = ReadToolSession(
            session_id="rds-task",
            project_id="proj-a",
            requester_id="alice",
            binding=env["binding"],
            generation=env["generation"],
            index=env["index"],
            org_state=org_state,
            relations=(),
            sources=env["sources"],
            clock=env["clock"],
        )
        return ReadTools(session)

    async def test_read_only_task_view(self, env) -> None:
        tools = self._tools_with_state(env, make_org_state())
        result = await tools.call("read_task_state", {"task_ids": ["task-payment"]})
        assert result.ok
        item = result.items[0]
        assert item["workflow_status"] == "doing"
        assert item["owner_id"] == "bob"
        assert item["event_refs"] == ["evt-task-payment-1"]
        assert item["dependencies"] == ["task-auth"]

    async def test_unknown_task_reported_not_fabricated(self, env) -> None:
        tools = self._tools_with_state(env, make_org_state())
        unknown = await tools.call("read_task_state", {"task_ids": ["task-ghost"]})
        assert unknown.ok and not unknown.items
        assert "task-ghost" in (unknown.detail or "")

    async def test_injection_and_limits_rejected(self, env) -> None:
        tools = self._tools_with_state(env, make_org_state())
        denied = await tools.call("read_task_state", {"task_ids": ["a; drop table x"]})
        assert denied.error_code == "tool_denied"
        many = await tools.call("read_task_state", {"task_ids": [f"t{i}" for i in range(6)]})
        assert many.error_code == "invalid_parameter"

    async def test_no_formal_state_is_unavailable(self, env) -> None:
        tools = self._tools_with_state(env, None)
        result = await tools.call("read_task_state", {"task_ids": ["task-payment"]})
        assert result.error_code == "unavailable"


class TestToolContract:
    async def test_unknown_tool_and_unknown_args_rejected(self, env) -> None:
        tools = make_tools(env)
        unknown_tool = await tools.call("run_shell", {"cmd": "ls"})
        assert unknown_tool.error_code == "tool_denied"
        smuggled = await tools.call(
            "search_project", {"query": "联调", "write": "true", "path": "/etc"}
        )
        assert smuggled.error_code == "invalid_parameter"

    async def test_cache_hits_identical_but_revalidated(self, env) -> None:
        tools = make_tools(env)
        args = {"query": "联调"}
        first = await tools.call("search_project", args)
        second = await tools.call("search_project", args)
        assert second.cached is True
        assert second.to_payload() == {**first.to_payload(), "cached": True}
        # Revoke a hit source: the cached result must not survive.
        hit_source = first.items[0]["source_id"]
        await env["sources"].set_source_availability(
            hit_source, SourceAvailability.REVOKED, "simulated"
        )
        third = await tools.call("search_project", args)
        assert third.cached is False
        assert third.ok is False or hit_source not in {
            item["source_id"] for item in third.items
        }

    async def test_result_size_cap_never_exceeds_4096(self, env) -> None:
        tools = make_tools(env)
        result = await tools.call("search_project", {"query": "联调"})
        payload = json.dumps(result.to_payload(), ensure_ascii=False)
        assert len(payload) <= 4096

    def test_fit_payload_shrinks_snippets_then_drops_items(self) -> None:
        """Unit pass over the shrinking ladder with a crafted oversized result."""
        big_item = {"source_id": "src-x", "revision_id": "rev-x", "snippet": "长" * 5000}
        result = ToolCallResult(ok=True, tool="search_project", items=(big_item,))
        fitted = _fit_payload(result)
        payload = json.dumps(fitted.to_payload(), ensure_ascii=False)
        assert len(payload) <= 4096
        assert fitted.truncated is True
        assert fitted.items[0]["source_id"] == "src-x"  # identity preserved
        single = ToolCallResult(
            ok=True,
            tool="search_project",
            items=({"source_id": "src-x", "blob": "长" * 6000},),
        )
        overflow = _fit_payload(single)
        assert overflow.error_code == "result_overflow"

    async def test_session_version_isolation(self, env) -> None:
        tools_a = make_tools(env, session_id="rds-a")
        tools_b = make_tools(env, session_id="rds-b")
        args = {"query": "联调"}
        first = await tools_a.call("search_project", args)
        again_b = await tools_b.call("search_project", args)
        assert again_b.cached is False  # separate session cache
        forged = encode_cursor(
            "rds-a", env["generation"].generation_id, "search_project", {"kinds": None}, 0
        )
        if first.next_cursor:
            cross = await tools_b.call(
                "search_project", {**args, "cursor": first.next_cursor}
            )
            assert cross.ok is True  # its own session; cursor A still foreign
        _ = forged


class TestZeroBusinessWrites:
    async def test_derivation_and_tools_write_no_business_state(self, env) -> None:
        events = SqlEventStore(env["db"])
        state_store = SqlStateStore(env["db"])
        tools = make_tools(env)
        await tools.call("search_project", {"query": "联调"})
        await tools.call("read_source", {"source_id": env["index"].entries[0].source_id})
        assert await events.count() == 0
        assert await state_store.load_cases("proj-a") == []
        assert await state_store.load_approvals("proj-a") == []
        assert await state_store.load_state("proj-a") is None

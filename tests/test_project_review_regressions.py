"""PR #20 review regressions, using real stores and public controller surfaces."""

import pytest

from orgpilot.knowledge.answerer import ProjectAnswerDecision
from orgpilot.knowledge.errors import ReadSessionConflict
from orgpilot.knowledge.models import AnswerStatus, GroundedStatement, StopReason
from orgpilot.knowledge.read_tools import ReadTools, ReadToolSession
from orgpilot.knowledge.retrieval import MaterialIndex
from tests import test_project_read_loop as loop_fixtures

make_loop = loop_fixtures.make_loop


@pytest.fixture(name="env")
async def material_fixture():
    fixture = loop_fixtures.env.__wrapped__()
    try:
        yield await anext(fixture)
    finally:
        await fixture.aclose()


async def make_tools(material):
    objects = await material["sources"].list_source_objects("proj-a", 1)
    revisions = {
        obj.source_id: await material["sources"].latest_revision(obj.source_id) for obj in objects
    }
    return ReadTools(
        ReadToolSession(
            session_id="rds-review",
            project_id="proj-a",
            requester_id="alice",
            binding=material["binding"],
            generation=material["generation"],
            index=MaterialIndex.from_generation_material(objects, revisions),
            org_state=None,
            sources=material["sources"],
            clock=material["clock"],
        )
    )


async def test_r1_replay_rechecks_disabled_binding(env):
    loop = make_loop(env)
    first = await loop.run("proj-a", "alice", "r1-replay", "项目目标是什么？")
    assert first.statements
    await env["sources"].set_binding_enabled("proj-a", 1, False)
    replay = await loop.run("proj-a", "alice", "r1-replay", "项目目标是什么？")
    assert replay.stop_reason is StopReason.ACCESS_DENIED
    assert not replay.statements


@pytest.mark.parametrize(
    "requester,question",
    [
        ("bob", "项目目标是什么？"),
        ("alice", "另一个问题"),
    ],
)
async def test_r1_same_message_rejects_identity_conflict(env, requester, question):
    loop = make_loop(env)
    await loop.run("proj-a", "alice", "r1-conflict", "项目目标是什么？")
    with pytest.raises(ReadSessionConflict):
        await loop.run("proj-a", requester, "r1-conflict", question)


async def test_r1_revocation_during_finish_drops_content(env):
    class Revoke:
        generator_version = "review-revoke"

        async def decide(self, view):
            await env["sources"].set_binding_enabled("proj-a", 1, False)
            return ProjectAnswerDecision.finish(
                (GroundedStatement(text="事实", evidence_refs=(view.evidence[0],)),),
                AnswerStatus.COMPLETE,
            )

    answer = await make_loop(env, generator=Revoke()).run(
        "proj-a",
        "alice",
        "r1-during",
        "项目目标是什么？",
    )
    assert answer.stop_reason is StopReason.ACCESS_DENIED
    assert not answer.statements


async def test_r1_cached_tools_recheck_binding(env):
    tools = await make_tools(env)
    assert (await tools.call("search_project", {"query": "项目目标"})).ok
    await env["sources"].set_binding_enabled("proj-a", 1, False)
    result = await tools.call("search_project", {"query": "项目目标"})
    assert not result.ok
    assert not result.items


async def test_r1_tools_recheck_after_execution(env, monkeypatch):
    tools = await make_tools(env)
    dispatch = tools._dispatch

    async def revoked_dispatch(tool, args):
        result = await dispatch(tool, args)
        await env["sources"].set_binding_enabled("proj-a", 1, False)
        return result

    monkeypatch.setattr(tools, "_dispatch", revoked_dispatch)
    result = await tools.call("search_project", {"query": "项目目标"})
    assert not result.ok
    assert not result.items


@pytest.mark.parametrize(
    "forgery",
    [
        {"source_id": "src-not-real"},
        {"paragraph": "L999999"},
        {"snippet": "REVIEW_FABRICATED_QUOTE_NOT_IN_ANY_SOURCE"},
        {"line_start": 999999},
    ],
)
async def test_r2_known_revision_does_not_license_forged_quote(env, forgery):
    class Forged:
        generator_version = "review-forged"

        async def decide(self, view):
            return ProjectAnswerDecision.finish(
                (
                    GroundedStatement(
                        text="伪造事实",
                        evidence_refs=(view.evidence[0].model_copy(update=forgery),),
                    ),
                ),
                AnswerStatus.COMPLETE,
            )

        async def repair(self, view, rejected, reason):
            return await self.decide(view)

    answer = await make_loop(env, generator=Forged()).run(
        "proj-a",
        "alice",
        "r2-forged",
        "项目目标是什么？",
    )
    assert answer.stop_reason is StopReason.MODEL_FAILED
    assert all(s.text != "伪造事实" for s in answer.statements)


async def test_r3_sync_rebuilds_without_manual_derivation(env):
    from orgpilot.github.sync import GitHubSyncService
    from orgpilot.storage.project_source_store import SqlSyncInboxStore

    env["snapshot"]["blobs"]["b-readme"] = "# 项目目标\n新项目目标：验证自动发布。\n"
    sync = GitHubSyncService(
        db=env["db"],
        sources=env["sources"],
        inbox_store=SqlSyncInboxStore(env["db"], env["clock"]),
        runs=env["runs"],
        knowledge=env["knowledge"],
        clock=env["clock"],
        client_factory=lambda b: env["client"],
    )
    await sync.ingest_webhook("1001", "r3-push", "push", None, {"ref": "refs/heads/main"})
    await sync.drain_repository("1001")
    old = await env["knowledge"].get_generation(env["generation"].generation_id)
    current_id = await env["knowledge"].current_generation_id("proj-a")
    current = await env["knowledge"].get_generation(current_id)
    assert not old.valid
    assert current.valid and current.generation_id != old.generation_id
    answer = await make_loop(env).run("proj-a", "alice", "r3-question", "项目目标是什么？")
    assert answer.stop_reason is StopReason.ANSWERED
    assert all(
        ref.revision_id in current.revision_manifest
        for statement in answer.statements
        for ref in statement.evidence_refs
    )
    assert "自动发布" in answer.rendered_text


async def test_r3_generation_change_during_finish_stops_session(env):
    from orgpilot.knowledge.derive import DerivationPipeline

    class Change:
        generator_version = "review-change"

        async def decide(self, view):
            await DerivationPipeline(
                env["sources"], env["knowledge"], env["clock"]
            ).build_and_publish(
                env["binding"],
                base_generation=env["generation"],
            )
            return ProjectAnswerDecision.finish(
                (GroundedStatement(text="旧事实", evidence_refs=(view.evidence[0],)),),
                AnswerStatus.COMPLETE,
            )

    answer = await make_loop(env, generator=Change()).run(
        "proj-a",
        "alice",
        "r3-mid",
        "项目目标是什么？",
    )
    assert answer.stop_reason is StopReason.CONTEXT_CHANGED
    assert not answer.statements


async def test_r3_formal_state_change_during_finish_stops_session(env):
    from orgpilot.domain.models import OrgState

    state = OrgState(project_id="proj-a")

    class Change:
        generator_version = "review-state-change"

        async def decide(self, view):
            state.last_event_id = "event-new"
            return ProjectAnswerDecision.finish(
                (GroundedStatement(text="旧事实", evidence_refs=(view.evidence[0],)),),
                AnswerStatus.COMPLETE,
            )

    answer = await make_loop(env, generator=Change(), org_state_provider=lambda p: state).run(
        "proj-a",
        "alice",
        "r3-state",
        "项目目标是什么？",
    )
    assert answer.stop_reason is StopReason.CONTEXT_CHANGED
    assert not answer.statements

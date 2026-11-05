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


async def test_r7_repair_cannot_add_a_request_after_cap(env):
    from orgpilot.knowledge.models import BudgetLimits

    class Repair:
        generator_version = "review-repair-cap"
        calls = 0

        async def decide(self, view):
            self.calls += 1
            ref = view.evidence[0].model_copy(update={"revision_id": "rev-fake"})
            return ProjectAnswerDecision.finish(
                (GroundedStatement(text="invalid", evidence_refs=(ref,)),),
                AnswerStatus.COMPLETE,
            )

        async def repair(self, view, rejected, reason):
            self.calls += 1
            return ProjectAnswerDecision.report_gap("should not run")

    generator = Repair()
    answer = await make_loop(
        env, generator=generator, limits=BudgetLimits(max_model_requests=1)
    ).run(
        "proj-a",
        "alice",
        "r7-cap",
        "项目目标是什么？",
    )
    assert generator.calls == 1
    assert answer.budget_usage.model_requests == 1
    assert answer.stop_reason is not StopReason.ANSWERED


async def test_r7_repair_obeys_single_call_and_total_deadline(env):
    import asyncio

    from orgpilot.knowledge.models import BudgetLimits

    class SlowRepair:
        generator_version = "review-slow-repair"
        completed = False

        async def decide(self, view):
            ref = view.evidence[0].model_copy(update={"revision_id": "rev-fake"})
            return ProjectAnswerDecision.finish(
                (GroundedStatement(text="invalid", evidence_refs=(ref,)),),
                AnswerStatus.COMPLETE,
            )

        async def repair(self, view, rejected, reason):
            await asyncio.sleep(1)
            self.completed = True
            return ProjectAnswerDecision.report_gap("late")

    generator = SlowRepair()
    answer = await make_loop(
        env, generator=generator, limits=BudgetLimits(max_model_seconds=0.02)
    ).run(
        "proj-a",
        "alice",
        "r7-timeout",
        "项目目标是什么？",
    )
    assert not generator.completed
    assert answer.stop_reason is StopReason.MODEL_FAILED
    assert answer.budget_usage.model_requests == 2
    assert answer.budget_usage.structured_repairs == 1
    assert answer.budget_usage.unknown_token_usage


async def test_r8_unknown_tokens_are_preserved_after_finish_and_replay(env):
    class Unknown:
        generator_version = "review-unknown"

        async def decide(self, view):
            raise TimeoutError("unknown")

    loop = make_loop(env, generator=Unknown())
    answer = await loop.run("proj-a", "alice", "r8-unknown", "项目目标是什么？")
    replay = await loop.run("proj-a", "alice", "r8-unknown", "项目目标是什么？")
    session = await env["read_sessions"].get_by_message("proj-a", "r8-unknown")
    assert answer.budget_usage.total_tokens() > 0
    assert session.used_budget.total_tokens() == answer.budget_usage.total_tokens()
    assert replay.budget_usage == answer.budget_usage
    assert answer.budget_usage.unknown_token_usage


async def test_r8_uses_full_output_usage_not_action_summary(env):
    from orgpilot.knowledge.models import BudgetUsage

    class Large:
        generator_version = "review-large"

        async def decide(self, view):
            decision = ProjectAnswerDecision.finish(
                (GroundedStatement(text="A" * 3500, evidence_refs=(view.evidence[0],)),),
                AnswerStatus.COMPLETE,
                summary="finish",
            )
            decision.usage = BudgetUsage(model_requests=1, input_tokens=300, output_tokens=1000)
            return decision

    answer = await make_loop(env, generator=Large()).run(
        "proj-a",
        "alice",
        "r8-full-output",
        "项目目标是什么？",
    )
    assert answer.budget_usage.output_tokens == 1000


async def test_r8_strict_initial_evidence_limits(env):
    from orgpilot.knowledge.models import BudgetLimits

    answer = await make_loop(
        env,
        limits=BudgetLimits(
            max_evidence_fragments=1,
            max_evidence_chars=12,
        ),
    ).run("proj-a", "alice", "r8-fragments", "联调术语交付目标")
    assert answer.budget_usage.evidence_fragments <= 1
    assert answer.budget_usage.evidence_chars <= 12


async def test_r1_replay_rechecks_new_sharing_list(env):
    loop = make_loop(env)
    first = await loop.run("proj-a", "alice", "r1-grant-change", "项目目标是什么？")
    await env["sources"].create_binding(
        env["binding"].model_copy(
            update={
                "binding_version": 2,
                "access_epoch": 2,
                "sharing_grants": (),
            }
        )
    )
    replay = await loop.run("proj-a", "alice", "r1-grant-change", "项目目标是什么？")
    assert replay.stop_reason is StopReason.ACCESS_DENIED
    assert not replay.statements
    assert replay.budget_usage == first.budget_usage


async def test_r1_concurrent_redelivery_executes_only_one_session(env):
    import asyncio

    class Once:
        generator_version = "review-once"
        calls = 0

        async def decide(self, view):
            self.calls += 1
            await asyncio.sleep(0)
            return ProjectAnswerDecision.finish(
                (GroundedStatement(text="事实", evidence_refs=(view.evidence[0],)),),
                AnswerStatus.COMPLETE,
            )

    generator = Once()
    loop = make_loop(env, generator=generator)
    one, two = await asyncio.gather(
        *[loop.run("proj-a", "alice", "r1-concurrent", "项目目标是什么？") for _ in range(2)]
    )
    assert generator.calls == 1
    assert one.read_session_id == two.read_session_id
    assert one.budget_usage == two.budget_usage


async def test_r3_partial_sync_keeps_honest_generation_coverage(env):
    from orgpilot.github.sync import GitHubSyncService
    from orgpilot.knowledge.importer import ImportLimits
    from orgpilot.storage.project_source_store import SqlSyncInboxStore

    sync = GitHubSyncService(
        env["db"],
        env["sources"],
        SqlSyncInboxStore(env["db"]),
        env["runs"],
        env["knowledge"],
        clock=env["clock"],
        client_factory=lambda b: env["client"],
        import_limits=ImportLimits(max_documents=1),
    )
    report = await sync.manual_recheck("proj-a")
    assert not report.fully_complete
    current = await env["knowledge"].get_generation(
        await env["knowledge"].current_generation_id("proj-a")
    )
    assert current.coverage["document"] == "partial"


async def test_r3_publish_rechecks_binding_in_pointer_transaction(env, monkeypatch):
    from orgpilot.knowledge.derive import DerivationAborted, DerivationPipeline

    publish = env["knowledge"].publish_generation
    candidate = []

    async def revoke_then_publish(generation_id, **kwargs):
        candidate.append(generation_id)
        await env["sources"].set_binding_enabled("proj-a", 1, False)
        await publish(generation_id, **kwargs)

    monkeypatch.setattr(env["knowledge"], "publish_generation", revoke_then_publish)
    with pytest.raises(DerivationAborted):
        await DerivationPipeline(env["sources"], env["knowledge"], env["clock"]).build_and_publish(
            env["binding"],
            base_generation=env["generation"],
        )
    assert (await env["knowledge"].get_generation(candidate[0])).status.value == "failed"
    assert await env["knowledge"].current_generation_id("proj-a") == env["generation"].generation_id


async def test_r7_r8_cancel_preserves_reservation_and_never_resends(env):
    import asyncio

    class Block:
        generator_version = "review-cancel"
        calls = 0
        entered = asyncio.Event()

        async def decide(self, view):
            self.calls += 1
            self.entered.set()
            await asyncio.Event().wait()

    generator = Block()
    loop = make_loop(env, generator=generator)
    task = asyncio.create_task(loop.run("proj-a", "alice", "r8-cancel", "项目目标是什么？"))
    await asyncio.wait_for(generator.entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session = await env["read_sessions"].get_by_message("proj-a", "r8-cancel")
    assert session.status.value == "interrupted"
    assert session.reserved_budget
    replay = await loop.run("proj-a", "alice", "r8-cancel", "项目目标是什么？")
    assert replay.stop_reason is StopReason.INTERRUPTED
    assert replay.budget_usage.total_tokens() > 0
    assert replay.budget_usage.unknown_token_usage
    assert generator.calls == 1

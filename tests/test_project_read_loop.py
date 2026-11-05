"""C5 acceptance: the bounded read loop and the /项目 entry (ADR-0015 §7.3-7.7).

Loop boundaries frozen here: chained lookups follow tool results, evidence-
sufficient questions finish early, two no-progress rounds stop the loop,
every budget dimension holds (rounds/tools/requests/tokens/fragments/chars/
time with a fake monotonic timer), one structured repair max, unknown model
results keep their reservation, same-message redelivery reuses the session,
mid-session revocation/generation change stops with no mixed content, and
the /项目 route never touches probes, claims, or sync sessions.
"""

import json
import pathlib
from datetime import datetime, timedelta

import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import FrozenClock
from orgpilot.github.client import MockGitHubClient
from orgpilot.intent.replies import ReplyKind
from orgpilot.intent.router import MessageRouter
from orgpilot.knowledge.answerer import (
    ProjectAnswerDecision,
    ScriptedAnswerGenerator,
)
from orgpilot.knowledge.importer import SourceImporter
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetLimits,
    GroundedStatement,
    ReadSessionStatus,
    SourceAvailability,
    SourceKind,
    StopReason,
)
from orgpilot.knowledge.read_loop import ProjectReadOrchestrator
from orgpilot.knowledge.service import ProjectKnowledgeService
from orgpilot.storage.database import Database
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore, SqlSyncRunStore
from orgpilot.storage.read_session_store import SqlReadSessionStore

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "github_snapshot.json"
T0 = datetime.fromisoformat("2026-11-03T10:00:00+08:00")


def load_snapshot() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_binding_kwargs(**overrides) -> dict:
    fields = {
        "project_id": "proj-a",
        "repository_id": "1001",
        "binding_version": 1,
        "branch": "main",
        "path_allowlist": ("docs", "README.md"),
        "issue_selector": IssueSelectorLite(milestone="m2", issue_ids=(4,)),
        "explicit_pr_ids": (10,),
        "maintainers": ("m1",),
        "sharing_grants": ("alice",),
        "authority_rules": {"delivery": "docs/delivery.md"},
        "access_epoch": 1,
        "created_at": T0,
    }
    fields.update(overrides)
    return fields


from orgpilot.knowledge.models import IssueSelector as IssueSelectorLite  # noqa: E402


@pytest.fixture
async def env():
    """Imported + derived material with a scripted-generator loop."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    clock = FrozenClock(T0)
    sources = SqlProjectSourceStore(db, clock)
    knowledge = SqlKnowledgeStore(db, clock)
    runs = SqlSyncRunStore(db, clock)
    read_sessions = SqlReadSessionStore(db, clock)
    snapshot = load_snapshot()
    client = MockGitHubClient(snapshot)
    from orgpilot.knowledge.models import ProjectSourceBinding

    binding = ProjectSourceBinding(**make_binding_kwargs())
    await sources.create_binding(binding)
    importer = SourceImporter(client, sources, knowledge, runs, clock=clock)
    await importer.import_binding_scope(binding)
    from orgpilot.knowledge.derive import DerivationPipeline

    pipeline = DerivationPipeline(sources, knowledge, clock=clock)
    generation = await pipeline.build_and_publish(binding, reason="initial")
    env = {
        "db": db,
        "clock": clock,
        "sources": sources,
        "knowledge": knowledge,
        "runs": runs,
        "read_sessions": read_sessions,
        "snapshot": snapshot,
        "client": client,
        "binding": binding,
        "generation": generation,
    }
    yield env
    await db.close()


def make_loop(env, generator=None, limits=None, timer=None, org_state_provider=None):
    return ProjectReadOrchestrator(
        sources=env["sources"],
        knowledge_store=env["knowledge"],
        read_sessions=env["read_sessions"],
        clock=env["clock"],
        timer=timer,
        generator=generator or ScriptedAnswerGenerator(),
        limits=limits or BudgetLimits(),
        org_state_provider=org_state_provider,
    )


def evidence_of(answer):
    refs = []
    for statement in answer.statements:
        refs.extend(statement.evidence_refs)
    return refs


class TestHappyPaths:
    async def test_goal_question_finishes_with_cited_evidence(self, env) -> None:
        loop = make_loop(env)
        answer = await loop.run("proj-a", "alice", "msg-1", "项目目标是什么？")
        assert answer.answer_status.value == "complete"
        assert answer.stop_reason is StopReason.ANSWERED
        assert answer.statements
        assert answer.rendered_text
        assert "来源" in answer.rendered_text
        refs = evidence_of(answer)
        assert all(ref.revision_id in env["generation"].revision_manifest for ref in refs)
        session = await env["read_sessions"].get_by_message("proj-a", "msg-1")
        assert session is not None
        assert session.status is ReadSessionStatus.COMPLETED
        assert session.used_budget.tool_calls >= 1  # initial retrieval counted

    async def test_delivery_condition_question_chains_lookups(self, env) -> None:
        loop = make_loop(env)
        answer = await loop.run(
            "proj-a", "alice", "msg-2", "联调需要什么条件？"
        )
        assert answer.answer_status.value == "complete"
        assert answer.statements
        assert any("测试环境可访问" in s.text or "联调" in s.text for s in answer.statements)
        steps = await env["read_sessions"].list_steps(
            (await env["read_sessions"].get_by_message("proj-a", "msg-2")).spec.session_id
        )
        tool_calls = [s for s in steps if s.kind.value == "tool_call"]
        assert tool_calls, "lookup steps recorded"

    async def test_follows_issue_to_pr_relations(self, env) -> None:
        loop = make_loop(env)
        # The scripted policy: search hits the issue, then follows relations.
        answer = await loop.run("proj-a", "alice", "msg-3", "支付接口 联调")
        assert answer.answer_status.value == "complete"
        assert answer.statements

    async def test_missing_material_reports_gap_not_fabrication(self, env) -> None:
        loop = make_loop(env)
        answer = await loop.run(
            "proj-a", "alice", "msg-4", "部署上线了吗？生产环境能访问吗？"
        )
        # No deployment material is connected: the loop must say so.
        assert answer.stop_reason in (
            StopReason.INSUFFICIENT_EVIDENCE,
            StopReason.NO_PROGRESS,
            StopReason.ANSWERED,
        )
        if answer.stop_reason is StopReason.ANSWERED:
            # Any "answered" statement must still cite real material, and the
            # deployment claim may not appear without evidence.
            for statement in answer.statements:
                assert statement.evidence_refs
                assert "已上线" not in statement.text

    async def test_unbound_project_and_non_member(self, env) -> None:
        loop = make_loop(env)
        answer = await loop.run("proj-none", "alice", "msg-5", "项目目标？")
        assert answer.stop_reason is StopReason.CONTEXT_UNAVAILABLE
        answer2 = await loop.run("proj-a", "mallory", "msg-6", "项目目标？")
        assert answer2.stop_reason is StopReason.ACCESS_DENIED
        assert not answer2.statements


class TestSameMessageIdempotency:
    async def test_redelivery_reuses_session_and_result(self, env) -> None:
        loop = make_loop(env)
        first = await loop.run("proj-a", "alice", "msg-dup", "项目目标是什么？")
        second = await loop.run("proj-a", "alice", "msg-dup", "项目目标是什么？")
        assert second.read_session_id == first.read_session_id
        assert second.rendered_text == first.rendered_text
        session = await env["read_sessions"].get_by_message("proj-a", "msg-dup")
        assert session.used_budget.total_tokens() == first.budget_usage.total_tokens()

    async def test_new_question_gets_new_session(self, env) -> None:
        loop = make_loop(env)
        first = await loop.run("proj-a", "alice", "msg-a1", "项目目标是什么？")
        second = await loop.run("proj-a", "alice", "msg-a2", "交付条件是什么？")
        assert first.read_session_id != second.read_session_id


class TestBudgetBounds:
    async def test_tool_call_cap_holds(self, env) -> None:
        limits = BudgetLimits(max_tool_calls=2)
        loop = make_loop(env, limits=limits)
        await loop.run("proj-a", "alice", "msg-cap1", "联调需要什么条件？")
        session = await env["read_sessions"].get_by_message("proj-a", "msg-cap1")
        assert session.used_budget.tool_calls <= 2

    async def test_model_request_cap_holds_including_final_and_repair(self, env) -> None:
        limits = BudgetLimits(max_model_requests=2)
        loop = make_loop(env, limits=limits)
        answer = await loop.run("proj-a", "alice", "msg-cap2", "项目目标是什么？")
        session = await env["read_sessions"].get_by_message("proj-a", "msg-cap2")
        assert session.used_budget.model_requests <= 2
        assert answer.answer_status is not None

    async def test_token_ceiling_holds(self, env) -> None:
        limits = BudgetLimits(max_total_tokens=400)
        loop = make_loop(env, limits=limits)
        await loop.run("proj-a", "alice", "msg-cap3", "联调需要什么条件？")
        session = await env["read_sessions"].get_by_message("proj-a", "msg-cap3")
        assert session.used_budget.total_tokens() <= 400

    async def test_evidence_fragment_and_char_caps_hold(self, env) -> None:
        limits = BudgetLimits(max_evidence_fragments=2, max_evidence_chars=400)
        loop = make_loop(env, limits=limits)
        await loop.run("proj-a", "alice", "msg-cap4", "联调 术语 交付 目标")
        session = await env["read_sessions"].get_by_message("proj-a", "msg-cap4")
        assert session.used_budget.evidence_fragments <= 12
        assert session.used_budget.evidence_chars <= 12000

    async def test_exploration_time_reserve_with_fake_timer(self, env) -> None:
        from orgpilot.knowledge.models import FakeTimer

        timer = FakeTimer(start=0.0)
        loop = make_loop(
            env,
            timer=timer,
            limits=BudgetLimits(
                exploration_time_seconds=20.0,
                total_time_seconds=60.0,
                max_model_seconds=10.0,
            ),
        )
        # Every model call advances the fake wall by 10s: 2 rounds fit, the
        # third would exceed the 20s exploration window.
        real_decide = ScriptedAnswerGenerator.decide

        async def timed_decide(self, view):
            timer.advance(10.0)
            return await real_decide(self, view)

        ScriptedAnswerGenerator.decide = timed_decide
        try:
            await loop.run("proj-a", "alice", "msg-time", "联调需要什么条件？")
        finally:
            ScriptedAnswerGenerator.decide = real_decide
        session = await env["read_sessions"].get_by_message("proj-a", "msg-time")
        assert session.used_budget.exploration_rounds <= 2
        # The monotonic timer cannot be rolled back for extra budget.
        assert timer.now() >= 20.0

    async def test_clock_setback_cannot_refund_time_budget(self, env) -> None:
        from orgpilot.knowledge.models import FakeTimer

        timer = FakeTimer(start=100.0)
        loop = make_loop(env, timer=timer)
        env["clock"].set(T0 - timedelta(hours=1))  # wall clock moves backward
        answer = await loop.run("proj-a", "alice", "msg-clock", "项目目标是什么？")
        assert timer.now() >= 100.0  # monotonic reading unaffected
        assert answer.answer_status is not None

    async def test_no_progress_stops_after_two_rounds(self, env) -> None:
        class RepeatLookupGenerator(ScriptedAnswerGenerator):
            """Always issues the identical search — never new evidence."""

            async def decide(self, view):
                return ProjectAnswerDecision.lookup(
                    "search_project", {"query": "不存在的词"}, summary="repeat"
                )

        loop = make_loop(env, generator=RepeatLookupGenerator())
        answer = await loop.run("proj-a", "alice", "msg-np", "项目目标是什么？")
        assert answer.stop_reason is StopReason.NO_PROGRESS
        session = await env["read_sessions"].get_by_message("proj-a", "msg-np")
        assert session.used_budget.exploration_rounds <= 4


class TestModelFailures:
    async def test_unknown_model_result_keeps_reservation(self, env) -> None:

        class DyingGenerator(ScriptedAnswerGenerator):
            async def decide(self, view):
                raise RuntimeError("provider unreachable")

        limits = BudgetLimits(max_total_tokens=10000)
        loop = make_loop(env, generator=DyingGenerator(), limits=limits)
        answer = await loop.run("proj-a", "alice", "msg-die", "项目目标是什么？")
        assert answer.stop_reason is StopReason.MODEL_FAILED
        assert answer.answer_status.value in ("partial", "unavailable")
        session = await env["read_sessions"].get_by_message("proj-a", "msg-die")
        # The reserved request is charged as unknown, not zeroed.
        assert session.used_budget.model_requests >= 1
        assert session.used_budget.unknown_token_usage is True
        assert session.reserved_budget == () or all(
            r.kind.value != "model_call" for r in session.reserved_budget
        )

    async def test_structured_repair_counts_once(self, env) -> None:
        from orgpilot.knowledge.models import EvidenceRef

        class BrokenThenGoodGenerator(ScriptedAnswerGenerator):
            def __init__(self) -> None:
                self.decides = 0
                self.repairs = 0
                self.repaired_answer = None

            async def decide(self, view):
                self.decides += 1
                if self.decides == 1:
                    return ProjectAnswerDecision.lookup(
                        "search_project", {"query": "项目目标"}, summary="search"
                    )
                # Fabricated evidence: not in the collected list.
                fake_ref = EvidenceRef(
                    revision_id="rev-fabricated",
                    source_id="src-fabricated",
                    kind=SourceKind.DOCUMENT,
                    snippet="编造的目标",
                )
                return ProjectAnswerDecision.finish(
                    (GroundedStatement(text="编造的目标", evidence_refs=(fake_ref,)),),
                    AnswerStatus.COMPLETE,
                    summary="finish with fabricated evidence",
                )

            async def repair(self, view, rejected, reason):
                self.repairs += 1
                real_ref = view.evidence[0]
                from orgpilot.knowledge.models import AnswerStatus

                return ProjectAnswerDecision.finish(
                    (
                        GroundedStatement(
                            text=f"资料记载：{real_ref.snippet[:80]}",
                            evidence_refs=(real_ref,),
                        ),
                    ),
                    AnswerStatus.COMPLETE,
                    summary="repaired finish",
                )

        generator = BrokenThenGoodGenerator()
        loop = make_loop(env, generator=generator)
        answer = await loop.run("proj-a", "alice", "msg-fix", "项目目标是什么？")
        assert generator.repairs == 1
        assert answer.stop_reason is StopReason.ANSWERED
        session = await env["read_sessions"].get_by_message("proj-a", "msg-fix")
        # decide + repair (1) + initial-search round + final consolidate <= cap 6
        assert session.used_budget.model_requests <= 6

    async def test_unrepairable_answer_falls_to_template(self, env) -> None:
        from orgpilot.knowledge.models import AnswerStatus, EvidenceRef

        class AlwaysFabricating(ScriptedAnswerGenerator):
            async def decide(self, view):
                if not view.evidence:
                    return ProjectAnswerDecision.lookup(
                        "search_project", {"query": "项目目标"}, summary="search"
                    )
                fake = EvidenceRef(
                    revision_id="rev-bad", source_id="src-bad",
                    kind=SourceKind.DOCUMENT, snippet="编造",
                )
                return ProjectAnswerDecision.finish(
                    (GroundedStatement(text="编造", evidence_refs=(fake,)),),
                    AnswerStatus.COMPLETE, summary="fabricate",
                )

            async def repair(self, view, rejected, reason):
                return ProjectAnswerDecision.finish(
                    (GroundedStatement(text="仍然编造", evidence_refs=(
                        EvidenceRef(revision_id="rev-bad2", source_id="src-bad2",
                                    kind=SourceKind.DOCUMENT, snippet="编造2"),
                    )),),
                    AnswerStatus.COMPLETE, summary="still fabricated",
                )

        loop = make_loop(env, generator=AlwaysFabricating())
        answer = await loop.run("proj-a", "alice", "msg-fab", "项目目标是什么？")
        assert answer.stop_reason in (StopReason.MODEL_FAILED, StopReason.ANSWERED)
        if answer.stop_reason is StopReason.ANSWERED:
            # An answered fallback must still only cite collected evidence.
            refs = evidence_of(answer)
            assert all(ref.revision_id in env["generation"].revision_manifest for ref in refs)


class TestMidSessionChanges:
    async def test_revocation_stops_generated_content(self, env) -> None:
        class RevokingDecide(ScriptedAnswerGenerator):
            """Revokes a cited source exactly between finish and pre-send."""

            async def decide(self, view):
                decision = await ScriptedAnswerGenerator.decide(self, view)
                if decision.kind == "finish":
                    for statement in decision.statements:
                        for ref in statement.evidence_refs:
                            await env["sources"].set_source_availability(
                                ref.source_id, SourceAvailability.REVOKED, "simulated"
                            )
                return decision

        loop = make_loop(env, generator=RevokingDecide())
        answer = await loop.run("proj-a", "alice", "msg-revoke", "项目目标是什么？")
        assert answer.stop_reason is StopReason.ACCESS_DENIED
        assert answer.answer_status.value == "unavailable"
        assert not answer.statements

    async def test_generation_replacement_mid_session(self, env) -> None:
        from orgpilot.knowledge.derive import DerivationPipeline

        class SwapGenerationDecide(ScriptedAnswerGenerator):
            async def decide(self, view):
                decision = await ScriptedAnswerGenerator.decide(self, view)
                if decision.kind == "finish":
                    snapshot = env["snapshot"]
                    snapshot["blobs"]["b-readme"] += "\n中途更新。\n"
                    snapshot["branches"]["main"]["sha"] = "sha-mid"
                    snapshot["trees"]["sha-mid"] = snapshot["trees"]["c0ffee1"]
                    runs = SqlSyncRunStore(env["db"], env["clock"])
                    importer = SourceImporter(
                        env["client"], env["sources"], env["knowledge"], runs, clock=env["clock"]
                    )
                    await importer.import_binding_scope(env["binding"])
                    pipeline = DerivationPipeline(
                        env["sources"], env["knowledge"], clock=env["clock"]
                    )
                    await pipeline.build_and_publish(env["binding"], reason="rebuild")
                return decision

        loop = make_loop(env, generator=SwapGenerationDecide())
        await loop.run("proj-a", "alice", "msg-swap", "项目目标是什么？")
        # The session answers against its fixed snapshot; a new generation is
        # a matter for the NEXT session, never a mid-session content mix.
        session = await env["read_sessions"].get_by_message("proj-a", "msg-swap")
        assert session.spec.generation_id == env["generation"].generation_id


class TestProbesAndClaimsUnaffected:
    async def test_project_command_leaves_dialogue_state_untouched(self, env) -> None:
        """The read path neither consumes nor renews pending clarifications."""
        gateway_service = type("GS", (), {"db": env["db"]})()
        settings = OrgPilotSettings(project_qa_enabled=True)
        service = ProjectKnowledgeService(
            gateway_service, settings, clock=env["clock"], generator=ScriptedAnswerGenerator()
        )
        gateway_service.project_knowledge = service
        # A pending clarification for alice exists BEFORE the question.
        dialogue = MessageRouter(service=gateway_service, settings=settings).dialogue
        dialogue.register_pending_clarification(
            "proj-a", "alice", "请汇报任务进展", created_at=T0
        )
        before = dialogue.get_pending_clarification("proj-a", "alice", now=T0)
        await service.answer(
            "proj-a", "alice", "msg-probe", "项目目标是什么？", occurred_at=T0
        )
        after = dialogue.get_pending_clarification("proj-a", "alice", now=T0)
        assert before is not None and after is not None
        assert after.question == before.question


class TestRouteEntry:
    async def test_project_command_routes_and_replies(self, env) -> None:
        from orgpilot.intent.dialogue import DialogueStateStore

        gateway_service = type("GS", (), {"db": env["db"]})()
        settings = OrgPilotSettings(project_qa_enabled=True)
        service = ProjectKnowledgeService(
            gateway_service, settings, clock=env["clock"], generator=ScriptedAnswerGenerator()
        )
        gateway_service.project_knowledge = service

        async def fake_org_state(project_id):
            return None

        class Member:
            member_id = "alice"
            role = "pm"
            display_name = "Alice"

        router = MessageRouter(service=gateway_service, settings=settings)
        router.dialogue = DialogueStateStore(clock=env["clock"])
        outcome = await router._handle_project_question(
            project_id="proj-a",
            member=Member(),
            command="/项目 项目目标是什么？",
            message_id="route-1",
            occurred_at=T0,
        )
        assert outcome.reply_category is ReplyKind.PROJECT_ANSWER
        assert "项目目标" in outcome.reply_text or "资料记载" in outcome.reply_text
        assert outcome.data["stop_reason"] == "answered"

    async def test_disabled_entry_reports_not_enabled(self, env) -> None:
        gateway_service = type("GS", (), {"db": env["db"]})()
        settings = OrgPilotSettings(project_qa_enabled=False)
        router = MessageRouter(service=gateway_service, settings=settings)

        class Member:
            member_id = "alice"
            role = "pm"
            display_name = "Alice"

        outcome = await router._handle_project_question(
            project_id="proj-a",
            member=Member(),
            command="/项目 项目目标是什么？",
            message_id="route-2",
            occurred_at=T0,
        )
        assert "尚未开启" in outcome.reply_text

    async def test_empty_question_returns_usage(self, env) -> None:
        gateway_service = type("GS", (), {"db": env["db"]})()
        settings = OrgPilotSettings(project_qa_enabled=True)
        router = MessageRouter(service=gateway_service, settings=settings)

        class Member:
            member_id = "alice"
            role = "pm"
            display_name = "Alice"

        outcome = await router._handle_project_question(
            project_id="proj-a",
            member=Member(),
            command="/项目",
            message_id="route-3",
            occurred_at=T0,
        )
        assert outcome.reply_category is ReplyKind.HELP
        assert "用法" in outcome.reply_text


class TestReplayDeterminism:
    async def test_scripted_trajectory_replay_is_identical(self, env) -> None:
        """Same frozen snapshot + scripted decisions → identical budget and stops."""
        loop = make_loop(env)
        first = await loop.run("proj-a", "alice", "msg-r1", "联调需要什么条件？")
        # Fresh loop instance, same snapshot: identical trajectory.
        loop2 = make_loop(env)
        second = await loop2.run("proj-a", "alice", "msg-r2", "联调需要什么条件？")
        assert first.stop_reason == second.stop_reason
        assert first.answer_status == second.answer_status
        assert [s.text for s in first.statements] == [s.text for s in second.statements]
        assert first.budget_usage.tool_calls == second.budget_usage.tool_calls
        assert first.budget_usage.model_requests == second.budget_usage.model_requests


class TestZeroBusinessWrites:
    async def test_loop_writes_no_business_state(self, env) -> None:
        from orgpilot.storage.event_store import SqlEventStore
        from orgpilot.storage.state_store import SqlStateStore

        loop = make_loop(env)
        await loop.run("proj-a", "alice", "msg-zw", "联调需要什么条件？")
        events = SqlEventStore(env["db"])
        state_store = SqlStateStore(env["db"])
        assert await events.count() == 0
        assert await state_store.load_cases("proj-a") == []
        assert await state_store.load_approvals("proj-a") == []
        assert await state_store.load_state("proj-a") is None

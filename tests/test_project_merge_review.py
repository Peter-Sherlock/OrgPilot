"""Final merge review: exercise real delivery and binding replacement boundaries."""

import asyncio
from types import SimpleNamespace

import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.models import MemberState, OrgState
from orgpilot.gateway.app import build_gateway_service
from orgpilot.github.sync import GitHubSyncService
from orgpilot.knowledge.answerer import ProjectAnswerDecision
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetLimits,
    GroundedStatement,
    IssueSelector,
)
from orgpilot.storage.project_source_store import SqlSyncInboxStore
from tests.test_project_read_loop import make_loop
from tests.test_project_review_regressions import material_fixture  # noqa: F401


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary_max_chars", 2001),
        ("total_time_seconds", 61),
        ("exploration_time_seconds", 46),
        ("finalize_reserve_seconds", 16),
        ("max_model_seconds", 11),
        ("max_tool_seconds", 3),
        ("max_no_progress_rounds", 3),
    ],
)
def test_review_r7_time_and_summary_limits_can_only_tighten(field, value):
    with pytest.raises(ValueError):
        BudgetLimits(**{field: value})


async def test_review_r7_tightened_brief_limit_is_applied(env):
    class Observe:
        generator_version = "review-brief"
        brief = None

        async def decide(self, view):
            self.brief = view.brief
            return ProjectAnswerDecision.finish(
                (
                    GroundedStatement(
                        text="事实",
                        evidence_refs=(view.evidence[0],),
                    ),
                ),
                AnswerStatus.COMPLETE,
            )

    generator = Observe()
    await make_loop(env, generator=generator, limits=BudgetLimits(summary_max_chars=12)).run(
        "proj-a",
        "alice",
        "review-summary",
        "项目目标是什么？",
    )
    assert generator.brief is not None and len(generator.brief) <= 12


async def test_review_r3_worker_imports_replacement_binding_without_manual_recheck(env):
    sync = GitHubSyncService(
        env["db"],
        env["sources"],
        SqlSyncInboxStore(env["db"]),
        env["runs"],
        env["knowledge"],
        clock=env["clock"],
        client_factory=lambda b: env["client"],
    )
    replacement = env["binding"].model_copy(
        update={
            "binding_version": 2,
            "access_epoch": 2,
            "path_allowlist": ("README.md",),
            "issue_selector": IssueSelector(),
            "explicit_pr_ids": (),
        }
    )
    await env["sources"].create_binding(replacement)
    await sync.start(poll_seconds=0.01)
    try:

        async def current_binding_ready():
            while True:
                current_id = await env["knowledge"].current_generation_id("proj-a")
                current = await env["knowledge"].get_generation(current_id)
                if current and current.valid and current.binding_version == 2:
                    return current
                await asyncio.sleep(0.01)

        current = await asyncio.wait_for(current_binding_ready(), timeout=5)
        assert current.access_epoch == 2
        for revision_id in current.revision_manifest:
            assert (await env["sources"].get_revision(revision_id)).binding_version == 2
    finally:
        await sync.stop()


async def test_review_r1_checkpoint_revocation_prevents_physical_reply_of_old_facts(
    env, monkeypatch
):
    router, sent = delivery_router(env, monkeypatch)

    async def checkpoint(project_id):
        await env["sources"].set_binding_enabled("proj-a", 1, False)

    monkeypatch.setattr(router, "checkpoint", checkpoint)
    result = await router.handle_message(
        project_id="proj-a",
        sender_id="alice",
        message_id="review-send-guard",
        text="/项目 项目目标是什么？",
        occurred_at=env["clock"].now(),
    )
    assert result.data["stop_reason"] == "access_denied"
    assert sent and "验证资料接入后的项目问答" not in sent[0]


def delivery_router(env, monkeypatch):
    settings = OrgPilotSettings(project_qa_enabled=True)
    service, _, _ = build_gateway_service(settings, env["db"], env["clock"])
    state = OrgState(
        project_id="proj-a",
        members={
            "alice": MemberState(
                member_id="alice",
                display_name="Alice",
                role="pm",
                source_event_id="member-1",
                last_update_at=env["clock"].now(),
            )
        },
    )

    async def agent(project_id):
        return SimpleNamespace(projector=SimpleNamespace(state=state))

    monkeypatch.setattr(service, "get_or_replay_agent", agent)
    router = service.intent_router
    sent = []

    async def checkpoint(project_id):
        pass

    class Messenger:
        async def send_text(self, recipient, text):
            sent.append(text)

    monkeypatch.setattr(router, "checkpoint", checkpoint)
    monkeypatch.setattr(router, "messenger_for", lambda project_id: Messenger())
    return router, sent


async def test_review_r1_recording_revocation_sanitizes_delivery_and_history(env, monkeypatch):
    router, sent = delivery_router(env, monkeypatch)
    record = router.messages.record_outcome

    async def revoke_after_record(**fields):
        await record(**fields)
        await env["sources"].set_binding_enabled("proj-a", 1, False)

    monkeypatch.setattr(router.messages, "record_outcome", revoke_after_record)
    result = await router.handle_message(
        project_id="proj-a",
        sender_id="alice",
        message_id="review-record-guard",
        text="/项目 项目目标是什么？",
        occurred_at=env["clock"].now(),
    )
    assert result.data["stop_reason"] == "access_denied"
    assert sent and "验证资料接入后的项目问答" not in sent[0]
    stored = await router.messages.get_outcome("proj-a", "review-record-guard")
    assert stored.reply_text == sent[0]
    assert "验证资料接入后的项目问答" not in stored.reply_text


async def test_review_r1_outer_message_cache_revalidates_without_new_budget(env, monkeypatch):
    router, sent = delivery_router(env, monkeypatch)
    request = {
        "project_id": "proj-a",
        "sender_id": "alice",
        "message_id": "review-outer-cache",
        "text": "/项目 项目目标是什么？",
        "occurred_at": env["clock"].now(),
    }
    first = await router.handle_message(**request)
    assert first.data["stop_reason"] == "answered"
    assert "验证资料接入后的项目问答" in sent[0]
    valid_duplicate = await router.handle_message(**request)
    assert valid_duplicate.duplicate
    assert valid_duplicate.reply_text == first.reply_text
    for overrides in ({"sender_id": "bob"}, {"text": "/项目 另一个问题"}):
        denied = await router.handle_message(**{**request, **overrides})
        assert denied.data["stop_reason"] == "access_denied"
        assert "验证资料接入后的项目问答" not in denied.reply_text
    await env["sources"].set_binding_enabled("proj-a", 1, False)
    revoked = await router.handle_message(**request)
    assert revoked.data["stop_reason"] == "access_denied"
    assert "验证资料接入后的项目问答" not in revoked.reply_text
    assert revoked.data["budget_usage"] == first.data["budget_usage"]
    assert len(sent) == 1


async def test_review_r8_send_revalidation_never_restarts_missing_session(env):
    loop = make_loop(env)
    result = await loop.revalidate("proj-a", "alice", "missing-session", "项目目标是什么？")
    assert result.stop_reason.value == "context_unavailable"
    assert not result.statements
    assert await env["read_sessions"].get_by_message("proj-a", "missing-session") is None

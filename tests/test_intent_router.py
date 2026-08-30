"""Tests for the role-aware pre-extraction intent routing layer (M3)."""

from datetime import datetime

from orgpilot.domain.enums import MessageIntent
from orgpilot.extraction.client import MockLLMClient
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.intent import IntentRouter
from orgpilot.extraction.models import MessageContext

NOW = datetime.fromisoformat("2026-09-10T15:00:00+08:00")
TASKS = {"backend_api": "Backend API", "frontend_integration": "Frontend"}
MEMBERS = {"alice": "engineer", "bob": "engineer", "carol": "pm", "david": "qa"}


def _context(actor_id: str, history: tuple[str, ...] = ()) -> MessageContext:
    return MessageContext(
        project_id="proj-intent",
        actor_id=actor_id,
        occurred_at=NOW,
        known_tasks=TASKS,
        known_members=MEMBERS,
        conversation_history=history,
    )


def test_pm_directive_with_target_and_deadline_regression() -> None:
    """Regression for the live-test failure: PM directive must be recognized."""
    result = IntentRouter().route("告诉Alice，必须在明天上午12点之前完成", _context("carol"))
    assert result.intent is MessageIntent.DIRECTIVE
    assert result.authority_ok is True
    assert result.confidence >= 0.9
    assert "alice" in result.hints.mentioned_member_ids
    assert result.hints.raw_time_expr is not None
    assert "明天上午12点" in result.hints.raw_time_expr


def test_member_directive_has_lower_authority() -> None:
    result = IntentRouter().route("让Bob帮忙看看收银台的报错", _context("alice"))
    assert result.intent is MessageIntent.DIRECTIVE
    assert result.authority_ok is False
    assert result.confidence < 0.9


def test_mandate_without_verb_is_directive() -> None:
    result = IntentRouter().route("Alice必须明天上午12点之前完成支付接入", _context("carol"))
    assert result.intent is MessageIntent.DIRECTIVE
    assert "alice" in result.hints.mentioned_member_ids


def test_reassign_verb_requires_target_adjacency() -> None:
    """「提交方案」contains a bare 交; it must not hijack a directive into reassign."""
    result = IntentRouter().route("告诉Alice，明天上午10点前提交方案", _context("carol"))
    assert result.intent is MessageIntent.DIRECTIVE

    # Genuine reassignment phrasings still route to task_reassign.
    for message in ("把Backend API任务转给David", "把Frontend交给Bob", "任务改派给David负责"):
        reassigned = IntentRouter().route(message, _context("carol"))
        assert reassigned.intent is MessageIntent.TASK_REASSIGN, message


def test_self_instruction_is_not_directive() -> None:
    result = IntentRouter().route("我自己必须在明天之前完成", _context("carol"))
    assert result.intent is not MessageIntent.DIRECTIVE


def test_task_create_and_reassign_by_pm() -> None:
    router = IntentRouter()
    created = router.route("新增一个任务：网关压测脚本，由David负责", _context("carol"))
    assert created.intent is MessageIntent.TASK_CREATE
    assert created.authority_ok is True

    reassigned = router.route("把后端接口的联调交给bob负责", _context("carol"))
    assert reassigned.intent is MessageIntent.TASK_REASSIGN
    assert "bob" in reassigned.hints.mentioned_member_ids


def test_pm_deadline_change_vs_member_delay_split() -> None:
    """Same '延期' topic: PM schedule edit routes to deadline_change, a member's
    own delay report stays a health report."""
    router = IntentRouter()
    pm_edit = router.route("后端接口的截止时间改到下周五", _context("carol"))
    assert pm_edit.intent is MessageIntent.DEADLINE_CHANGE

    member_delay = router.route("收银台前端要延期到明天下午 6 点，组件报错", _context("bob"))
    assert member_delay.intent is MessageIntent.HEALTH_REPORT


def test_chit_chat_wins_over_question_when_no_work_context() -> None:
    result = IntentRouter().route("大家中午吃饭了吗？", _context("alice"))
    assert result.intent is MessageIntent.CHIT_CHAT


def test_question_intent_with_work_context() -> None:
    result = IntentRouter().route("网关压测什么时候可以开始？", _context("bob"))
    assert result.intent is MessageIntent.QUESTION


def test_report_markers_route_to_extraction() -> None:
    result = IntentRouter().route("收银台前端要延期到明天下午 6 点，组件报错", _context("bob"))
    assert result.intent is MessageIntent.HEALTH_REPORT


def test_uncertain_when_no_rule_fires() -> None:
    result = IntentRouter().route("这个方案我持保留意见，先这样吧", _context("bob"))
    assert result.intent is MessageIntent.UNCERTAIN


def test_short_circuit_skips_llm_call() -> None:
    """Zero-slot non-report intents (directive, chit_chat) must not spend an LLM
    extraction call; task_create deliberately reaches the LLM to extract its
    proposal slots (title/owner/deadline) in the same invocation."""
    client = MockLLMClient()
    extractor = ClaimExtractor(llm_client=client)
    for message, actor in [
        ("告诉Alice，必须在明天上午12点之前完成", "carol"),
        ("收到，辛苦了！", "alice"),
    ]:
        result, events = extractor.extract_from_message(message, _context(actor))
        assert events == []
        assert result.is_actionable is False
        assert result.intent is not MessageIntent.HEALTH_REPORT
    assert client.call_history == []

    proposal_result, events = extractor.extract_from_message(
        "新增一个任务：网关压测脚本，由David负责", _context("carol")
    )
    assert events == []
    assert proposal_result.is_actionable is False
    assert proposal_result.intent is MessageIntent.TASK_CREATE
    assert proposal_result.task_proposal is not None
    assert proposal_result.task_proposal.title == "网关压测脚本"
    assert len(client.call_history) == 1

    deadline_result, events = extractor.extract_from_message(
        "Backend API截止时间改到后天下午5点", _context("carol")
    )
    assert events == []
    assert deadline_result.intent is MessageIntent.DEADLINE_CHANGE
    assert deadline_result.task_proposal is not None
    assert deadline_result.task_proposal.operation == "deadline_change"
    assert deadline_result.task_proposal.task_ref == "Backend API"
    assert deadline_result.task_proposal.deadline_expr == "Backend API截止时间改到后天下午5点"
    assert len(client.call_history) == 2


def test_report_message_still_reaches_llm_extraction() -> None:
    client = MockLLMClient()
    extractor = ClaimExtractor(llm_client=client)
    result, events = extractor.extract_from_message(
        "收银台前端要延期到明天下午 6 点，组件报错", _context("bob")
    )
    assert len(client.call_history) == 1
    assert result.intent is MessageIntent.HEALTH_REPORT
    assert len(events) == 1
    assert events[0].event_type == "task.health_reported"


def test_verifier_preserves_intent_field() -> None:
    extractor = ClaimExtractor(llm_client=MockLLMClient())
    result, _ = extractor.extract_from_message(
        "Backend API 报错卡住了，排查需要到明天下午 5 点", _context("alice")
    )
    assert result.intent is MessageIntent.HEALTH_REPORT

"""Unit tests for SlotCompletenessEvaluator and multi-turn autonomous clarification."""

from datetime import UTC, datetime

from orgpilot.domain.enums import HealthStatus
from orgpilot.extraction.clarification import SlotCompletenessEvaluator
from orgpilot.extraction.models import ExtractedHealthClaim, ExtractionResult


def test_complete_on_track_evaluation() -> None:
    claim = ExtractedHealthClaim(
        task_id="task-1",
        health_status=HealthStatus.ON_TRACK,
        expected_completion=None,
        blocker=None,
        confidence=0.95,
        source_quote="已解决，按原计划推进",
    )
    result = ExtractionResult(
        is_actionable=True,
        claims=[claim],
        commitments=[],
        reasoning="Recovered",
    )
    is_complete, missing = SlotCompletenessEvaluator.evaluate_completeness(
        result, "已解决，按原计划推进"
    )
    assert is_complete is True
    assert missing == []


def test_incomplete_delayed_without_expected_time() -> None:
    claim = ExtractedHealthClaim(
        task_id="task-payment",
        health_status=HealthStatus.DELAYED,
        expected_completion=None,
        blocker="报错卡住了",
        confidence=0.90,
        source_quote="报错卡住了",
    )
    result = ExtractionResult(
        is_actionable=True,
        claims=[claim],
        commitments=[],
        reasoning="Delayed without time",
    )
    is_complete, missing = SlotCompletenessEvaluator.evaluate_completeness(result, "报错卡住了")
    assert is_complete is False
    assert "预计恢复或完成时间点" in missing

    question = SlotCompletenessEvaluator.generate_clarification_question(
        task_title="支付SDK接入",
        missing_slots=missing,
        raw_reply="报错卡住了",
    )
    assert "支付SDK接入" in question
    assert "什么时候" in question or "几点" in question


def test_complete_delayed_with_all_slots() -> None:
    claim = ExtractedHealthClaim(
        task_id="task-payment",
        health_status=HealthStatus.DELAYED,
        expected_completion=datetime.now(UTC),
        blocker="数据库死锁",
        confidence=0.95,
        source_quote="数据库死锁，排查到明天5点",
    )
    result = ExtractionResult(
        is_actionable=True,
        claims=[claim],
        commitments=[],
        reasoning="Complete delayed",
    )
    is_complete, missing = SlotCompletenessEvaluator.evaluate_completeness(
        result, "数据库死锁，排查到明天5点"
    )
    assert is_complete is True
    assert missing == []


def test_non_actionable_on_track_keyword_is_complete() -> None:
    result = ExtractionResult(is_actionable=False, claims=[], commitments=[], reasoning="chitchat")
    is_complete, missing = SlotCompletenessEvaluator.evaluate_completeness(result, "一切顺利")
    assert is_complete is True
    assert missing == []


def test_non_actionable_without_keyword_requires_followup() -> None:
    result = ExtractionResult(is_actionable=False, claims=[], commitments=[], reasoning="chitchat")
    is_complete, missing = SlotCompletenessEvaluator.evaluate_completeness(result, "在忙别的")
    assert is_complete is False
    assert missing == ["当前具体进展与预计完成时间"]


def test_delayed_without_blocker_asks_for_root_cause() -> None:
    claim = ExtractedHealthClaim(
        task_id="task-payment",
        health_status=HealthStatus.DELAYED,
        expected_completion=datetime.now(UTC),
        blocker=None,
        confidence=0.90,
        source_quote="要延到明天",
    )
    result = ExtractionResult(
        is_actionable=True,
        claims=[claim],
        commitments=[],
        reasoning="Delayed without blocker",
    )
    is_complete, missing = SlotCompletenessEvaluator.evaluate_completeness(result, "要延到明天")
    assert is_complete is False
    assert "具体阻塞根因与卡点" in missing

    question = SlotCompletenessEvaluator.generate_clarification_question(
        task_title="支付SDK接入",
        missing_slots=missing,
        raw_reply="要延到明天",
    )
    assert "卡点" in question


def test_missing_time_and_blocker_generates_combined_question() -> None:
    claim = ExtractedHealthClaim(
        task_id="task-payment",
        health_status=HealthStatus.DELAYED,
        expected_completion=None,
        blocker=None,
        confidence=0.90,
        source_quote="有困难",
    )
    result = ExtractionResult(
        is_actionable=True,
        claims=[claim],
        commitments=[],
        reasoning="Delayed without time and blocker",
    )
    _, missing = SlotCompletenessEvaluator.evaluate_completeness(result, "有困难")
    question = SlotCompletenessEvaluator.generate_clarification_question(
        task_title="支付SDK接入",
        missing_slots=missing,
        raw_reply="有困难",
    )
    assert "什么困难" in question
    assert "几点" in question


def test_unknown_slot_falls_back_to_default_question() -> None:
    question = SlotCompletenessEvaluator.generate_clarification_question(
        task_title="支付SDK接入",
        missing_slots=["未知槽位"],
        raw_reply="不清楚",
    )
    assert "预计完成时间或当前具体进展" in question

"""Tests for the intent output schema contracts (ADR-0008)."""

import pytest
from pydantic import ValidationError

from orgpilot.intent.models import (
    Certainty,
    IntentItem,
    IntentResult,
    IntentType,
    Polarity,
    SpeechAct,
)


def _item(**overrides) -> dict:
    data = {
        "intent": "report_status",
        "task_id": "pay_sdk",
        "speech_act": "statement",
        "polarity": "affirmative",
        "certainty": "certain",
        "health_status": "delayed",
        "time_expression": "明天下午5点",
        "expected_completion": "2026-10-14T17:00:00+08:00",
        "blocker": "第三方接口报错",
        "source_quote": "支付SDK报错",
        "confidence": 0.9,
    }
    data.update(overrides)
    return data


def test_intent_item_accepts_full_payload() -> None:
    item = IntentItem.model_validate(_item())
    assert item.intent is IntentType.REPORT_STATUS
    assert item.speech_act is SpeechAct.STATEMENT
    assert item.polarity is Polarity.AFFIRMATIVE
    assert item.certainty is Certainty.CERTAIN
    assert item.expected_completion is not None
    assert item.expected_completion.tzinfo is not None


def test_intent_item_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        IntentItem.model_validate(_item(sentiment="happy"))


def test_intent_item_requires_timezone_aware_completion() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        IntentItem.model_validate(_item(expected_completion="2026-10-14T17:00:00"))


def test_intent_item_rejects_unknown_health_status() -> None:
    with pytest.raises(ValidationError, match="health_status"):
        IntentItem.model_validate(_item(health_status="fine"))


def test_intent_result_supports_multiple_items_and_pending_flag() -> None:
    result = IntentResult.model_validate(
        {
            "items": [
                _item(task_id="pay_sdk"),
                _item(
                    intent="report_status",
                    task_id="checkout_page",
                    health_status="on_track",
                    time_expression=None,
                    expected_completion=None,
                    blocker=None,
                    source_quote="收银台正常",
                ),
            ],
            "answers_pending": True,
            "reasoning": "一句话覆盖两个任务",
        }
    )
    assert len(result.items) == 2
    assert result.answers_pending is True
    assert {item.task_id for item in result.items} == {"pay_sdk", "checkout_page"}
    summary = result.to_summary()
    assert summary["answers_pending"] is True
    assert len(summary["items"]) == 2


def test_intent_result_defaults_to_empty() -> None:
    result = IntentResult.model_validate({})
    assert result.items == []
    assert result.answers_pending is False
    assert result.reasoning == ""

"""Tests for the intent output schema contracts (ADR-0008)."""

import json
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from orgpilot.intent.llm_client import normalize_intent_payload
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


def test_health_status_schema_exposes_enum_and_coercion() -> None:
    """3b hardening: the JSON schema carries the health enum; same valid set."""
    schema = IntentResult.model_json_schema()
    dumped = json.dumps(schema, ensure_ascii=False)
    assert '"enum"' in dumped
    for value in ("unknown", "on_track", "at_risk", "delayed"):
        assert value in dumped
    item = IntentItem(**_item())
    assert item.health_status.value == "delayed"
    assert item.health_status == "delayed"  # StrEnum: string comparisons hold
    with pytest.raises(ValidationError):
        IntentItem(**_item(health_status="excellent"))


def test_schema_describes_fields_and_puts_reasoning_first() -> None:
    """Every field carries a Chinese value-rule description; reasoning leads."""
    schema = IntentResult.model_json_schema()
    properties = list(schema["properties"].keys())
    assert properties[0] == "reasoning"
    for field in ("reasoning", "items", "answers_pending"):
        assert schema["properties"][field].get("description"), field
    item_schema = schema["$defs"]["IntentItem"]
    item_properties = item_schema["properties"]
    assert item_schema.get("additionalProperties") is False
    completion = item_properties["expected_completion"]["description"]
    assert "ISO 8601" in completion and "时区" in completion
    quote = item_properties["source_quote"]["description"]
    assert "原文" in quote and "子串" in quote


def test_normalize_intent_payload_drops_unknown_fields_and_counts() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    payload = {
        "reasoning": "汇报风险",
        "mood": "negative",  # hallucinated top-level key
        "items": [
            {
                "intent": "report_status",
                "task_id": "pay_sdk",
                "speech_act": "statement",
                "polarity": "affirmative",
                "certainty": "hedged",
                "health_status": "at_risk",
                "source_quote": "估计要延期",
                "confidence": 0.7,
                "sentiment": "worried",  # hallucinated item key
            }
        ],
    }
    cleaned, counters = normalize_intent_payload(payload, tz)
    assert "mood" not in cleaned
    assert "sentiment" not in cleaned["items"][0]
    assert counters["unknown_fields_dropped"] == 2
    assert counters["naive_datetimes_fixed"] == 0
    # the cleaned payload validates as-is
    IntentResult.model_validate(cleaned)


def test_normalize_intent_payload_attaches_business_timezone() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    payload = {
        "items": [
            {
                "intent": "report_status",
                "speech_act": "statement",
                "polarity": "affirmative",
                "certainty": "certain",
                "source_quote": "明天下午5点能修好",
                "confidence": 0.9,
                "expected_completion": "2026-10-16T17:00:00",
            }
        ]
    }
    cleaned, counters = normalize_intent_payload(payload, tz)
    assert cleaned["items"][0]["expected_completion"] == "2026-10-16T17:00:00+08:00"
    assert counters["naive_datetimes_fixed"] == 1
    # already-aware datetimes pass through untouched
    aware = dict(payload)
    aware["items"] = [dict(payload["items"][0], expected_completion="2026-10-16T17:00:00+08:00")]
    _, counters = normalize_intent_payload(aware, tz)
    assert counters["naive_datetimes_fixed"] == 0


def test_normalize_intent_payload_leaves_garbage_for_pydantic() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    payload = {"items": [{"intent": "report_status", "confidence": "high"}]}
    cleaned, counters = normalize_intent_payload(payload, tz)
    assert counters == {"unknown_fields_dropped": 0, "naive_datetimes_fixed": 0}
    with pytest.raises(ValidationError):
        IntentResult.model_validate(cleaned)
    # a non-dict payload passes through untouched
    same, counters = normalize_intent_payload(["not", "a", "dict"], tz)
    assert same == ["not", "a", "dict"]
    assert counters["unknown_fields_dropped"] == 0

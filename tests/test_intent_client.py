"""Tests for the intent client abstraction (ADR-0008 layer 1)."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orgpilot.intent.client import (
    HeuristicIntentClient,
    IntentContext,
    ScriptedIntentClient,
    classify_with_timeout,
)
from orgpilot.intent.models import IntentResult, IntentType

BJ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=BJ)


def _context(message: str, **overrides: object) -> IntentContext:
    values: dict[str, object] = {
        "project_id": "proj",
        "message": message,
        "occurred_at": NOW,
        "sender_id": "ou_alice",
        "sender_role": "backend",
        "sender_display_name": "Alice",
    }
    values.update(overrides)
    return IntentContext(**values)  # type: ignore[arg-type]


def test_intent_context_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _context("hi", occurred_at=datetime(2026, 10, 13, 10, 0))


async def test_heuristic_maps_claim_commitment_and_chitchat() -> None:
    client = HeuristicIntentClient()
    context = _context(
        "支付SDK报错，明天下午5点能修好",
        known_tasks=[
            {
                "task_id": "backend_api",
                "title": "后端接口",
                "owner_id": "ou_alice",
                "deadline": None,
                "health_status": "unknown",
            }
        ],
    )
    result = await client.classify(context.message, context)
    assert isinstance(result, IntentResult)
    assert result.items[0].intent == IntentType.REPORT_STATUS
    assert result.items[0].task_id == "backend_api"
    assert result.items[0].health_status == "delayed"
    assert result.items[0].expected_completion == datetime(2026, 10, 14, 17, 0, tzinfo=BJ)

    commitment_context = _context(
        "我保证明天下午提 PR",
        known_tasks=[
            {
                "task_id": "backend_api",
                "title": "后端接口",
                "owner_id": "ou_alice",
                "deadline": None,
                "health_status": "unknown",
            }
        ],
    )
    commitment_result = await client.classify(commitment_context.message, commitment_context)
    assert commitment_result.items[0].intent == IntentType.MAKE_COMMITMENT

    casual = await client.classify("你好", _context("你好"))
    assert casual.items[0].intent == IntentType.CHITCHAT
    assert casual.answers_pending is False


async def test_heuristic_flags_answers_pending_with_open_question() -> None:
    client = HeuristicIntentClient()
    context = _context(
        "支付SDK报错，明天下午5点能修好",
        known_tasks=[
            {
                "task_id": "backend_api",
                "title": "后端接口",
                "owner_id": "ou_alice",
                "deadline": None,
                "health_status": "unknown",
            }
        ],
        pending_question={"question": "大概什么时候能恢复？", "task_id": "backend_api"},
    )
    result = await client.classify(context.message, context)
    assert result.answers_pending is True


async def test_scripted_client_returns_presets_and_records_calls() -> None:
    scripted_result = IntentResult(reasoning="preset")
    client = ScriptedIntentClient(responses={"你好": scripted_result})
    context = _context("你好")
    assert await client.classify("你好", context) is scripted_result
    assert client.calls == [("你好", context)]


async def test_scripted_client_simulates_failure_modes() -> None:
    raising = ScriptedIntentClient(responses={"坏": RuntimeError("boom")})
    with pytest.raises(RuntimeError, match="boom"):
        await raising.classify("坏", _context("坏"))

    invalid = ScriptedIntentClient(responses={"坏": {"not": "an intent result"}})
    outcome = await invalid.classify("坏", _context("坏"))
    assert not isinstance(outcome, IntentResult)

    unknown = ScriptedIntentClient()
    with pytest.raises(ValueError, match="no scripted"):
        await unknown.classify("没预设", _context("没预设"))


async def test_classify_with_timeout_enforces_budget() -> None:
    slow = ScriptedIntentClient(
        responses={"慢": IntentResult()}, delay_seconds=0.2
    )
    with pytest.raises(asyncio.TimeoutError):
        await classify_with_timeout(slow, "慢", _context("慢"), timeout_seconds=0.05)

    fast = ScriptedIntentClient(responses={"快": IntentResult(reasoning="ok")})
    result = await classify_with_timeout(fast, "快", _context("快"), timeout_seconds=1.0)
    assert result.reasoning == "ok"

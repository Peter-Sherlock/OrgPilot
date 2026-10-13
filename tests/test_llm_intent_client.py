"""Offline tests for LLMIntentClient (3b): everything runs on httpx MockTransport.

The live path is exercised only through mocked transports; replay mode reads
recordings from tmp directories. No test ever touches the network.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.intent.client import HeuristicIntentClient, IntentContext
from orgpilot.intent.llm_client import (
    FIXED_PROMPT_PREFIX,
    FORCED_TOOL_NAME,
    PROMPT_VERSION,
    LLMIntentClient,
    LLMIntentError,
    SpendLimitExceeded,
)
from orgpilot.intent.router import default_intent_client

BJ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=BJ)
VALID_INPUT = {
    "reasoning": "消息明确汇报了延期风险并给出恢复时间",
    "items": [
        {
            "intent": "report_status",
            "task_id": "search_api",
            "speech_act": "statement",
            "polarity": "affirmative",
            "certainty": "certain",
            "health_status": "delayed",
            "time_expression": "下周三",
            "expected_completion": "2026-10-28T18:00:00+08:00",
            "blocker": "上游接口限流",
            "source_quote": "搜索服务最近老是超时，估计要拖到下周三",
            "confidence": 0.9,
        }
    ],
    "answers_pending": False,
}


def _context(message: str = "搜索服务最近老是超时，估计要拖到下周三") -> IntentContext:
    return IntentContext(
        project_id="proj",
        message=message,
        occurred_at=NOW,
        sender_id="ou_alice",
        sender_role="backend",
        sender_display_name="Alice",
    )


def _tool_response(tool_input: object, model: str = "deepseek-flash") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": FORCED_TOOL_NAME, "input": tool_input}
            ],
            "stop_reason": "tool_use",
            "model": model,
            "usage": {
                "input_tokens": 900,
                "output_tokens": 120,
                "cache_read_input_tokens": 8000,
            },
        },
    )


def _text_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "model": "deepseek-flash",
            "usage": {"input_tokens": 800, "output_tokens": 200},
        },
    )


def _client(tmp_path: Path, handler, **overrides) -> LLMIntentClient:
    client = LLMIntentClient(
        api_key="test-key",
        base_url="https://api.deepseek.test",
        model="deepseek-flash",
        business_tz=BJ,
        recordings_dir=tmp_path / "recordings",
        transport=httpx.MockTransport(handler),
        **overrides,
    )
    return client


async def test_forced_tool_request_shape_and_cache_friendly_prompt(tmp_path: Path) -> None:
    captured: list[dict] = []
    client = _client(
        tmp_path,
        lambda request: _tool_response(
            (captured.append(json.loads(request.read().decode("utf-8"))), VALID_INPUT)[1]
        ),
    )
    result = await client.classify(_context().message, _context())
    assert result.items[0].health_status == "delayed"
    body = captured[0]
    assert body["model"] == "deepseek-flash"
    assert body["temperature"] == 0
    assert body["thinking"] == {"type": "disabled"}
    assert body["tool_choice"] == {"type": "tool", "name": FORCED_TOOL_NAME}
    assert body["system"] == FIXED_PROMPT_PREFIX
    dynamic = body["messages"][0]["content"]
    assert dynamic.startswith("当前时间")  # dynamic content lives in the message only
    assert "本条消息" in dynamic and "全部任务" in dynamic
    assert json.dumps(body["tools"][0]["input_schema"], ensure_ascii=False).count('"enum"') >= 4


async def test_metrics_and_recording_are_written(tmp_path: Path, caplog) -> None:
    client = _client(tmp_path, lambda request: _tool_response(VALID_INPUT))
    with caplog.at_level(logging.INFO, logger="orgpilot.intent.llm_client"):
        await client.classify(_context().message, _context())
    metric = client.metrics[-1]
    assert metric["prompt_version"] == PROMPT_VERSION
    assert metric["input_tokens"] == 900
    assert metric["cache_read_input_tokens"] == 8000
    assert metric["stop_reason"] == "tool_use"
    assert metric["retried"] is False and metric["degraded"] is False
    assert metric["validation_errors"] == []
    assert metric["cost_usd_estimate"] > 0
    # structured log carries no key material
    assert "intent_llm_call" in caplog.text
    assert "test-key" not in caplog.text
    recordings = list((tmp_path / "recordings").glob("*.json"))
    assert len(recordings) == 1
    record = json.loads(recordings[0].read_text(encoding="utf-8"))
    assert record["request_hash"] == recordings[0].stem
    assert record["response"]["stop_reason"] == "tool_use"


async def test_validation_failure_retries_once_with_error_summary(tmp_path: Path) -> None:
    captured: list[dict] = []
    bad_input = {
        "items": [
            {
                "intent": "report_status",
                "speech_act": "statement",
                "polarity": "affirmative",
                "certainty": "certain",
                "source_quote": "x",
                "confidence": 0.9,
                "health_status": "excellent",  # invalid enum -> drives the retry
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read().decode("utf-8")))
        return _tool_response(VALID_INPUT if len(captured) > 1 else bad_input)

    client = _client(tmp_path, handler)
    result = await client.classify(_context().message, _context())
    assert result.items[0].intent.value == "report_status"
    assert len(captured) == 2
    retry_note = captured[1]["messages"][0]["content"]
    assert "没有通过校验" in retry_note and "enum" in retry_note
    metric = client.metrics[-1]
    assert metric["retried"] is True
    assert any("health_status" in error["loc"] for error in metric["validation_errors"])


async def test_second_validation_failure_raises(tmp_path: Path) -> None:
    bad_input = {
        "items": [
            {
                "intent": "report_status",
                "speech_act": "statement",
                "polarity": "affirmative",
                "certainty": "certain",
                "source_quote": "x",
                "confidence": 5.0,  # out of range, invalid on every attempt
            }
        ]
    }
    client = _client(tmp_path, lambda request: _tool_response(bad_input))
    with pytest.raises(LLMIntentError, match="failed validation"):
        await client.classify(_context().message, _context())


async def test_missing_tool_use_degrades_to_schema_prompt(tmp_path: Path) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode("utf-8"))
        captured.append(payload)
        if payload.get("tool_choice"):
            return _text_response("抱歉，我不会调用工具。")
        return _text_response(f"```json\n{json.dumps(VALID_INPUT, ensure_ascii=False)}\n```")

    client = _client(tmp_path, handler)
    result = await client.classify(_context().message, _context())
    assert result.items[0].health_status == "delayed"
    fallback = captured[1]
    assert "tool_choice" not in fallback
    assert '"IntentItem"' in fallback["system"]  # schema embedded for the degraded call
    metric = client.metrics[-1]
    assert metric["degraded"] is True


async def test_replay_mode_reads_recordings_and_fails_on_miss(tmp_path: Path) -> None:
    live = _client(tmp_path, lambda request: _tool_response(VALID_INPUT))
    await live.classify(_context().message, _context())

    def _no_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError("replay mode must not send requests")

    replay = LLMIntentClient(
        api_key="test-key",
        base_url="https://api.deepseek.test",
        model="deepseek-flash",
        business_tz=BJ,
        mode="replay",
        recordings_dir=tmp_path / "recordings",
        transport=httpx.MockTransport(_no_http),
    )
    result = await replay.classify(_context().message, _context())
    assert result.items[0].health_status == "delayed"
    assert replay.metrics[-1]["mode"] == "replay"
    assert replay.metrics[-1]["cost_usd_estimate"] == 0

    empty = LLMIntentClient(
        api_key="test-key",
        base_url="https://api.deepseek.test",
        model="deepseek-flash",
        business_tz=BJ,
        mode="replay",
        recordings_dir=tmp_path / "missing",
        transport=httpx.MockTransport(_no_http),
    )
    with pytest.raises(FileNotFoundError, match="missing replay recording"):
        await empty.classify(_context().message, _context())


async def test_normalization_fixes_timezone_and_drops_unknown_fields(tmp_path: Path) -> None:
    sloppy_input = {
        "mood": "worried",  # unknown top-level field
        "items": [
            {
                "intent": "report_status",
                "task_id": "search_api",
                "speech_act": "statement",
                "polarity": "affirmative",
                "certainty": "certain",
                "health_status": "delayed",
                "blocker": "上游接口限流",
                "source_quote": "搜索服务最近老是超时",
                "confidence": 0.9,
                "expected_completion": "2026-10-28T18:00:00",  # naive
                "sentiment": "bad",  # unknown item field
            }
        ],
    }
    client = _client(tmp_path, lambda request: _tool_response(sloppy_input))
    result = await client.classify(_context().message, _context())
    item = result.items[0]
    assert item.expected_completion is not None
    assert item.expected_completion.utcoffset() is not None
    metric = client.metrics[-1]
    assert metric["normalization"] == {
        "unknown_fields_dropped": 2,
        "naive_datetimes_fixed": 1,
    }


async def test_spend_limit_aborts_mid_run(tmp_path: Path) -> None:
    client = _client(
        tmp_path, lambda request: _tool_response(VALID_INPUT), max_spend_usd=0.000001
    )
    with pytest.raises(SpendLimitExceeded, match="max-spend-usd"):
        await client.classify(_context().message, _context())


def test_from_settings_and_config_validation(tmp_path: Path) -> None:
    settings = OrgPilotSettings(
        llm_provider="deepseek",
        deepseek_api_key="k",
        intent_client="llm",
    )
    assert isinstance(default_intent_client(settings), LLMIntentClient)
    assert isinstance(default_intent_client(OrgPilotSettings()), HeuristicIntentClient)

    with pytest.raises(ValueError, match="deepseek 或 aihubmix"):
        LLMIntentClient.from_settings(OrgPilotSettings())
    with pytest.raises(ValueError, match="ORGPILOT_INTENT_CLIENT"):
        OrgPilotSettings(intent_client="oracle").validate()
    with pytest.raises(ValueError, match="ORGPILOT_INTENT_CLIENT=llm requires"):
        OrgPilotSettings(intent_client="llm", llm_provider="mock").validate()

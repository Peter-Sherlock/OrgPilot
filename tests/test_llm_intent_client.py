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
    DEGRADATION_INSTRUCTIONS,
    DYNAMIC_TEMPLATE_VERSION,
    FIXED_PROMPT_PREFIX,
    FORCED_TOOL_NAME,
    PROMPT_VERSION,
    LLMIntentClient,
    LLMIntentError,
    SpendLimitExceeded,
    compute_prompt_version,
    estimate_cost_usd,
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
        # default for these tests: the eval-CLI style opt-in to recording
        record=True,
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
    runtime_client = default_intent_client(settings)
    assert isinstance(runtime_client, LLMIntentClient)
    # runtime paths NEVER record: recordings carry verbatim user quotes (3b.1)
    assert runtime_client.record is False
    assert runtime_client.recent_turns == settings.intent_recent_turns
    assert isinstance(default_intent_client(OrgPilotSettings()), HeuristicIntentClient)

    with pytest.raises(ValueError, match="deepseek 或 aihubmix"):
        LLMIntentClient.from_settings(OrgPilotSettings())
    with pytest.raises(ValueError, match="ORGPILOT_INTENT_CLIENT"):
        OrgPilotSettings(intent_client="oracle").validate()
    with pytest.raises(ValueError, match="ORGPILOT_INTENT_CLIENT=llm requires"):
        OrgPilotSettings(intent_client="llm", llm_provider="mock").validate()


async def test_runtime_client_never_writes_recordings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime path (record defaults to False) must leave no files behind."""
    monkeypatch.chdir(tmp_path)
    client = LLMIntentClient(
        api_key="test-key",
        base_url="https://api.deepseek.test",
        model="deepseek-flash",
        business_tz=BJ,
        transport=httpx.MockTransport(lambda request: _tool_response(VALID_INPUT)),
    )
    assert client.record is False
    await client.classify(_context().message, _context())
    assert not (tmp_path / "evals").exists()


async def test_budget_gate_blocks_every_request_after_the_cap(tmp_path: Path) -> None:
    """Regression (3b.1 §1): each call costs far more than the $0.01 budget, so
    the FIRST request goes out, the budget is blown, and every later request is
    refused before being sent."""
    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "tool_use", "id": "t", "name": FORCED_TOOL_NAME, "input": VALID_INPUT}
                ],
                "stop_reason": "tool_use",
                "model": "deepseek-flash",
                "usage": {
                    "input_tokens": 50_000,  # ~$0.015 at official peak prices
                    "output_tokens": 900,
                    "cache_read_input_tokens": 0,
                },
            },
        )

    client = _client(tmp_path, handler, max_spend_usd=0.01)
    with pytest.raises(SpendLimitExceeded, match="exceeded"):
        await client.classify(_context().message, _context())
    assert http_calls == 1

    with pytest.raises(SpendLimitExceeded, match="not sent"):
        await client.classify(_context().message, _context())
    assert http_calls == 1  # the pre-check refused to send a second request


async def test_official_peak_prices_and_unknown_model_refuses_budget() -> None:
    # official DeepSeek peak prices, checked 2026-10 (USD per MTok)
    assert estimate_cost_usd(
        "deepseek-flash", {"input_tokens": 1_000_000, "cache_read_input_tokens": 0,
                           "output_tokens": 0}
    ) == pytest.approx(0.30)
    assert estimate_cost_usd(
        "deepseek-flash", {"input_tokens": 0, "cache_read_input_tokens": 1_000_000,
                           "output_tokens": 0}
    ) == pytest.approx(0.006)
    assert estimate_cost_usd(
        "deepseek-flash", {"input_tokens": 0, "cache_read_input_tokens": 0,
                           "output_tokens": 1_000_000}
    ) == pytest.approx(1.20)
    # unknown models have NO price: never silently default to a DeepSeek price
    assert estimate_cost_usd("gpt-5.6-luna", {"input_tokens": 10**9}) is None
    with pytest.raises(ValueError, match="价格"):
        LLMIntentClient(
            api_key="k",
            base_url="https://x.test",
            model="gpt-5.6-luna",
            business_tz=BJ,
            max_spend_usd=0.5,
        )


def test_prompt_version_covers_every_prompt_component() -> None:
    from orgpilot.intent.llm_client import _tool_schema

    assert compute_prompt_version(
        FIXED_PROMPT_PREFIX, _tool_schema(), DYNAMIC_TEMPLATE_VERSION, DEGRADATION_INSTRUCTIONS
    ) == PROMPT_VERSION
    base = {
        "prefix": FIXED_PROMPT_PREFIX,
        "tool_schema": _tool_schema(),
        "dynamic_template_version": DYNAMIC_TEMPLATE_VERSION,
        "degradation": DEGRADATION_INSTRUCTIONS,
    }

    def _version(**overrides: object) -> str:
        return compute_prompt_version(**{**base, **overrides})  # type: ignore[arg-type]

    # any wording, schema, template, or degradation change rolls the version
    assert _version() == PROMPT_VERSION
    assert _version(prefix=FIXED_PROMPT_PREFIX + "微调。") != PROMPT_VERSION
    assert _version(dynamic_template_version=DYNAMIC_TEMPLATE_VERSION + 1) != PROMPT_VERSION
    assert _version(degradation=DEGRADATION_INSTRUCTIONS + " extra") != PROMPT_VERSION
    tweaked_schema = {
        "name": _tool_schema()["name"],
        "description": _tool_schema()["description"],
        "input_schema": {"type": "object", "properties": {"confidence": {
            "type": "number", "description": "changed description"}}},
    }
    assert _version(tool_schema=tweaked_schema) != PROMPT_VERSION


async def test_recent_turns_setting_controls_conversation_window(tmp_path: Path) -> None:
    captured: list[dict] = []
    client = _client(
        tmp_path,
        lambda request: _tool_response(
            (captured.append(json.loads(request.read().decode("utf-8"))), VALID_INPUT)[1]
        ),
        recent_turns=2,
    )
    context = IntentContext(
        project_id="proj",
        message=_context().message,
        occurred_at=NOW,
        sender_id="ou_alice",
        sender_role="backend",
        sender_display_name="Alice",
        conversation_history=("t1", "t2", "t3", "t4"),
    )
    await client.classify(context.message, context)
    content = captured[0]["messages"][0]["content"]
    recent = content.split("最近对话：\n", 1)[1].split("\n本条消息", 1)[0]
    assert recent.splitlines() == ["t3", "t4"]

    silent = _client(tmp_path, lambda request: _tool_response(VALID_INPUT), recent_turns=0)
    body = silent._build_body(context.message, context)
    assert "最近对话" not in body["messages"][0]["content"]


def test_aggregate_llm_metrics_without_known_prices() -> None:
    from orgpilot.intent.llm_client import aggregate_llm_metrics

    base = {
        "prompt_version": "v1",
        "model": "gpt-5.6-luna",
        "mode": "live",
        "stop_reason": "tool_use",
        "latency_ms": 100,
        "input_tokens": 100,
        "cache_read_input_tokens": 0,
        "output_tokens": 50,
        "retried": False,
        "degraded": False,
        "validation_errors": [],
        "cost_usd_estimate": None,
    }
    stats = aggregate_llm_metrics([dict(base)])
    assert stats["total_cost_usd_estimate"] is None
    assert stats["cost_per_message_usd_estimate"] is None


def test_aggregate_llm_metrics_rates_costs_and_errors() -> None:
    from orgpilot.intent.llm_client import aggregate_llm_metrics

    base = {
        "prompt_version": "v1",
        "model": "deepseek-flash",
        "mode": "live",
        "stop_reason": "tool_use",
        "normalization": {"unknown_fields_dropped": 1, "naive_datetimes_fixed": 0},
    }
    metrics = [
        {**base, "latency_ms": 100, "input_tokens": 100, "cache_read_input_tokens": 900,
         "output_tokens": 50, "retried": False, "degraded": False,
         "validation_errors": [], "cost_usd_estimate": 0.001},
        {**base, "latency_ms": 300, "input_tokens": 200, "cache_read_input_tokens": 0,
         "output_tokens": 60, "retried": True, "degraded": False,
         "validation_errors": [{"loc": "items.0.health_status", "type": "enum"}],
         "cost_usd_estimate": 0.002},
        {**base, "latency_ms": 200, "input_tokens": 0, "cache_read_input_tokens": 0,
         "output_tokens": 70, "retried": False, "degraded": True,
         "validation_errors": [], "cost_usd_estimate": 0.0},
    ]
    stats = aggregate_llm_metrics(metrics)
    assert stats["call_count"] == 3
    assert stats["first_pass_rate"] == pytest.approx(1 / 3)
    assert stats["retry_rate"] == pytest.approx(1 / 3)
    assert stats["degraded_rate"] == pytest.approx(1 / 3)
    assert stats["validation_error_types"] == {"enum": 1}
    assert stats["cache_hit_ratio"] == pytest.approx(round(900 / 1200, 4))
    assert stats["total_cost_usd_estimate"] == pytest.approx(0.003)
    assert stats["latency_p50_ms"] == 200
    assert stats["latency_p95_ms"] == 300
    assert stats["normalization_totals"] == {
        "unknown_fields_dropped": 3,
        "naive_datetimes_fixed": 0,
    }
    assert aggregate_llm_metrics([]) == {"call_count": 0}

"""Unit tests for scripts/llm_compat_check.py using httpx MockTransport only.

The live probe must never run in tests: these exercise the per-provider run
gate, the thinking-field injection, the forced-tool probe (including the
$defs/$ref rejection fallback), per-probe HTTP error isolation, and the
stop_reason / model-mismatch statistics against a mocked endpoint.
"""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from orgpilot.intent.models import IntentResult

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "llm_compat_check.py"
_spec = importlib.util.spec_from_file_location("llm_compat_check", _SCRIPT_PATH)
llm_compat_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llm_compat_check)

VALID_INTENT_JSON = json.dumps(
    {
        "items": [
            {
                "intent": "report_status",
                "task_id": "pay_sdk",
                "speech_act": "statement",
                "polarity": "affirmative",
                "certainty": "certain",
                "health_status": "delayed",
                "time_expression": "明天下午5点",
                "expected_completion": "2026-10-14T17:00:00+08:00",
                "source_quote": "被第三方接口卡住了",
                "confidence": 0.9,
            }
        ],
        "answers_pending": False,
        "reasoning": "明确的状态汇报",
    },
    ensure_ascii=False,
)
VALID_INTENT_INPUT = json.loads(VALID_INTENT_JSON)


def _text_response(
    model: str | None,
    usage: tuple[int, int] = (300, 90),
    stop_reason: str | None = None,
) -> httpx.Response:
    body: dict[str, object] = {
        "content": [{"type": "text", "text": f"```json\n{VALID_INTENT_JSON}\n```"}],
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }
    if model is not None:
        body["model"] = model
    if stop_reason is not None:
        body["stop_reason"] = stop_reason
    return httpx.Response(200, json=body)


def _tool_response(
    name: str,
    tool_input: object,
    model: str | None,
    stop_reason: str = "tool_use",
    usage: tuple[int, int] = (120, 40),
) -> httpx.Response:
    body: dict[str, object] = {
        "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }
    if model is not None:
        body["model"] = model
    return httpx.Response(200, json=body)


def _routing_handler(captured: list[dict]) -> httpx.MockTransport:
    """Happy path: schema probes return valid JSON, tool probes return tool_use."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode("utf-8"))
        captured.append(payload)
        if payload.get("tool_choice"):
            return _tool_response("record_intent", VALID_INTENT_INPUT, payload["model"])
        if "tools" in payload:
            return _tool_response("get_task_health", {"task_id": "pay_sdk"}, payload["model"])
        return _text_response(payload["model"], stop_reason="end_turn")

    return httpx.MockTransport(handler)


def test_run_conditions_are_per_provider() -> None:
    with pytest.raises(SystemExit) as exc:
        llm_compat_check.check_run_conditions(
            {"ORGPILOT_LLM_PROVIDER": "mock", "AIHUBMIX_API_KEY": ""}, confirm_spend=False
        )
    message = str(exc.value)
    assert "ORGPILOT_LLM_PROVIDER" in message
    assert "--confirm-spend" in message

    # each online provider demands ITS OWN key
    with pytest.raises(SystemExit, match="DEEPSEEK_API_KEY"):
        llm_compat_check.check_run_conditions(
            {"ORGPILOT_LLM_PROVIDER": "deepseek", "AIHUBMIX_API_KEY": "k"}, confirm_spend=True
        )
    with pytest.raises(SystemExit, match="AIHUBMIX_API_KEY"):
        llm_compat_check.check_run_conditions(
            {"ORGPILOT_LLM_PROVIDER": "aihubmix", "DEEPSEEK_API_KEY": "k"}, confirm_spend=True
        )

    assert (
        llm_compat_check.check_run_conditions(
            {"ORGPILOT_LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "k"}, confirm_spend=True
        )
        == "deepseek"
    )
    assert (
        llm_compat_check.check_run_conditions(
            {"ORGPILOT_LLM_PROVIDER": "aihubmix", "AIHUBMIX_API_KEY": "k"}, confirm_spend=True
        )
        == "aihubmix"
    )


def test_extract_json_handles_fences_and_prose() -> None:
    assert llm_compat_check.extract_json('{"a": 1}') == '{"a": 1}'
    assert llm_compat_check.extract_json(f"```json\n{VALID_INTENT_JSON}\n```") == VALID_INTENT_JSON
    assert llm_compat_check.extract_json(f"结果如下：{VALID_INTENT_JSON}") == VALID_INTENT_JSON
    with pytest.raises(ValueError, match="JSON object"):
        llm_compat_check.extract_json("完全没有 JSON")


def test_percentile_nearest_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 100.0]
    assert llm_compat_check.percentile(values, 50) == 30.0
    assert llm_compat_check.percentile(values, 95) == 100.0
    assert llm_compat_check.percentile([], 50) == 0.0


def test_expand_schema_refs_resolves_every_ref() -> None:
    schema = IntentResult.model_json_schema()
    expanded = llm_compat_check.expand_schema_refs(schema)
    dumped = json.dumps(expanded)
    assert "$defs" not in dumped and "$ref" not in dumped
    assert expanded["type"] == "object"
    assert "items" in expanded["properties"]


async def test_thinking_flag_controls_payload_and_max_tokens() -> None:
    for thinking, has_field, expected_tokens in (
        ("omit", False, llm_compat_check.SCHEMA_PROBE_MAX_TOKENS),
        ("disabled", True, llm_compat_check.SCHEMA_PROBE_MAX_TOKENS),
        ("enabled", True, llm_compat_check.THINKING_MAX_TOKENS),
    ):
        captured: list[dict] = []
        results = await llm_compat_check.run_check(
            api_key="test-key",
            base_url="https://endpoint.test",
            model="deepseek-flash",
            calls=1,
            timeout=5.0,
            thinking=thinking,
            provider="deepseek",
            transport=_routing_handler(captured),
        )
        assert results["thinking"] == thinking
        assert all(("thinking" in payload) == has_field for payload in captured)
        if has_field:
            assert all(
                payload["thinking"] == {"type": thinking} for payload in captured
            )
        tool_payload = next(p for p in captured if "tools" in p and not p.get("tool_choice"))
        assert tool_payload["max_tokens"] == (
            llm_compat_check.THINKING_MAX_TOKENS if thinking == "enabled" else 256
        )
        schema_payload = next(p for p in captured if "tools" not in p)
        assert schema_payload["max_tokens"] == expected_tokens


async def test_run_check_reports_rates_usage_and_call_records() -> None:
    captured: list[dict] = []
    results = await llm_compat_check.run_check(
        api_key="test-key",
        base_url="https://aihubmix.test",
        model="gpt-5.6-luna",
        calls=2,
        timeout=5.0,
        transport=_routing_handler(captured),
    )
    assert results["model"] == "gpt-5.6-luna"
    assert results["endpoint"] == "aihubmix.test"
    assert results["tool_use_supported"] is True
    reliability = results["json_reliability"]
    assert reliability["json_parse_rate"] == pytest.approx(1.0)
    assert reliability["schema_pass_rate"] == pytest.approx(1.0)
    forced = results["forced_tool_use"]
    assert forced["calls"] == 2
    assert forced["tool_use_rate"] == pytest.approx(1.0)
    assert forced["pydantic_pass_rate"] == pytest.approx(1.0)
    assert forced["schema_expanded"] is False
    # 1 tool probe + 2 schema calls + 2 forced calls
    assert results["call_count"] == 5
    assert len(results["calls"]) == 5
    assert [record["stop_reason"] for record in results["calls"]] == [
        "tool_use",
        "end_turn",
        "end_turn",
        "tool_use",
        "tool_use",
    ]
    assert all(record["response_model"] == "gpt-5.6-luna" for record in results["calls"])
    assert results["max_tokens_truncations"] == 0
    assert results["model_mismatches"] == 0
    # 1 tool probe (120/40) + 2 schema calls (300/90) + 2 forced calls (120/40)
    assert results["token_usage"] == {
        "input_tokens": 120 + 2 * 300 + 2 * 120,
        "output_tokens": 40 + 2 * 90 + 2 * 40,
    }
    assert results["latency_p95_ms"] >= results["latency_p50_ms"] >= 0
    assert results["errors"] == []


async def test_truncations_and_model_mismatches_are_flagged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode("utf-8"))
        if payload.get("tool_choice"):
            return _tool_response(
                "record_intent", VALID_INTENT_INPUT, "deepseek-flash", stop_reason="max_tokens"
            )
        if "tools" in payload:
            return _tool_response("get_task_health", {"task_id": "pay_sdk"}, "deepseek-flash")
        return _text_response("deepseek-flash", stop_reason="max_tokens")

    results = await llm_compat_check.run_check(
        api_key="test-key",
        base_url="https://aihubmix.test",
        model="gpt-5.6-luna",
        calls=1,
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    # the endpoint silently mapped the unknown model name (3 calls total)
    assert results["call_count"] == 3
    assert results["model_mismatches"] == 3
    assert results["max_tokens_truncations"] == 2  # forced + schema probes hit the cap


async def test_forced_tool_probe_expands_schema_after_refs_rejection() -> None:
    captured: list[dict] = []
    forced_attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode("utf-8"))
        captured.append(payload)
        if payload.get("tool_choice"):
            forced_attempts.append(1)
            if len(forced_attempts) == 1:
                return httpx.Response(
                    400,
                    json={"error": {"message": "tools.0.input_schema: $defs/$ref not supported"}},
                )
            return _tool_response("record_intent", VALID_INTENT_INPUT, payload["model"])
        if "tools" in payload:
            return _tool_response("get_task_health", {"task_id": "pay_sdk"}, payload["model"])
        return _text_response(payload["model"])

    results = await llm_compat_check.run_check(
        api_key="test-key",
        base_url="https://api.deepseek.test",
        model="deepseek-flash",
        calls=2,
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    forced = results["forced_tool_use"]
    assert forced["schema_expanded"] is True
    assert forced["tool_use_rate"] == pytest.approx(1.0)
    assert forced["pydantic_pass_rate"] == pytest.approx(1.0)
    assert len(forced_attempts) == 3  # one rejected strict round + expanded round
    rejected_payload = next(
        p for p in captured if p.get("tool_choice")
    )
    assert "$defs" in json.dumps(rejected_payload["tools"][0]["input_schema"])
    expanded_payload = [p for p in captured if p.get("tool_choice")][-1]
    expanded_schema = expanded_payload["tools"][0]["input_schema"]
    assert "$defs" not in json.dumps(expanded_schema)
    assert "$ref" not in json.dumps(expanded_schema)
    assert any(
        error["probe"] == "forced_tool_use" and error["status"] == 400
        for error in results["errors"]
    )


async def test_http_error_is_recorded_without_aborting_the_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode("utf-8"))
        if payload.get("tool_choice"):
            return _tool_response("record_intent", VALID_INTENT_INPUT, payload["model"])
        if "tools" in payload:
            return _tool_response("get_task_health", {"task_id": "pay_sdk"}, payload["model"])
        return httpx.Response(
            500, text="upstream exploded while holding key test-key, please retry"
        )

    results = await llm_compat_check.run_check(
        api_key="test-key",
        base_url="https://aihubmix.test",
        model="gpt-5.6-luna",
        calls=2,
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert results["tool_use_supported"] is True
    assert results["forced_tool_use"]["pydantic_pass_rate"] == pytest.approx(1.0)
    assert results["json_reliability"] is None
    assert len(results["errors"]) == 1
    error = results["errors"][0]
    assert error["probe"] == "json_schema"
    assert error["status"] == 500
    assert len(error["detail"]) <= 300
    assert "test-key" not in error["detail"]  # the key is always redacted


def test_main_gates_then_prints_and_writes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "mock")
    with pytest.raises(SystemExit) as exc:
        llm_compat_check.main(["--confirm-spend"], transport=_routing_handler([]))
    assert "ORGPILOT_LLM_PROVIDER" in str(exc.value)

    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="DEEPSEEK_API_KEY"):
        llm_compat_check.main(["--confirm-spend"], transport=_routing_handler([]))

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    with pytest.raises(SystemExit, match="--confirm-spend"):
        llm_compat_check.main([], transport=_routing_handler([]))

    report_dir = tmp_path / "compat"
    exit_code = llm_compat_check.main(
        ["--confirm-spend", "--calls", "2", "--base-url", "https://deepseek.test",
         "--report-dir", str(report_dir)],
        transport=_routing_handler([]),
    )
    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["provider"] == "deepseek"
    assert summary["thinking"] == "disabled"  # provider default
    assert summary["tool_use_supported"] is True
    assert summary["forced_tool_use"]["pydantic_pass"] == 2
    report = Path(summary["report_path"])
    assert report.parent == report_dir
    assert report.name.startswith("deepseek-deepseek-flash-disabled-")
    assert json.loads(report.read_text(encoding="utf-8"))["call_count"] == 5


def test_main_thinking_override_and_provider_defaults_pass_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: list[dict] = []
    exit_code = llm_compat_check.main(
        ["--confirm-spend", "--calls", "1", "--thinking", "enabled",
         "--base-url", "https://deepseek.test", "--report-dir", str(tmp_path / "compat")],
        transport=_routing_handler(captured),
    )
    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["thinking"] == "enabled"
    assert summary["max_tokens_truncations"] == 0
    assert all(payload["thinking"] == {"type": "enabled"} for payload in captured)
    assert all(
        payload["max_tokens"] == llm_compat_check.THINKING_MAX_TOKENS for payload in captured
    )


def test_main_rejects_non_positive_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "aihubmix")
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    with pytest.raises(SystemExit, match="--calls"):
        llm_compat_check.main(
            ["--confirm-spend", "--calls", "0"], transport=_routing_handler([])
        )

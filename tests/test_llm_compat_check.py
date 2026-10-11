"""Unit tests for scripts/llm_compat_check.py using httpx MockTransport only.

The live probe must never run in tests: these exercise the guards, the request
shapes, and the statistics against a mocked Anthropic-Messages endpoint.
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


def _mock_handler(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.read().decode("utf-8"))
    if "tools" in payload:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_task_health",
                        "input": {"task_id": "pay_sdk"},
                    }
                ],
                "usage": {"input_tokens": 120, "output_tokens": 40},
            },
        )
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": f"```json\n{VALID_INTENT_JSON}\n```"}],
            "usage": {"input_tokens": 300, "output_tokens": 90},
        },
    )


@pytest.fixture
def transport() -> httpx.MockTransport:
    return httpx.MockTransport(_mock_handler)


def test_run_conditions_require_provider_key_and_confirm() -> None:
    env = {"ORGPILOT_LLM_PROVIDER": "mock", "AIHUBMIX_API_KEY": ""}
    with pytest.raises(SystemExit) as exc:
        llm_compat_check.check_run_conditions(env, confirm_spend=False)
    message = str(exc.value)
    assert "ORGPILOT_LLM_PROVIDER" in message
    assert "AIHUBMIX_API_KEY" in message
    assert "--confirm-spend" in message

    # Key present but provider still wrong
    env = {"ORGPILOT_LLM_PROVIDER": "mock", "AIHUBMIX_API_KEY": "k"}
    with pytest.raises(SystemExit):
        llm_compat_check.check_run_conditions(env, confirm_spend=False)

    # Everything satisfied
    env = {"ORGPILOT_LLM_PROVIDER": "aihubmix", "AIHUBMIX_API_KEY": "k"}
    llm_compat_check.check_run_conditions(env, confirm_spend=True)


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


async def test_run_check_reports_rates_latency_and_usage(transport: httpx.MockTransport) -> None:
    results = await llm_compat_check.run_check(
        api_key="test-key",
        base_url="https://aihubmix.test",
        model="gpt-5.6-luna",
        calls=3,
        timeout=5.0,
        transport=transport,
    )
    assert results["model"] == "gpt-5.6-luna"
    assert results["endpoint"] == "aihubmix.test"
    assert results["tool_use_supported"] is True
    reliability = results["json_reliability"]
    assert reliability["calls"] == 3
    assert reliability["json_parse_success"] == 3
    assert reliability["json_parse_rate"] == pytest.approx(1.0)
    assert reliability["schema_pass"] == 3
    assert reliability["schema_pass_rate"] == pytest.approx(1.0)
    # one tool call + three schema calls, each with the mocked usage numbers
    assert results["token_usage"] == {"input_tokens": 120 + 3 * 300, "output_tokens": 40 + 3 * 90}
    assert results["latency_p50_ms"] >= 0
    assert results["latency_p95_ms"] >= results["latency_p50_ms"]


def test_main_guards_then_prints_summary(
    transport: httpx.MockTransport,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "mock")
    with pytest.raises(SystemExit) as exc:
        llm_compat_check.main(["--confirm-spend"], transport=transport)
    assert "ORGPILOT_LLM_PROVIDER" in str(exc.value)

    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "aihubmix")
    monkeypatch.delenv("AIHUBMIX_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="AIHUBMIX_API_KEY"):
        llm_compat_check.main(["--confirm-spend"], transport=transport)

    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    with pytest.raises(SystemExit, match="--confirm-spend"):
        llm_compat_check.main([], transport=transport)

    exit_code = llm_compat_check.main(
        ["--confirm-spend", "--calls", "2", "--base-url", "https://aihubmix.test"],
        transport=transport,
    )
    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["tool_use_supported"] is True
    assert summary["json_reliability"]["calls"] == 2
    parsed = IntentResult.model_validate_json(VALID_INTENT_JSON)
    assert parsed.items[0].intent.value == "report_status"


def test_main_rejects_non_positive_calls(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    monkeypatch.setenv("ORGPILOT_LLM_PROVIDER", "aihubmix")
    monkeypatch.setenv("AIHUBMIX_API_KEY", "test-key")
    with pytest.raises(SystemExit, match="--calls"):
        llm_compat_check.main(["--confirm-spend", "--calls", "0"], transport=transport)

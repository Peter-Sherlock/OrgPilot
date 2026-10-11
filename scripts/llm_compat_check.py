"""AIHubMix gpt-5.6-luna compatibility probe for the intent layer (ADR-0008 §7).

Manual, spend-gated diagnostic. It checks, against the Anthropic
Messages-compatible endpoint:

(a) whether tool definitions come back as ``tool_use`` content blocks;
(b) how reliably the model emits JSON matching the ``IntentResult`` schema when
    the schema is embedded in the prompt, over N calls;
(c) request latency p50/p95 over those calls;
(d) accumulated token usage.

Run conditions (ALL required, otherwise the script exits without any request):
``ORGPILOT_LLM_PROVIDER=aihubmix``, ``AIHUBMIX_API_KEY`` set, and the explicit
``--confirm-spend`` flag. Do NOT wire this into CI; live calls cost money and
are non-deterministic.
"""

import argparse
import asyncio
import json
import math
import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from orgpilot.intent.models import IntentResult

DEFAULT_BASE_URL = "https://aihubmix.com"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_CALLS = 50
ANTHROPIC_VERSION = "2023-06-01"

TOOL_USE_PROMPT = (
    "支付SDK接入这个任务现在是什么健康状态？"
    "请调用 get_task_health 工具查询 task_id 为 pay_sdk 的任务。"
)

TOOL_DEFINITION: dict[str, Any] = {
    "name": "get_task_health",
    "description": "查询一个项目任务的当前健康状态与阻塞信息",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID，例如 pay_sdk"},
        },
        "required": ["task_id"],
    },
}

SCHEMA_PROMPT_PREFIX = (
    "你是研发团队的意图识别助手。请只输出一个 JSON 对象，不要输出任何其他文字，"
    "也不要使用代码块标记。JSON 必须符合下面的 JSON Schema：\n"
)
SCHEMA_PROMPT_SUFFIX = (
    '\n\n请分析这句话并输出 JSON：今天支付SDK这边被第三方接口卡住了，'
    "估计要弄到明天下午5点才能恢复。"
)


def check_run_conditions(env: dict[str, str], confirm_spend: bool) -> None:
    """Fails fast unless live spending is explicitly authorized from this shell."""
    problems: list[str] = []
    if env.get("ORGPILOT_LLM_PROVIDER") != "aihubmix":
        problems.append("ORGPILOT_LLM_PROVIDER 必须设置为 'aihubmix'")
    if not env.get("AIHUBMIX_API_KEY"):
        problems.append("必须设置 AIHUBMIX_API_KEY")
    if not confirm_spend:
        problems.append("必须显式传入 --confirm-spend（在线调用会产生费用）")
    if problems:
        raise SystemExit("未满足运行条件：\n- " + "\n- ".join(problems))


def build_client(
    api_key: str,
    base_url: str,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )


async def post_messages(
    client: httpx.AsyncClient, model: str, payload: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post("/v1/messages", json={"model": model, **payload})
    response.raise_for_status()
    return response.json()


def _text_of(data: dict[str, Any]) -> str:
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )


def extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM text block did not contain a JSON object")
    return stripped[start : end + 1]


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


async def check_tool_use(
    client: httpx.AsyncClient,
    model: str,
    latencies: list[float],
    usage_totals: dict[str, int],
) -> bool:
    started = time.perf_counter()
    data = await post_messages(
        client,
        model,
        payload={
            "system": "你是研发团队的进度协调助手。",
            "messages": [{"role": "user", "content": TOOL_USE_PROMPT}],
            "max_tokens": 256,
            "tools": [TOOL_DEFINITION],
        },
    )
    latencies.append((time.perf_counter() - started) * 1000)
    usage = data.get("usage", {})
    usage_totals["input_tokens"] += int(usage.get("input_tokens", 0))
    usage_totals["output_tokens"] += int(usage.get("output_tokens", 0))
    return any(block.get("type") == "tool_use" for block in data.get("content", []))


async def check_json_schema_reliability(
    client: httpx.AsyncClient,
    model: str,
    calls: int,
    latencies: list[float],
    usage_totals: dict[str, int],
) -> dict[str, Any]:
    schema = json.dumps(IntentResult.model_json_schema(), ensure_ascii=False)
    user_prompt = SCHEMA_PROMPT_PREFIX + schema + SCHEMA_PROMPT_SUFFIX
    parse_ok = 0
    schema_ok = 0
    for _ in range(calls):
        started = time.perf_counter()
        data = await post_messages(
            client,
            model,
            payload={
                "system": "你是研发团队的意图识别助手，输出严格遵守给定 JSON Schema。",
                "messages": [{"role": "user", "content": user_prompt}],
                "max_tokens": 1024,
            },
        )
        latencies.append((time.perf_counter() - started) * 1000)
        usage = data.get("usage", {})
        usage_totals["input_tokens"] += int(usage.get("input_tokens", 0))
        usage_totals["output_tokens"] += int(usage.get("output_tokens", 0))
        text = _text_of(data)
        try:
            parsed = json.loads(extract_json(text))
        except (ValueError, json.JSONDecodeError):
            continue
        parse_ok += 1
        try:
            IntentResult.model_validate(parsed)
        except ValueError:
            continue
        schema_ok += 1
    return {
        "calls": calls,
        "json_parse_success": parse_ok,
        "json_parse_rate": parse_ok / calls if calls else 0.0,
        "schema_pass": schema_ok,
        "schema_pass_rate": schema_ok / calls if calls else 0.0,
    }


async def run_check(
    api_key: str,
    base_url: str,
    model: str,
    calls: int,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Runs every probe and returns the summary document (no printing here)."""
    latencies: list[float] = []
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    async with build_client(api_key, base_url, timeout, transport) as client:
        tool_use_supported = await check_tool_use(client, model, latencies, usage_totals)
        reliability = await check_json_schema_reliability(
            client, model, calls, latencies, usage_totals
        )
    host = urlsplit(base_url).netloc or base_url
    return {
        "model": model,
        "endpoint": host,
        "tool_use_supported": tool_use_supported,
        "json_reliability": reliability,
        "latency_p50_ms": round(percentile(latencies, 50), 1),
        "latency_p95_ms": round(percentile(latencies, 95), 1),
        "token_usage": usage_totals,
    }


def main(argv: list[str] | None = None, transport: httpx.AsyncBaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm_compat_check",
        description="Probe AIHubMix gpt-5.6-luna compatibility for the intent layer",
    )
    parser.add_argument(
        "--calls", type=int, default=DEFAULT_CALLS, help="structured-output call count"
    )
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument(
        "--base-url",
        type=str,
        default=os.getenv("AIHUBMIX_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="acknowledge that live calls cost money and send team data to a third party",
    )
    args = parser.parse_args(argv)

    check_run_conditions(os.environ, args.confirm_spend)
    if args.calls < 1:
        raise SystemExit("--calls must be >= 1")

    results = asyncio.run(
        run_check(
            api_key=os.environ["AIHUBMIX_API_KEY"],
            base_url=args.base_url,
            model=args.model,
            calls=args.calls,
            timeout=args.timeout,
            transport=transport,
        )
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

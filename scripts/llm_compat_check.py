"""LLM compatibility probe for the intent layer (ADR-0008 §7).

Manual, spend-gated diagnostic against Anthropic Messages-compatible endpoints.
Supports the two online providers:

- ``deepseek``: DeepSeek V4.1 Flash (``deepseek-flash``) via the official
  Anthropic-format endpoint ``https://api.deepseek.com/anthropic`` (primary
  candidate);
- ``aihubmix``: ``gpt-5.6-luna`` via ``https://aihubmix.com`` (optional
  comparison).

Probes:

(a) whether plain tool definitions come back as ``tool_use`` content blocks;
(b) how reliably the model emits JSON matching the ``IntentResult`` schema when
    the schema is embedded in the prompt, over N calls;
(c) request latency p50/p95 over those calls;
(d) accumulated token usage;
(e) forced tool call (``tool_choice={"type": "tool", ...}``) with the
    ``IntentResult`` schema as the tool's ``input_schema`` — tool_use occurrence
    and Pydantic validation rates. DeepSeek has no native structured output
    (``output_config`` only supports effort). If the endpoint rejects the
    schema because of ``$defs``/``$ref``, the probe records the error, retries
    with a fully expanded schema, and notes that in the summary.

``--thinking omit|disabled|enabled`` controls the Anthropic-format thinking
switch per call; with ``enabled`` every probe raises ``max_tokens`` to 4096 so
thinking budget cannot truncate the answer. Every call records ``stop_reason``
and the response ``model``; the summary counts ``max_tokens`` truncations and
request/response model mismatches (unknown model names are silently mapped to
``deepseek-flash`` server-side, so mismatches matter). A probe that hits an
HTTP error records the status code and the first 300 characters of the body
(API key redacted) and the remaining probes still run.

Run conditions (ALL required, otherwise the script exits without any request):
``ORGPILOT_LLM_PROVIDER`` in {aihubmix, deepseek}, the matching API key set,
and the explicit ``--confirm-spend`` flag. Do NOT wire this into CI; live calls
cost money and are non-deterministic. Results are written under
``reports/compat/`` (gitignored) and printed to stdout.
"""

import argparse
import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from orgpilot.intent.models import IntentResult

PROVIDERS: dict[str, dict[str, str]] = {
    "aihubmix": {
        "key_env": "AIHUBMIX_API_KEY",
        "base_url_env": "AIHUBMIX_BASE_URL",
        "model_env": "AIHUBMIX_MODEL",
        "base_url": "https://aihubmix.com",
        "model": "gpt-5.6-luna",
        "default_thinking": "omit",
    },
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "model_env": "DEEPSEEK_MODEL",
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-flash",
        "default_thinking": "disabled",
    },
}
DEFAULT_REPORT_DIR = "reports/compat"
DEFAULT_CALLS = 50
THINKING_MAX_TOKENS = 4096
SCHEMA_PROBE_MAX_TOKENS = 1024
TOOL_PROBE_MAX_TOKENS = 256
ANTHROPIC_VERSION = "2023-06-01"
ERROR_BODY_CHARS = 300

FORCED_TOOL_NAME = "record_intent"
FORCED_TOOL_PROMPT = (
    "请分析这句话并调用 record_intent 工具记录意图："
    "今天支付SDK这边被第三方接口卡住了，估计要弄到明天下午5点才能恢复。"
)

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


def check_run_conditions(env: dict[str, str], confirm_spend: bool) -> str:
    """Fails fast unless live spending is explicitly authorized from this shell."""
    problems: list[str] = []
    provider = env.get("ORGPILOT_LLM_PROVIDER", "").strip().lower()
    if provider not in PROVIDERS:
        allowed = " / ".join(sorted(PROVIDERS))
        problems.append(f"ORGPILOT_LLM_PROVIDER 必须设置为 '{allowed}'")
    else:
        key_env = PROVIDERS[provider]["key_env"]
        if not env.get(key_env):
            problems.append(f"必须设置 {key_env}")
    if not confirm_spend:
        problems.append("必须显式传入 --confirm-spend（在线调用会产生费用）")
    if problems:
        raise SystemExit("未满足运行条件：\n- " + "\n- ".join(problems))
    return provider


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


def thinking_payload(thinking: str) -> dict[str, Any]:
    """Anthropic-format thinking switch; 'omit' sends nothing."""
    if thinking == "omit":
        return {}
    return {"thinking": {"type": thinking}}


def expand_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolves every ``$ref`` against the schema's ``$defs`` in place.

    Endpoints that reject ``$defs``/``$ref`` (DeepSeek is not guaranteed to
    accept them) get this flattened structure instead.
    """
    defs = schema.get("$defs", {})

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs.get(str(node["$ref"]).split("/")[-1], {})
                return _resolve(target)
            return {key: _resolve(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    return _resolve(schema)


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


class ProbeContext:
    """Shared counters for one run: latency, usage, per-call records, errors."""

    def __init__(self, api_key: str, requested_model: str) -> None:
        self.api_key = api_key
        self.requested_model = requested_model
        self.latencies: list[float] = []
        self.usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self.call_records: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

    async def call(
        self,
        client: httpx.AsyncClient,
        model: str,
        probe: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        data = await post_messages(client, model, payload)
        self.latencies.append((time.perf_counter() - started) * 1000)
        usage = data.get("usage", {})
        self.usage_totals["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        self.usage_totals["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        self.usage_totals["cache_read_input_tokens"] += int(
            usage.get("cache_read_input_tokens", 0) or 0
        )
        self.call_records.append(
            {
                "probe": probe,
                "stop_reason": data.get("stop_reason"),
                "response_model": data.get("model"),
            }
        )
        return data

    def record_error(self, probe: str, exc: httpx.HTTPError) -> None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        detail = (getattr(response, "text", None) or str(exc))[:ERROR_BODY_CHARS]
        self.errors.append(
            {"probe": probe, "status": status, "detail": detail.replace(self.api_key, "***")}
        )


async def check_tool_use(
    client: httpx.AsyncClient, model: str, thinking: str, ctx: ProbeContext
) -> bool:
    data = await ctx.call(
        client,
        model,
        "tool_use",
        {
            "system": "你是研发团队的进度协调助手。",
            "messages": [{"role": "user", "content": TOOL_USE_PROMPT}],
            "max_tokens": THINKING_MAX_TOKENS if thinking == "enabled" else TOOL_PROBE_MAX_TOKENS,
            "tools": [TOOL_DEFINITION],
            **thinking_payload(thinking),
        },
    )
    return any(block.get("type") == "tool_use" for block in data.get("content", []))


async def check_json_schema_reliability(
    client: httpx.AsyncClient,
    model: str,
    calls: int,
    thinking: str,
    ctx: ProbeContext,
) -> dict[str, Any]:
    schema = json.dumps(IntentResult.model_json_schema(), ensure_ascii=False)
    user_prompt = SCHEMA_PROMPT_PREFIX + schema + SCHEMA_PROMPT_SUFFIX
    parse_ok = 0
    schema_ok = 0
    validation_errors: list[dict[str, str]] = []
    for _ in range(calls):
        data = await ctx.call(
            client,
            model,
            "json_schema",
            {
                "system": "你是研发团队的意图识别助手，输出严格遵守给定 JSON Schema。",
                "messages": [{"role": "user", "content": user_prompt}],
                "max_tokens": (
                    THINKING_MAX_TOKENS if thinking == "enabled" else SCHEMA_PROBE_MAX_TOKENS
                ),
                **thinking_payload(thinking),
            },
        )
        text = _text_of(data)
        try:
            parsed = json.loads(extract_json(text))
        except (ValueError, json.JSONDecodeError):
            continue
        parse_ok += 1
        try:
            IntentResult.model_validate(parsed)
        except ValidationError as exc:
            _record_validation_errors("json_schema", exc, validation_errors)
            continue
        schema_ok += 1
    return {
        "calls": calls,
        "json_parse_success": parse_ok,
        "json_parse_rate": parse_ok / calls if calls else 0.0,
        "schema_pass": schema_ok,
        "schema_pass_rate": schema_ok / calls if calls else 0.0,
        "validation_errors": validation_errors,
    }


def _record_validation_errors(
    probe: str, exc: ValidationError, sink: list[dict[str, str]], cap: int = 50
) -> None:
    """Records loc + type per failed field so the report shows WHAT breaks."""
    for error in exc.errors(include_url=False):
        if len(sink) >= cap:
            return
        sink.append(
            {
                "probe": probe,
                "loc": ".".join(str(part) for part in error["loc"]),
                "type": error["type"],
            }
        )


async def _forced_tool_round(
    client: httpx.AsyncClient,
    model: str,
    calls: int,
    thinking: str,
    tool: dict[str, Any],
    ctx: ProbeContext,
    validation_errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    tool_use_seen = 0
    validated = 0
    payload = {
        "system": "你是研发团队的意图识别助手，必须调用给定的工具输出结构化结果。",
        "messages": [{"role": "user", "content": FORCED_TOOL_PROMPT}],
        "max_tokens": THINKING_MAX_TOKENS if thinking == "enabled" else SCHEMA_PROBE_MAX_TOKENS,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        **thinking_payload(thinking),
    }
    for _ in range(calls):
        data = await ctx.call(client, model, "forced_tool_use", payload)
        blocks = [
            block
            for block in data.get("content", [])
            if block.get("type") == "tool_use" and block.get("name") == FORCED_TOOL_NAME
        ]
        if not blocks:
            continue
        tool_use_seen += 1
        try:
            IntentResult.model_validate(blocks[0].get("input") or {})
        except ValidationError as exc:
            if validation_errors is not None:
                _record_validation_errors("forced_tool_use", exc, validation_errors)
            continue
        validated += 1
    return {
        "calls": calls,
        "tool_use_calls": tool_use_seen,
        "tool_use_rate": tool_use_seen / calls if calls else 0.0,
        "pydantic_pass": validated,
        "pydantic_pass_rate": validated / calls if calls else 0.0,
        "validation_errors": validation_errors or [],
    }


def _is_schema_rejection(exc: httpx.HTTPStatusError) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 400:
        return False
    body = getattr(response, "text", "")
    return "$defs" in body or "$ref" in body


async def check_forced_tool_use(
    client: httpx.AsyncClient,
    model: str,
    calls: int,
    thinking: str,
    ctx: ProbeContext,
) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": FORCED_TOOL_NAME,
        "description": "记录结构化意图识别结果",
        "input_schema": IntentResult.model_json_schema(),
    }
    schema_expanded = False
    validation_errors: list[dict[str, str]] = []
    try:
        stats = await _forced_tool_round(
            client, model, calls, thinking, tool, ctx, validation_errors
        )
    except httpx.HTTPStatusError as exc:
        if not _is_schema_rejection(exc):
            raise
        ctx.record_error("forced_tool_use", exc)
        tool = {**tool, "input_schema": expand_schema_refs(tool["input_schema"])}
        schema_expanded = True
        stats = await _forced_tool_round(
            client, model, calls, thinking, tool, ctx, validation_errors
        )
    return {
        **stats,
        "schema_expanded": schema_expanded,
        "validation_errors": validation_errors,
    }


async def _guarded(probe: str, ctx: ProbeContext, factory: Any) -> Any:
    """Runs one probe; an HTTP error is recorded and the run continues."""
    try:
        return await factory()
    except httpx.HTTPError as exc:
        ctx.record_error(probe, exc)
        return None


async def run_check(
    api_key: str,
    base_url: str,
    model: str,
    calls: int,
    timeout: float,
    thinking: str = "omit",
    provider: str = "aihubmix",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Runs every probe and returns the summary document (no printing here)."""
    ctx = ProbeContext(api_key, model)
    async with build_client(api_key, base_url, timeout, transport) as client:
        tool_use_supported = await _guarded(
            "tool_use", ctx, lambda: check_tool_use(client, model, thinking, ctx)
        )
        reliability = await _guarded(
            "json_schema",
            ctx,
            lambda: check_json_schema_reliability(client, model, calls, thinking, ctx),
        )
        forced_tool = await _guarded(
            "forced_tool_use",
            ctx,
            lambda: check_forced_tool_use(client, model, calls, thinking, ctx),
        )
    host = urlsplit(base_url).netloc or base_url
    truncations = sum(1 for record in ctx.call_records if record["stop_reason"] == "max_tokens")
    mismatches = sum(
        1
        for record in ctx.call_records
        if record["response_model"] and record["response_model"] != model
    )
    return {
        "provider": provider,
        "model": model,
        "endpoint": host,
        "thinking": thinking,
        "tool_use_supported": tool_use_supported,
        "json_reliability": reliability,
        "forced_tool_use": forced_tool,
        "latency_p50_ms": round(percentile(ctx.latencies, 50), 1),
        "latency_p95_ms": round(percentile(ctx.latencies, 95), 1),
        "token_usage": ctx.usage_totals,
        "call_count": len(ctx.call_records),
        "calls": ctx.call_records,
        "max_tokens_truncations": truncations,
        "model_mismatches": mismatches,
        "errors": ctx.errors,
    }


def _resolve_defaults(
    args: argparse.Namespace, env: dict[str, str], provider: str
) -> tuple[str, str, str]:
    spec = PROVIDERS[provider]
    base_url = args.base_url or env.get(spec["base_url_env"], spec["base_url"]).rstrip("/")
    model = args.model or env.get(spec["model_env"], spec["model"])
    thinking = args.thinking or spec["default_thinking"]
    return base_url, model, thinking


def main(argv: list[str] | None = None, transport: httpx.AsyncBaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm_compat_check",
        description="Probe DeepSeek/AIHubMix Anthropic compatibility for the intent layer",
    )
    parser.add_argument(
        "--calls", type=int, default=DEFAULT_CALLS, help="structured-output call count"
    )
    parser.add_argument("--model", type=str, default=None, help="override the provider default")
    parser.add_argument("--base-url", type=str, default=None, help="override the provider default")
    parser.add_argument(
        "--thinking",
        choices=("omit", "disabled", "enabled"),
        default=None,
        help="Anthropic thinking switch (default: aihubmix omit, deepseek disabled)",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--report-dir", type=str, default=DEFAULT_REPORT_DIR, help="directory for the JSON report"
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="acknowledge that live calls cost money and send team data to a third party",
    )
    args = parser.parse_args(argv)

    provider = check_run_conditions(os.environ, args.confirm_spend)
    if args.calls < 1:
        raise SystemExit("--calls must be >= 1")
    base_url, model, thinking = _resolve_defaults(args, os.environ, provider)
    key_env = PROVIDERS[provider]["key_env"]

    results = asyncio.run(
        run_check(
            api_key=os.environ[key_env],
            base_url=base_url,
            model=model,
            calls=args.calls,
            timeout=args.timeout,
            thinking=thinking,
            provider=provider,
            transport=transport,
        )
    )
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"{provider}-{model}-{thinking}-{stamp}.json"
    results["report_path"] = str(report_path)
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

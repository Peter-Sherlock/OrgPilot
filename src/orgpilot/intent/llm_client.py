"""LLM-backed intent client (ADR-0008 §7, step 3b): DeepSeek V4.1 Flash first.

The client implements the ``IntentClient`` protocol with forced tool calling
(``record_intent`` carrying the ``IntentResult`` JSON schema,
``tool_choice=tool``, ``temperature=0``, ``thinking=disabled`` — exactly the
configuration the compatibility probe settled on; thinking mode is never used).
The prompt puts a hashed fixed prefix FIRST (role, intent taxonomy, orthogonal
attributes, R1–R11 judging rules rewritten, output requirements) and the
per-message dynamic content AFTER it, so DeepSeek's automatic context cache
hits on every call within a run. All prompt examples are freshly written and
share no sentence with any eval sample.

Validation failures append a Pydantic error summary to the dynamic content and
retry exactly once; a second failure raises ``LLMIntentError`` and the router
falls back. When a forced call returns no ``tool_use`` block, the documented
degradation path runs once (schema embedded in the prompt, JSON parsed from
the text block). The LLM-boundary normalization (drop unknown fields, attach
the business timezone to naive completion times) runs before validation.

Every call is recorded for offline replay under
``evals/intent/recordings/<prompt version>/<request hash>.json``; replay mode
only READS recordings and fails on a miss. One structured log line (no key
material) is emitted per ``classify`` with tokens, latency, cache reads,
retry/degradation flags, validation errors, and normalization counts.
"""

import hashlib
import json
import logging
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from orgpilot.config import OrgPilotSettings
from orgpilot.intent.client import IntentContext
from orgpilot.intent.models import IntentResult

logger = logging.getLogger(__name__)

FIXED_PROMPT_PREFIX = """你是研发团队的意图识别助手。你的唯一输出方式是调用 record_intent 工具，\
tool 参数必须严格符合工具 input_schema 给出的 JSON Schema。

## 意图分类
- report_status：汇报任务状态或进展。例：「搜索服务最近老是超时」「审批流第一版已经上线了」
- make_commitment：明确承诺某个时间前完成。例：「我保证周五下班前把灰度发布做完」
- answer_pending：直接回答机器人上一条追问（补时间、补任务名等），此时 answers_pending=true
- query_status：询问任务当前状态。例：「数据迁移那批跑完了吗？」
- request_sync：请求发起全员进度同步（只有项目负责人有权发起）
- acknowledge：单纯的确认、应答（好的、收到、发吧）
- chitchat：与工作进度无关的寒暄、吐槽、闲聊
- help：询问机器人能做什么
- out_of_scope：项目协调之外的请求（订会议室、写周报等）
- correction / dependency_wait / unavailability / text_approval / correct_bot / \
propose_plan_change：P1/P2 识别类，只识别不执行

## 正交属性
- speech_act：statement=陈述；question=提问；hypothetical=假设（“如果……就……”），\
假设永远不产生状态变更；imperative=请求或指令
- polarity：affirmative=肯定；negative=带否定词（没、不、别、尚未）。\
否定风险等于正常（“没卡住了”→on_track）；字面否定但问题仍在的，按实际含义判断
- certainty：certain=明确断言；hedged=含糊推测（应该、估计、可能、说不好）

## health_status 判定规则
1. 预计完成时间晚于任务截止日，或明确说延期/推迟 → delayed，必须给出 blocker；
2. 有风险但预计能在截止日前解决，或没有给出时间 → at_risk；
3. 正常推进、已完成、阻塞已解除 → on_track；
4. 完全说不清 → unknown 或留空。
非负责人汇报别人的任务照常识别，不要因为身份拒绝。一条消息包含多件事时拆成多个 items。

## 时间规则
- expected_completion 只填“任务完成或阻塞解除”的时间：开始时间、演示时间、\
“几点能定位”、下次同步时间都不填；任务已经完成也不填；
- 今晚/晚上=20:00，中午=12:00，早上=10:00，下班前=18:00，月底=当月最后一天 18:00；
- 只有日期没有时刻=当天 18:00；“明天下午5点”=次日 17:00；“下周三”=下一个自然周的周三；
- expected_completion 必须是带时区偏移的 ISO 8601 字符串。

## 换话题
对话状态给出待回答问题时：本条消息正面或部分回答了它 → answer_pending 且 \
answers_pending=true；只是闲聊或问别的 → 按消息本身分类且 answers_pending=false，\
不要强行当作回答。

## 输出要求
- 只调用 record_intent 工具；source_quote 必须是本条消息的逐字子串，不得改写；
- reasoning 不超过两句话。"""

PROMPT_VERSION = hashlib.sha256(FIXED_PROMPT_PREFIX.encode("utf-8")).hexdigest()[:12]

FORCED_TOOL_NAME = "record_intent"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
ERROR_BODY_CHARS = 300

# 官方高峰价估算（USD / 百万 token），仅用于 --max-spend-usd 预算闸门与费用
# 报告；价格调整时更新此表。cache_read 为缓存命中价，未列出的模型用 DEFAULT。
PEAK_PRICE_PER_MTOK: dict[str, dict[str, float]] = {
    "deepseek-flash": {"input": 0.28, "cache_read": 0.028, "output": 0.42},
}
DEFAULT_PRICE_PER_MTOK = {"input": 0.28, "cache_read": 0.028, "output": 0.42}


class LLMIntentError(RuntimeError):
    """The LLM could not produce a schema-valid IntentResult after the retry."""


class ReplayMissError(FileNotFoundError):
    """Replay mode had no recording for a request — an infrastructure failure.

    Subclasses FileNotFoundError so CLI handlers can catch either; the router
    deliberately re-raises it instead of degrading to the fallback reply.
    """


class SpendLimitExceeded(RuntimeError):
    """The estimated live-call cost exceeded --max-spend-usd."""


def estimate_cost_usd(model: str, usage: dict[str, Any]) -> float:
    price = PEAK_PRICE_PER_MTOK.get(model, DEFAULT_PRICE_PER_MTOK)
    return (
        usage.get("input_tokens", 0) / 1e6 * price["input"]
        + usage.get("cache_read_input_tokens", 0) / 1e6 * price["cache_read"]
        + usage.get("output_tokens", 0) / 1e6 * price["output"]
    )


def aggregate_llm_metrics(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregates per-classify CallMetric dicts for the evaluation reports."""
    total = len(metrics)
    if not total:
        return {"call_count": 0}
    latencies = sorted(float(metric["latency_ms"]) for metric in metrics)

    def _pct(percent: float) -> float:
        rank = max(1, math.ceil(percent / 100 * len(latencies)))
        return latencies[rank - 1]

    input_total = sum(int(metric["input_tokens"]) for metric in metrics)
    cache_total = sum(int(metric["cache_read_input_tokens"]) for metric in metrics)
    output_total = sum(int(metric["output_tokens"]) for metric in metrics)
    cost = sum(float(metric["cost_usd_estimate"]) for metric in metrics)
    error_types: dict[str, int] = {}
    for metric in metrics:
        for error in metric["validation_errors"]:
            error_types[error["type"]] = error_types.get(error["type"], 0) + 1
    normalizers = metrics[0].get("normalization", {})
    normalization_totals = {
        key: sum(int(metric.get("normalization", {}).get(key, 0)) for metric in metrics)
        for key in normalizers
    }
    return {
        "call_count": total,
        "first_pass_rate": sum(1 for m in metrics if not m["retried"] and not m["degraded"])
        / total,
        "retry_rate": sum(1 for m in metrics if m["retried"]) / total,
        "degraded_rate": sum(1 for m in metrics if m["degraded"]) / total,
        "validation_error_types": error_types,
        "latency_p50_ms": round(_pct(50), 1),
        "latency_p95_ms": round(_pct(95), 1),
        "avg_input_tokens": round(input_total / total, 1),
        "avg_output_tokens": round(output_total / total, 1),
        "avg_cache_read_tokens": round(cache_total / total, 1),
        "cache_hit_ratio": round(cache_total / (cache_total + input_total), 4)
        if (cache_total + input_total)
        else 0.0,
        "total_cost_usd_estimate": round(cost, 4),
        "cost_per_message_usd_estimate": round(cost / total, 6),
        "normalization_totals": normalization_totals,
    }


def validate_intent_payload(payload: Any) -> tuple[IntentResult | None, list[dict[str, str]]]:
    """Validates the normalized tool input; returns (result, errors)."""
    try:
        return IntentResult.model_validate(payload), []
    except ValidationError as exc:
        errors = [
            {"loc": ".".join(str(part) for part in error["loc"]), "type": error["type"]}
            for error in exc.errors(include_url=False)
        ]
        return None, errors


def _error_summary(errors: list[dict[str, str]]) -> str:
    joined = "；".join(f"{error['loc']} ({error['type']})" for error in errors[:8])
    return (
        "注意：上一次调用 record_intent 的参数没有通过校验，错误：" + joined + "。"
        "请修正这些问题后重新调用 record_intent 工具，不要改变其他字段。"
    )


# Mirrors the model definitions in orgpilot.intent.models; used to silently
# DROP hallucinated keys at the LLM boundary (counted), everything else is
# left for Pydantic to reject and drive the single retry.
TOP_LEVEL_FIELDS = {"reasoning", "items", "answers_pending"}
ITEM_FIELDS = {
    "intent",
    "task_id",
    "speech_act",
    "polarity",
    "certainty",
    "health_status",
    "time_expression",
    "expected_completion",
    "blocker",
    "source_quote",
    "confidence",
}


def normalize_intent_payload(
    payload: Any, business_tz: ZoneInfo
) -> tuple[dict[str, Any], dict[str, int]]:
    """Repairs ONLY the two benign LLM output quirks, with per-kind counts.

    - unknown (hallucinated) fields at either level are dropped;
    - a naive ``expected_completion`` string gets the business timezone
      attached.

    Every other malformation is left untouched so Pydantic rejects it and the
    caller can retry with the validation error summary.
    """
    counters = {"unknown_fields_dropped": 0, "naive_datetimes_fixed": 0}

    def _fix_datetime(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value  # unparseable: Pydantic reports the real error
        if parsed.tzinfo is None:
            counters["naive_datetimes_fixed"] += 1
            return parsed.replace(tzinfo=business_tz).isoformat()
        return value

    def _clean_dict(node: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key not in allowed:
                counters["unknown_fields_dropped"] += 1
                continue
            if key == "expected_completion":
                cleaned[key] = _fix_datetime(value)
            elif key == "items" and isinstance(value, list):
                cleaned[key] = [
                    _clean_dict(entry, ITEM_FIELDS) if isinstance(entry, dict) else entry
                    for entry in value
                ]
            else:
                cleaned[key] = value
        return cleaned

    if not isinstance(payload, dict):
        return payload, counters
    return _clean_dict(payload, TOP_LEVEL_FIELDS), counters


def request_hash(payload: dict[str, Any]) -> str:
    """Stable hash over the full request body, used as the recording key."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _tool_schema() -> dict[str, Any]:
    return {
        "name": FORCED_TOOL_NAME,
        "description": "记录这条消息的意图识别结果",
        "input_schema": IntentResult.model_json_schema(),
    }


class LLMIntentClient:
    """IntentClient over an Anthropic-format endpoint with forced tool calling."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        business_tz: ZoneInfo,
        mode: str = "live",
        recordings_dir: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_spend_usd: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if mode not in {"live", "replay"}:
            raise ValueError("mode must be 'live' or 'replay'")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.business_tz = business_tz
        self.mode = mode
        self.recordings_dir = (
            recordings_dir or Path("evals/intent/recordings") / PROMPT_VERSION
        )
        self.max_tokens = max_tokens
        self.max_spend_usd = max_spend_usd
        self.spent_usd = 0.0
        self.metrics: list[dict[str, Any]] = []
        self._tool_schema = _tool_schema()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    @classmethod
    def from_settings(
        cls,
        settings: OrgPilotSettings,
        *,
        mode: str = "live",
        recordings_dir: Path | None = None,
        max_spend_usd: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "LLMIntentClient":
        if settings.llm_provider == "deepseek":
            key, base_url, model = (
                settings.deepseek_api_key or "",
                settings.deepseek_base_url,
                settings.deepseek_model,
            )
        elif settings.llm_provider == "aihubmix":
            key, base_url, model = (
                settings.aihubmix_api_key or "",
                settings.aihubmix_base_url,
                settings.aihubmix_model,
            )
        else:
            raise ValueError(
                "LLMIntentClient 需要 ORGPILOT_LLM_PROVIDER=deepseek 或 aihubmix"
            )
        return cls(
            api_key=key,
            base_url=base_url,
            model=model,
            business_tz=settings.business_timezone,
            mode=mode,
            recordings_dir=recordings_dir,
            max_spend_usd=max_spend_usd,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------- protocol

    async def classify(self, message: str, context: IntentContext) -> IntentResult:
        started = time.perf_counter()
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        stop_reason: str | None = None
        validation_errors: list[dict[str, str]] = []
        normalization = {"unknown_fields_dropped": 0, "naive_datetimes_fixed": 0}
        retried = False
        degraded = False

        def _absorb(data: dict[str, Any]) -> None:
            nonlocal stop_reason
            call_usage = data.get("usage", {}) or {}
            for key in usage:
                usage[key] += int(call_usage.get(key, 0) or 0)
            if data.get("stop_reason"):
                stop_reason = data.get("stop_reason")

        body = self._build_body(message, context)
        data, _hash = await self._exchange(body)
        _absorb(data)
        payload, tool_use_seen = self._tool_input_of(data)

        if not tool_use_seen:
            # documented degradation: schema in prompt, parse the text block
            degraded = True
            fallback_body = self._build_body(message, context, degraded=True)
            data, _hash = await self._exchange(fallback_body)
            _absorb(data)
            payload = self._text_input_of(data)

        if payload is not None:
            payload, counts = normalize_intent_payload(payload, self.business_tz)
            normalization = {
                key: normalization[key] + counts[key] for key in normalization
            }
        result, first_errors = validate_intent_payload(payload)

        if result is None:
            # the metric reports the errors that DROVE the retry
            validation_errors = first_errors
            retried = True
            retry_body = self._build_body(
                message, context, error_note=_error_summary(first_errors)
            )
            data, _hash = await self._exchange(retry_body)
            _absorb(data)
            payload, tool_use_seen = self._tool_input_of(data)
            if payload is not None:
                payload, counts = normalize_intent_payload(payload, self.business_tz)
                normalization = {
                    key: normalization[key] + counts[key] for key in normalization
                }
            result, retry_errors = validate_intent_payload(payload)
            if result is None:
                raise LLMIntentError(
                    "intent output failed validation after retry: "
                    + _error_summary(retry_errors)
                )

        metric = {
            "prompt_version": PROMPT_VERSION,
            "model": self.model,
            "mode": self.mode,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cache_read_input_tokens": usage["cache_read_input_tokens"],
            "stop_reason": stop_reason,
            "retried": retried,
            "degraded": degraded,
            "validation_errors": validation_errors,
            "normalization": normalization,
            "cost_usd_estimate": round(
                self._cost_of(usage) if self.mode == "live" else 0.0, 6
            ),
        }
        self.metrics.append(metric)
        logger.info("intent_llm_call %s", json.dumps(metric, ensure_ascii=False))
        return result

    # ------------------------------------------------------------- plumbing

    def _cost_of(self, usage: dict[str, Any]) -> float:
        return estimate_cost_usd(self.model, usage)

    def _build_body(
        self,
        message: str,
        context: IntentContext,
        *,
        error_note: str | None = None,
        degraded: bool = False,
    ) -> dict[str, Any]:
        content = self._dynamic_content(message, context)
        if error_note:
            content = content + "\n\n" + error_note
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": content}],
        }
        if degraded:
            body["system"] = (
                FIXED_PROMPT_PREFIX
                + "\n\n## 降级输出\n无法调用工具时，直接输出一个严格符合以下 JSON Schema 的"
                "JSON 对象，不要输出任何其他文字：\n"
                + json.dumps(IntentResult.model_json_schema(), ensure_ascii=False)
            )
        else:
            body["system"] = FIXED_PROMPT_PREFIX
            body["tools"] = [self._tool_schema]
            body["tool_choice"] = {"type": "tool", "name": FORCED_TOOL_NAME}
        return body

    def _dynamic_content(self, message: str, context: IntentContext) -> str:
        def _tasks(pairs: list[str]) -> str:
            return "；".join(pairs) if pairs else "（无）"

        lines = [
            f"当前时间：{context.occurred_at.isoformat()}（时区 {self.business_tz}）",
            "发送者："
            f"{context.sender_display_name}（{context.sender_id}，角色 {context.sender_role}）",
            "负责的任务："
            + _tasks(
                f"{task.task_id}={task.title}" for task in self._sender_tasks(context)
            ),
            "全部任务："
            + _tasks(
                f"{task.task_id}={task.title}"
                + (f"（截止 {task.deadline.isoformat()}）" if task.deadline else "")
                + f"（健康度 {task.health_status}）"
                for task in context.known_tasks
            ),
        ]
        pending = context.pending_question
        if pending is not None:
            lines.append(
                f"待回答问题：{pending.question}（目标任务："
                + ("、".join(pending.task_ids) or "未指明")
                + f"；已追问 {pending.asked_rounds} 轮）"
            )
        else:
            lines.append("待回答问题：无")
        confirmation = context.pending_confirmation
        if confirmation is not None:
            lines.append(f"待确认的同步卡片：发起人 {confirmation.initiated_by}")
        if context.conversation_history:
            lines.append(
                "最近对话：\n" + "\n".join(context.conversation_history[-6:]),
            )
        lines.append(f"本条消息：\n{message}")
        return "\n".join(lines)

    @staticmethod
    def _sender_tasks(context: IntentContext) -> list[Any]:
        return [
            task for task in context.known_tasks if task.task_id in context.sender_task_ids
        ]

    def _tool_input_of(self, data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        for block in data.get("content", []) or []:
            if block.get("type") == "tool_use" and block.get("name") == FORCED_TOOL_NAME:
                return block.get("input") or {}, True
        return None, False

    def _text_input_of(self, data: dict[str, Any]) -> dict[str, Any] | None:
        text = "".join(
            block.get("text", "") for block in data.get("content", []) or []
            if block.get("type") == "text"
        )
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = "\n".join(stripped.splitlines()[1:-1]).strip()
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None

    async def _exchange(self, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        key = request_hash(body)
        if self.mode == "replay":
            path = self.recordings_dir / f"{key}.json"
            if not path.exists():
                raise ReplayMissError(f"missing replay recording: {path}")
            record = json.loads(path.read_text(encoding="utf-8"))
            return record["response"], key
        response = await self._http.post("/v1/messages", json=body)
        response.raise_for_status()
        data = response.json()
        self._save_recording(key, data)
        usage = data.get("usage", {}) or {}
        self.spent_usd += self._cost_of(usage)
        if self.max_spend_usd is not None and self.spent_usd > self.max_spend_usd:
            raise SpendLimitExceeded(
                f"estimated live spend {self.spent_usd:.4f} USD exceeded "
                f"--max-spend-usd {self.max_spend_usd}"
            )
        return data, key

    def _save_recording(self, key: str, data: dict[str, Any]) -> None:
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "prompt_version": PROMPT_VERSION,
            "request_hash": key,
            "model": self.model,
            "endpoint": urlsplit(self.base_url).netloc,
            "stop_reason": data.get("stop_reason"),
            "usage": data.get("usage", {}),
            "recorded_at": datetime.now(UTC).isoformat(),
            "response": data,
        }
        path = self.recordings_dir / f"{key}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

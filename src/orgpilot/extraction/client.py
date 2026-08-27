"""Provider-agnostic LLM clients supporting deterministic mock, replay, and live APIs."""

import json
from abc import ABC, abstractmethod
from typing import Any

import httpx

from orgpilot.domain.enums import HealthStatus
from orgpilot.extraction.models import (
    ExtractedCommitment,
    ExtractedHealthClaim,
    ExtractionResult,
    MessageContext,
)
from orgpilot.extraction.verifier import TemporalResolver


class LLMClient(ABC):
    """Abstract LLM extraction client protocol."""

    @abstractmethod
    def extract(
        self, system_prompt: str, user_prompt: str, raw_message: str, context: MessageContext
    ) -> ExtractionResult:
        """Extracts structured claims from text."""


class AnthropicCompatibleLLMClient(LLMClient):
    """Calls an Anthropic Messages-compatible endpoint and validates typed JSON output."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://aihubmix.com",
        max_tokens: int = 1024,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def extract(
        self,
        system_prompt: str,
        user_prompt: str,
        raw_message: str,
        context: MessageContext,
    ) -> ExtractionResult:
        schema = json.dumps(ExtractionResult.model_json_schema(), ensure_ascii=False)
        payload = {
            "model": self.model,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": f"{user_prompt}\n\nOutput JSON matching this schema:\n{schema}",
                }
            ],
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        response = self._client.post(
            f"{self.base_url}/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        text_blocks = [
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ]
        if not text_blocks:
            raise ValueError("LLM response did not contain a text block")
        return ExtractionResult.model_validate_json(self._extract_json(text_blocks[0]))

    @staticmethod
    def _extract_json(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            stripped = "\n".join(lines[1:-1]).strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise ValueError("LLM text block did not contain a JSON object")
        return stripped[start : end + 1]

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class MockLLMClient(LLMClient):
    """Deterministic offline mock LLM client with fixture registry and rule-based heuristic."""

    def __init__(self) -> None:
        self._fixtures: dict[str, ExtractionResult] = {}
        self.call_history: list[tuple[str, MessageContext, ExtractionResult]] = []

    def register_fixture(self, text_snippet: str, result: ExtractionResult) -> None:
        """Registers a predefined ExtractionResult for messages matching text_snippet."""
        self._fixtures[text_snippet.lower()] = result

    def extract(
        self, system_prompt: str, user_prompt: str, raw_message: str, context: MessageContext
    ) -> ExtractionResult:
        clean_msg = raw_message.strip()
        clean_lower = clean_msg.lower()

        # 1. Check exact or snippet matches in fixtures
        for snippet, fixture in self._fixtures.items():
            if snippet in clean_lower:
                self.call_history.append((raw_message, context, fixture))
                return fixture

        # 2. Rule-based heuristic simulation for standard developer expressions
        result = self._heuristic_extract(raw_message, context)
        self.call_history.append((raw_message, context, result))
        return result

    def _heuristic_extract(self, message: str, context: MessageContext) -> ExtractionResult:
        # Match task ID from context with bilingual alias support
        matched_task_id: str | None = None
        msg_clean = message.lower().replace(" ", "")
        for task_id, title in context.known_tasks.items():
            t_id_lower = task_id.lower()
            t_title_lower = title.lower().replace(" ", "")
            if t_id_lower in message.lower() or t_title_lower in msg_clean:
                matched_task_id = task_id
                break
            # Keyword indicator matching
            keywords_map = {
                ("支付", "sdk", "pay", "payment"): ["pay", "payment", "支付", "sdk", "backend", "api"],
                ("前端", "结账", "收银", "页面", "ui", "checkout"): [
                    "checkout",
                    "front",
                    "frontend",
                    "收银",
                    "结账",
                    "页面",
                ],
                ("压测", "验收", "测试", "qa", "test"): [
                    "qa",
                    "test",
                    "压测",
                    "验收",
                    "测试",
                    "联调",
                ],
                (
                    "后端",
                    "接口",
                    "api",
                    "网关",
                    "服务端",
                    "数据库",
                    "redis",
                    "死锁",
                    "连接池",
                ): ["backend", "api", "server", "后端", "接口", "db"],
            }
            matched = False
            for trigger_kws, task_indicators in keywords_map.items():
                if any(kw in message.lower() for kw in trigger_kws) and any(
                    ind in t_id_lower or ind in t_title_lower for ind in task_indicators
                ):
                    matched_task_id = task_id
                    matched = True
                    break
            if matched:
                break

        if matched_task_id is None and len(context.known_tasks) == 1:
            matched_task_id = next(iter(context.known_tasks))

        # 1. Check for casual chat / non-actionable noise
        casual_phrases = [
            "你好",
            "hello",
            "hi",
            "今天好累",
            "早安",
            "晚安",
            "吃饭了吗",
            "今天天气真好",
            "哈哈",
            "收到，赞",
            "赞！",
        ]
        if any(phrase in message.lower() for phrase in casual_phrases) and not any(
            k in message
            for k in ["延期", "卡住", "报错", "完成", "搞定", "block", "调通", "已解决", "按原计划推进"]
        ):
            return ExtractionResult(
                is_actionable=False, claims=[], commitments=[], reasoning="Casual chat"
            )

        if matched_task_id is None:
            return ExtractionResult(
                is_actionable=False,
                claims=[],
                commitments=[],
                reasoning="No unambiguous task reference found",
            )

        # 2. Check for commitment
        if any(k in message for k in ["保证", "承诺", "一定", "提 PR", "提pr"]):
            due_at = TemporalResolver.resolve_relative_time(message, context.occurred_at)
            commitment = ExtractedCommitment(
                target_type="task",
                target_id=matched_task_id,
                predicate="workflow_status",
                expected_value="review",
                due_at=due_at,
                confidence=0.92,
                source_quote=message,
            )
            return ExtractionResult(
                is_actionable=True, claims=[], commitments=[commitment], reasoning="Commitment made"
            )

        # 3. Check for resolved recovery ("已解决", "已修复", "恢复正常", "全部调通", "已完成", "按原计划推进")
        is_recovery = any(
            k in message
            for k in [
                "已解决",
                "已修复",
                "恢复正常",
                "全部调通",
                "已完成",
                "搞定了",
                "按原计划推进",
            ]
        ) and not any(k in message for k in ["延期", "搞不定", "无法按时", "受阻", "才能把"])
        if is_recovery:
            claim = ExtractedHealthClaim(
                task_id=matched_task_id,
                health_status=HealthStatus.ON_TRACK,
                expected_completion=None,
                blocker=None,
                confidence=0.98,
                source_quote=message,
            )
            return ExtractionResult(
                is_actionable=True, claims=[claim], commitments=[], reasoning="Task recovered"
            )

        # 4. Check for delayed / at_risk
        if any(
            k in message
            for k in [
                "延期",
                "卡住",
                "报错",
                "问题",
                "不行",
                "搞不定",
                "受阻",
                "block",
                "不太稳",
                "才能",
                "需要到",
            ]
        ):
            exp_comp = TemporalResolver.resolve_relative_time(message, context.occurred_at)
            is_delayed = any(
                k in message for k in ["延期", "搞不定", "无法按时", "才能", "需要到", "才能把"]
            ) or (exp_comp is not None and any(k in message for k in ["报错", "卡住", "受阻"]))
            status = HealthStatus.DELAYED if is_delayed else HealthStatus.AT_RISK
            blocker = message.split("，")[0] if "，" in message else message
            claim = ExtractedHealthClaim(
                task_id=matched_task_id,
                health_status=status,
                expected_completion=exp_comp,
                blocker=blocker,
                confidence=0.95,
                source_quote=message,
            )
            return ExtractionResult(
                is_actionable=True, claims=[claim], commitments=[], reasoning="Risk/delay reported"
            )

        return ExtractionResult(
            is_actionable=False, claims=[], commitments=[], reasoning="No actionable state found"
        )


class RecordedReplayClient(LLMClient):
    """Replays recorded extraction outputs from a test fixture dataset."""

    def __init__(self, recorded_data: dict[str, ExtractionResult]) -> None:
        self._data = recorded_data

    def extract(
        self, system_prompt: str, user_prompt: str, raw_message: str, context: MessageContext
    ) -> ExtractionResult:
        if raw_message in self._data:
            return self._data[raw_message]
        raise KeyError(f"No recorded response for message: {raw_message!r}")

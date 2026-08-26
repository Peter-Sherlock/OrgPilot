"""Provider-agnostic LLM clients supporting deterministic mock, replay, and live APIs."""

from abc import ABC, abstractmethod

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
        for task_id, title in context.known_tasks.items():
            t_id_lower = task_id.lower()
            t_title_lower = title.lower()
            if t_id_lower in message.lower() or t_title_lower in message.lower():
                matched_task_id = task_id
                break
            if "前端" in message and ("frontend" in t_id_lower or "frontend" in t_title_lower):
                matched_task_id = task_id
                break
            if any(w in message for w in ["后端", "支付", "sdk", "数据库", "redis", "接口"]) and (
                "backend" in t_id_lower or "api" in t_id_lower
            ):
                matched_task_id = task_id
                break

        if matched_task_id is None and context.known_tasks:
            matched_task_id = next(iter(context.known_tasks.keys()))
        elif matched_task_id is None:
            matched_task_id = "default_task"

        # 1. Check for casual chat / non-actionable noise
        casual_phrases = [
            "今天好累",
            "早安",
            "晚安",
            "吃饭了吗",
            "今天天气真好",
            "哈哈",
            "收到，赞",
            "赞！",
        ]
        if any(phrase in message for phrase in casual_phrases) and not any(
            k in message
            for k in ["延期", "卡住", "报错", "完成", "搞定", "block", "调通", "已解决"]
        ):
            return ExtractionResult(
                is_actionable=False, claims=[], commitments=[], reasoning="Casual chat"
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

        # 3. Check for resolved recovery ("已解决", "已修复", "恢复正常", "全部调通", "已完成")
        is_recovery = any(
            k in message for k in ["已解决", "已修复", "恢复正常", "全部调通", "已完成", "搞定了"]
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

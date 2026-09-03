"""Provider-agnostic LLM clients supporting deterministic mock, replay, and live APIs."""

import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from orgpilot.domain.enums import HealthStatus, MessageIntent
from orgpilot.extraction.models import (
    ExtractedCommitment,
    ExtractedHealthClaim,
    ExtractionResult,
    MessageContext,
    TaskProposal,
)
from orgpilot.extraction.verifier import TemporalResolver

_TASK_OP_CREATE_RE = re.compile(
    r"(新增|新建|创建|建个|立个|加个|排一?个|开一?个)[一个项]*任务|new\s+task", re.IGNORECASE
)
_TASK_OP_REASSIGN_RE = re.compile(r"改派给?|移交给?|负责人?换成?|转给?|交给?|重新指派给?")
_TASK_OP_DEADLINE_RE = re.compile(
    r"(截止(时间|日期)?|deadline|交付时间|完成时间)[^，。;；\n]{0,6}"
    r"(改到|改为|改成|变更为?|调整到?|提前到?|推迟到?)"
    r"|把[^，。;；\n]{0,12}改到",
    re.IGNORECASE,
)
_TIME_HINT_RE = re.compile(r"周|天|号|点|月|底")


class LLMUnavailableError(RuntimeError):
    """The LLM provider is unreachable: retries exhausted or circuit open."""


class LLMResponseError(RuntimeError):
    """The LLM provider returned a response that cannot satisfy the extraction contract."""


class CircuitBreaker:
    """Fail-fast guard after repeated consecutive provider failures."""

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at >= self.cooldown_seconds:
            # Half-open: give the provider one probe window.
            self._opened_at = None
            self._consecutive_failures = self.failure_threshold - 1
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._opened_at = self._clock()


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
        reasoning_effort: str | None = None,
        max_retries: int = 1,
        breaker_failure_threshold: int = 3,
        breaker_cooldown_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self.max_retries = max_retries
        self.breaker = CircuitBreaker(
            failure_threshold=breaker_failure_threshold,
            cooldown_seconds=breaker_cooldown_seconds,
        )

    def extract(
        self,
        system_prompt: str,
        user_prompt: str,
        raw_message: str,
        context: MessageContext,
    ) -> ExtractionResult:
        if not self.breaker.allow():
            raise LLMUnavailableError("LLM circuit breaker open; failing fast")
        last_error: Exception | None = None
        for _attempt in range(1 + self.max_retries):
            try:
                result = self._extract_once(system_prompt, user_prompt, raw_message, context)
                self.breaker.record_success()
                return result
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPStatusError,
                LLMResponseError,
            ) as exc:
                last_error = exc
        self.breaker.record_failure()
        if isinstance(last_error, LLMResponseError):
            raise LLMUnavailableError(
                f"LLM returned invalid structured output after retries: {last_error}"
            ) from last_error
        raise LLMUnavailableError(f"LLM unavailable after retries: {last_error}") from last_error

    def _extract_once(
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
            # Greedy decoding keeps the extraction benchmark reproducible.
            "temperature": 0,
            "stream": False,
        }
        if self.reasoning_effort is not None:
            if self.reasoning_effort == "none":
                payload["thinking"] = {"type": "disabled"}
            else:
                payload["thinking"] = {"type": "enabled"}
                payload["output_config"] = {"effort": self.reasoning_effort}
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
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise LLMResponseError("LLM response body was not valid JSON") from exc
        content = data.get("content", [])
        text_blocks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not text_blocks:
            block_types = [
                str(block.get("type", "unknown")) for block in content if isinstance(block, dict)
            ]
            raise LLMResponseError(
                "LLM response did not contain a text block "
                f"(block_types={block_types}, stop_reason={data.get('stop_reason')!r})"
            )
        try:
            return ExtractionResult.model_validate_json(self._extract_json(text_blocks[0]))
        except ValidationError as exc:
            raise LLMResponseError("LLM JSON did not match ExtractionResult schema") from exc

    @staticmethod
    def _extract_json(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            stripped = "\n".join(lines[1:-1]).strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise LLMResponseError("LLM text block did not contain a JSON object")
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
                ("支付", "sdk", "pay", "payment"): [
                    "pay",
                    "payment",
                    "支付",
                    "sdk",
                    "backend",
                    "api",
                ],
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
            for k in [
                "延期",
                "卡住",
                "报错",
                "完成",
                "搞定",
                "block",
                "调通",
                "已解决",
                "按原计划推进",
            ]
        ):
            return ExtractionResult(
                is_actionable=False,
                claims=[],
                commitments=[],
                intent=MessageIntent.CHIT_CHAT,
                reasoning="Casual chat",
            )

        # 1b. Task operation proposals — deterministic parse of
        # the canonical phrasings; slots stay verbatim for the TaskManager to ground.
        proposal = self._heuristic_task_proposal(message, context, matched_task_id)
        if proposal is not None:
            intent = {
                "create": MessageIntent.TASK_CREATE,
                "reassign": MessageIntent.TASK_REASSIGN,
                "deadline_change": MessageIntent.DEADLINE_CHANGE,
            }[proposal.operation]
            return ExtractionResult(
                is_actionable=False,
                claims=[],
                commitments=[],
                intent=intent,
                task_proposal=proposal,
                reasoning="Task operation proposal (awaiting grounding + approval)",
            )

        if matched_task_id is None:
            return ExtractionResult(
                is_actionable=False,
                claims=[],
                commitments=[],
                intent=MessageIntent.UNCERTAIN,
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
                is_actionable=True,
                claims=[],
                commitments=[commitment],
                intent=MessageIntent.HEALTH_REPORT,
                reasoning="Commitment made",
            )

        # 3. Check for resolved recovery status
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
                is_actionable=True,
                claims=[claim],
                commitments=[],
                intent=MessageIntent.HEALTH_REPORT,
                reasoning="Task recovered",
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
                is_actionable=True,
                claims=[claim],
                commitments=[],
                intent=MessageIntent.HEALTH_REPORT,
                reasoning="Risk/delay reported",
            )

        return ExtractionResult(
            is_actionable=False,
            claims=[],
            commitments=[],
            intent=MessageIntent.UNCERTAIN,
            reasoning="No actionable state found",
        )

    def _heuristic_task_proposal(
        self, message: str, context: MessageContext, matched_task_id: str | None
    ) -> TaskProposal | None:
        """Parses canonical task-operation phrasings into a verbatim-slot
        proposal; the TaskManager grounds slots against the directory and ledger."""
        create_match = _TASK_OP_CREATE_RE.search(message)
        reassign_match = _TASK_OP_REASSIGN_RE.search(message)
        deadline_match = _TASK_OP_DEADLINE_RE.search(message)
        if create_match is None and reassign_match is None and deadline_match is None:
            return None

        if create_match is not None:
            parts = re.split(r"任务[：:]", message, maxsplit=1)
            body = parts[1] if len(parts) == 2 else message
            segments = [seg.strip() for seg in re.split(r"[，,;；]", body) if seg.strip()]
            title = segments[0] if segments else None
            owner_name: str | None = None
            deadline_expr: str | None = None
            for seg in segments[1:]:
                if owner_name is None and "负责" in seg:
                    owner_name = seg.replace("由", "").replace("负责", "").strip() or None
                elif deadline_expr is None and _TIME_HINT_RE.search(seg):
                    deadline_expr = re.sub(r"(完成|交付|搞定)$", "", seg).strip() or seg
            if owner_name is None:
                owner_name = self._owner_token_after(message, context, 0)
            return TaskProposal(
                operation="create",
                title=title,
                owner_name=owner_name,
                task_ref=None,
                deadline_expr=deadline_expr,
            )

        if deadline_match is not None:
            task_ref = None
            if matched_task_id is not None:
                candidate = context.known_tasks.get(matched_task_id, matched_task_id)
                compact_message = message.lower().replace(" ", "")
                if (
                    matched_task_id.lower() in compact_message
                    or candidate.lower().replace(" ", "") in compact_message
                ):
                    task_ref = candidate
            deadline_expr = re.split(r"[，。;；\n]", message[deadline_match.end() :], maxsplit=1)[
                0
            ].strip()
            deadline_expr = re.sub(r"\s*(完成|交付|搞定)$", "", deadline_expr).strip()
            return TaskProposal(
                operation="deadline_change",
                title=None,
                owner_name=None,
                task_ref=task_ref,
                deadline_expr=deadline_expr or None,
            )

        assert reassign_match is not None
        owner_name = self._owner_token_after(message, context, reassign_match.end(), max_gap=3)
        if owner_name is None:
            # 「已交付」「提交」etc. contain a bare 交; without a member right
            # after the keyword this is not a reassignment — fall through to
            # the normal claim-extraction path.
            return None
        task_ref = None
        if matched_task_id is not None:
            task_ref = context.known_tasks.get(matched_task_id, matched_task_id)
        return TaskProposal(
            operation="reassign",
            title=None,
            owner_name=owner_name,
            task_ref=task_ref,
            deadline_expr=None,
        )

    @staticmethod
    def _owner_token_after(
        message: str, context: MessageContext, search_from: int, max_gap: int | None = None
    ) -> str | None:
        """Finds the member token (id or id suffix) nearest after search_from.

        With ``max_gap`` the token must start within that many characters of
        search_from — the adjacency discipline that keeps transfer verbs in
        完成/交付 phrasings from hijacking the message.
        """
        lower = message.lower()
        best: tuple[int, str] | None = None
        for member_id in context.known_members:
            tokens = {member_id.lower(), member_id.lower().split("_")[-1]}
            for token in tokens:
                if len(token) < 2:
                    continue
                idx = lower.find(token, max(0, search_from))
                if idx >= 0 and (best is None or idx < best[0]):
                    best = (idx, token)
        if best is None:
            return None
        if max_gap is not None and best[0] - max(0, search_from) > max_gap:
            return None
        return best[1]


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

"""Typed contracts for the layered intent recognition output (ADR-0008).

This module DEFINES the intent layer's output schema. The field descriptions
double as the contract shown to the LLM via ``IntentResult.model_json_schema()``
(3b): every description states the value rule the model must follow. Runtime
semantics are unchanged by the 3b schema hardening — ``health_status`` was
already restricted to the same four values by validator, now expressed as the
``HealthStatus`` StrEnum (pydantic coerces the same strings, rejects the rest).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from orgpilot.domain.enums import HealthStatus
from orgpilot.extraction.models import StrictModel


class IntentType(StrEnum):
    """Intent values from ADR-0008 section 2, covering the P0, P1, and P2 phases."""

    # P0 — handled end to end once the intent layer lands.
    REPORT_STATUS = "report_status"
    MAKE_COMMITMENT = "make_commitment"
    ANSWER_PENDING = "answer_pending"
    QUERY_STATUS = "query_status"
    REQUEST_SYNC = "request_sync"
    ACKNOWLEDGE = "acknowledge"
    CHITCHAT = "chitchat"
    HELP = "help"
    OUT_OF_SCOPE = "out_of_scope"
    # P1 — recognized but answered with "recorded, not supported yet"; no side effects.
    CORRECTION = "correction"
    DEPENDENCY_WAIT = "dependency_wait"
    UNAVAILABILITY = "unavailability"
    TEXT_APPROVAL = "text_approval"
    CORRECT_BOT = "correct_bot"
    # P2 — depends on the ADR-0007 PR channel.
    PROPOSE_PLAN_CHANGE = "propose_plan_change"


class SpeechAct(StrEnum):
    """Orthogonal attribute: what the utterance is doing, independent of topic."""

    STATEMENT = "statement"
    QUESTION = "question"
    HYPOTHETICAL = "hypothetical"
    IMPERATIVE = "imperative"


class Polarity(StrEnum):
    """Orthogonal attribute: whether the utterance affirms or negates its content."""

    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"


class Certainty(StrEnum):
    """Orthogonal attribute: committed assertion versus hedged guess."""

    CERTAIN = "certain"
    HEDGED = "hedged"


class IntentItem(StrictModel):
    """One actionable unit extracted from a message; a message may yield several."""

    intent: IntentType = Field(
        description=(
            "意图类型：report_status=汇报任务状态；make_commitment=承诺完成时间；"
            "answer_pending=回答机器人待回答的问题；query_status=查询任务状态；"
            "request_sync=请求全员进度同步；acknowledge=确认或应答；chitchat=纯闲聊；"
            "help=询问机器人用法；out_of_scope=超出处理能力；"
            "correction/dependency_wait/unavailability/text_approval/correct_bot/"
            "propose_plan_change 为 P1/P2 识别类（识别但不执行）"
        )
    )
    task_id: str | None = Field(
        default=None,
        description="涉及的任务 ID，必须取自上下文给出的已知任务列表；无法确定时留空，不要猜",
    )
    speech_act: SpeechAct = Field(
        description=(
            "语气：statement=陈述句；question=提问；hypothetical=假设（“如果……”）；"
            "imperative=请求或指令"
        )
    )
    polarity: Polarity = Field(
        description="极性：affirmative=肯定；negative=含否定词（没/不/别/尚未）"
    )
    certainty: Certainty = Field(
        description="确定度：certain=明确断言；hedged=含糊推测（应该/估计/可能/差不多）"
    )
    health_status: HealthStatus | None = Field(
        default=None,
        description=(
            "任务健康度枚举：unknown=未知；on_track=正常推进、已完成或阻塞已解除；"
            "at_risk=有风险但预计能在截止日前解决，或没给时间；delayed=明确延期，"
            "或预计完成时间晚于任务截止日。仅汇报状态时填写"
        ),
    )
    time_expression: str | None = Field(
        default=None,
        description="消息中出现的完成时间原话，必须是逐字子串（如“明天下午5点”）",
    )
    expected_completion: datetime | None = Field(
        default=None,
        description=(
            "预计完成/恢复时间，必须是带时区偏移的 ISO 8601 字符串"
            "（如 2026-10-16T17:00:00+08:00）；只填任务完成或阻塞解除的时间，"
            "开始/演示/定位原因/下次同步的时间不填，任务已完成也不填"
        ),
    )
    blocker: str | None = Field(
        default=None,
        description="阻塞根因短语；仅 health_status=delayed 时必须给出",
    )
    source_quote: str = Field(
        description="支撑本次判断的消息原文逐字子串，不得改写或拼接；整条消息可直接作为子串"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="本次判断的置信度，0 到 1；表达含糊或信息不足时降低",
    )

    @field_validator("expected_completion")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expected_completion must carry an explicit timezone")
        return value


class IntentResult(StrictModel):
    """Full intent-layer output for one message."""

    reasoning: str = Field(
        default="",
        description="一句话到两句话的判定理由；不超过两句话",
    )
    items: list[IntentItem] = Field(
        default_factory=list,
        description="从消息中抽取的动作条目列表；消息没有可处理内容时为空数组",
    )
    answers_pending: bool = Field(
        default=False,
        description=(
            "本条消息是否在回答对话状态中的待回答问题；是则至少给出一个 answer_pending 条目"
        ),
    )

    def to_summary(self) -> dict[str, Any]:
        """Compact view used by logs and evaluation reports."""
        return {
            "items": [item.model_dump(mode="json") for item in self.items],
            "answers_pending": self.answers_pending,
        }

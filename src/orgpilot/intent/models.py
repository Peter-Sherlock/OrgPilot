"""Typed contracts for the layered intent recognition output (ADR-0008).

This module only DEFINES the intent layer's output schema. Nothing in the
runtime pipeline consumes it yet: wiring it into routing, dialogue state, and
guardrails is a later step per ADR-0008's implementation order.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

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

    intent: IntentType
    task_id: str | None = None
    speech_act: SpeechAct
    polarity: Polarity
    certainty: Certainty
    health_status: str | None = None
    time_expression: str | None = Field(
        default=None,
        description="Verbatim time expression as it appeared in the message",
    )
    expected_completion: datetime | None = None
    blocker: str | None = None
    source_quote: str = Field(description="Exact verbatim substring of the message")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("expected_completion")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expected_completion must carry an explicit timezone")
        return value

    @field_validator("health_status")
    @classmethod
    def _known_health_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {"unknown", "on_track", "at_risk", "delayed"}
        if value not in allowed:
            raise ValueError(f"health_status must be one of {sorted(allowed)}, got {value!r}")
        return value


class IntentResult(StrictModel):
    """Full intent-layer output for one message."""

    items: list[IntentItem] = Field(default_factory=list)
    answers_pending: bool = Field(
        default=False,
        description="Whether this message answers the pending question in the dialogue state",
    )
    reasoning: str = ""

    def to_summary(self) -> dict[str, Any]:
        """Compact view used by logs and evaluation reports."""
        return {
            "items": [item.model_dump(mode="json") for item in self.items],
            "answers_pending": self.answers_pending,
        }

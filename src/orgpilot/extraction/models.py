"""Typed Pydantic v2 contracts for LLM claim extraction and confidence evaluation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from orgpilot.domain.enums import HealthStatus


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MessageContext(StrictModel):
    """Contextual metadata injected into the extraction prompt."""

    project_id: str
    actor_id: str
    occurred_at: datetime
    known_tasks: dict[str, str] = Field(
        default_factory=dict, description="Map of canonical task_id to title and status"
    )
    known_members: dict[str, str] = Field(
        default_factory=dict, description="Map of member_id to name and role"
    )
    conversation_history: tuple[str, ...] = ()


class ExtractedHealthClaim(StrictModel):
    """A structured health assessment extracted from human message text."""

    task_id: str
    health_status: HealthStatus
    expected_completion: datetime | None = None
    blocker: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source_quote: str = Field(
        description="Exact verbatim substring from message that justifies this claim"
    )


class ExtractedCommitment(StrictModel):
    """A structured promise or commitment extracted from human message text."""

    target_type: Literal["task"] = "task"
    target_id: str
    predicate: Literal["workflow_status", "health_status"]
    expected_value: str
    due_at: datetime | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source_quote: str = Field(description="Exact verbatim substring from message")


class ExtractionResult(StrictModel):
    """Structured LLM extraction outcome."""

    is_actionable: bool
    claims: list[ExtractedHealthClaim] = Field(default_factory=list)
    commitments: list[ExtractedCommitment] = Field(default_factory=list)
    reasoning: str = Field(default="", description="Chain-of-thought rationale")


class EvaluationSample(StrictModel):
    """A benchmark test sample for the extraction Gold Dataset."""

    sample_id: str
    message: str
    actor_id: str
    occurred_at: datetime
    expected_is_actionable: bool
    expected_claims: list[ExtractedHealthClaim] = Field(default_factory=list)
    expected_commitments: list[ExtractedCommitment] = Field(default_factory=list)


class ExtractionMetrics(StrictModel):
    """Aggregated quantitative evaluation metrics."""

    total_samples: int
    health_status_precision: float
    health_status_recall: float
    health_status_f1: float
    task_id_accuracy: float
    slot_datetime_accuracy: float
    false_alarm_rate: float
    grounding_valid_rate: float
    passed: bool

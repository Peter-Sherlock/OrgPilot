"""Typed Pydantic v2 contracts for LLM claim extraction and confidence evaluation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from orgpilot.domain.enums import HealthStatus, MessageIntent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MessageContext(StrictModel):
    """Contextual metadata injected into the extraction prompt."""

    project_id: str
    actor_id: str
    occurred_at: datetime
    source_ref: str | None = Field(
        default=None,
        description="Stable upstream message identifier used for event idempotency",
    )
    known_tasks: dict[str, str] = Field(
        default_factory=dict, description="Map of canonical task_id to title and status"
    )
    known_members: dict[str, str] = Field(
        default_factory=dict, description="Map of member_id to name and role"
    )
    reference_timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA timezone the team works in; relative times resolve against it",
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


class IntentHint(StrictModel):
    """Loose entity hints captured during intent routing for downstream slot extraction."""

    mentioned_member_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Known member ids referenced by the message (e.g. directive targets)",
    )
    mentioned_task_ids: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Known task ids referenced by the message",
    )
    raw_time_expr: str | None = Field(
        default=None,
        description="Verbatim deadline-ish time expression, e.g. '明天上午12点' (unresolved)",
    )


class IntentResult(StrictModel):
    """Outcome of the pre-extraction intent routing pass."""

    intent: MessageIntent
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    method: Literal["rule", "llm"] = "rule"
    authority_ok: bool | None = Field(
        default=None,
        description="Whether the actor's role may exercise this intent (None = role-insensitive)",
    )
    reasoning: str = ""
    hints: IntentHint = Field(default_factory=IntentHint)


class TaskProposal(StrictModel):
    """LLM-proposed task operation awaiting grounding checks and human approval.

    All string fields are verbatim quotes from the message (grounding rule):
    resolution against the member directory and task ledger happens in the
    TaskManager, never in the model output.
    """

    operation: Literal["create", "reassign", "deadline_change"] = Field(
        description="Requested task operation"
    )
    title: str | None = Field(default=None, description="Proposed task title, verbatim")
    owner_name: str | None = Field(default=None, description="New owner as written, verbatim")
    task_ref: str | None = Field(
        default=None, description="Existing task title/id to reassign or reschedule, verbatim"
    )
    deadline_expr: str | None = Field(
        default=None, description="Raw deadline expression, e.g. '周五前' (unresolved)"
    )


class ExtractionResult(StrictModel):
    """Structured LLM extraction outcome."""

    is_actionable: bool
    claims: list[ExtractedHealthClaim] = Field(default_factory=list)
    commitments: list[ExtractedCommitment] = Field(default_factory=list)
    intent: MessageIntent | None = Field(
        default=None,
        description="Routing-level intent; None on results produced before intent routing",
    )
    hints: IntentHint | None = Field(
        default=None,
        description="Entity hints from intent routing (targets, tasks, raw time expression)",
    )
    task_proposal: TaskProposal | None = Field(
        default=None,
        description="Task create/reassign/deadline proposal; set only for those intents",
    )
    reasoning: str = Field(default="", description="Brief extraction decision summary")


class EvaluationSample(StrictModel):
    """A benchmark test sample for the extraction Gold Dataset."""

    sample_id: str
    message: str
    actor_id: str
    occurred_at: datetime
    expected_is_actionable: bool
    expected_claims: list[ExtractedHealthClaim] = Field(default_factory=list)
    expected_commitments: list[ExtractedCommitment] = Field(default_factory=list)
    expected_intent: MessageIntent | None = None


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
    intent_accuracy: float = 1.0
    passed: bool

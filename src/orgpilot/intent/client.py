"""Intent client abstraction and implementations (ADR-0008 layer 1).

Three implementations exist:

- ``HeuristicIntentClient`` wraps the existing ``MockLLMClient`` keyword rules and
  maps ``ExtractionResult`` to ``IntentResult``. It is the default when
  ``ORGPILOT_LLM_PROVIDER=mock`` so demos and CI keep working; its quality is
  explicitly NOT a goal.
- ``ScriptedIntentClient`` serves unit tests: preset message → result mappings,
  and can simulate timeouts, exceptions, and invalid (non-IntentResult) output.
- The oracle client lives in the evaluation code path (``orgpilot.intent.oracle``)
  and is intentionally NOT importable through runtime configuration.

Every classify call goes through :func:`classify_with_timeout` so a slow client
can never block the event loop beyond the configured budget; synchronous work is
pushed to a thread via ``asyncio.to_thread``.
"""

import asyncio
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, field_validator

from orgpilot.config import DEFAULT_INTENT_TIMEOUT_SECONDS
from orgpilot.extraction.client import LLMClient, MockLLMClient
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import MessageContext, StrictModel
from orgpilot.intent.models import (
    Certainty,
    IntentItem,
    IntentResult,
    IntentType,
    Polarity,
    SpeechAct,
)


class TaskSnapshot(StrictModel):
    """Read-only view of one known task handed to the intent client."""

    task_id: str
    title: str
    owner_id: str
    deadline: datetime | None = None
    health_status: str = "unknown"


class PendingQuestionView(StrictModel):
    """The clarification the bot is waiting on, with every task it targets."""

    question: str
    task_ids: list[str] = Field(default_factory=list)
    asked_rounds: int = 0


class PendingConfirmationView(StrictModel):
    """The sync confirmation card the sender can confirm or cancel."""

    initiated_by: str


class IntentContext(StrictModel):
    """Everything layer 1 may look at for one incoming message."""

    project_id: str
    message: str
    occurred_at: datetime
    sender_id: str
    sender_role: str
    sender_display_name: str
    sender_task_ids: list[str] = Field(default_factory=list)
    known_tasks: list[TaskSnapshot] = Field(default_factory=list)
    pending_question: PendingQuestionView | None = None
    pending_confirmation: PendingConfirmationView | None = None
    conversation_history: tuple[str, ...] = ()

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("IntentContext.occurred_at must carry an explicit timezone")
        return value


@runtime_checkable
class IntentClient(Protocol):
    """Anything that can turn one message plus context into an IntentResult."""

    async def classify(self, message: str, context: IntentContext) -> IntentResult: ...


async def classify_with_timeout(
    client: IntentClient,
    message: str,
    context: IntentContext,
    timeout_seconds: float = DEFAULT_INTENT_TIMEOUT_SECONDS,
) -> IntentResult:
    """Runs one classify call under ``asyncio.wait_for``; raises ``TimeoutError``."""
    return await asyncio.wait_for(
        client.classify(message, context), timeout=timeout_seconds
    )


class HeuristicIntentClient:
    """Bridges the existing keyword-rules extractor into the intent schema.

    Quality is explicitly not a goal (ADR-0008): this exists so ORGPILOT_LLM_PROVIDER=mock
    keeps a working end-to-end demo. The synchronous MockLLMClient runs in a worker
    thread so the event loop is never blocked.
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self._extractor = ClaimExtractor(llm_client=llm_client or MockLLMClient())

    async def classify(self, message: str, context: IntentContext) -> IntentResult:
        def _run_extraction() -> Any:
            # Scope to the sender's own tasks when known: a probe answer like
            # "预计排查需要到明天下午5点" refers to the probed task, and the old
            # coordinator path extracted with exactly this scoping (3a.1).
            scoped = [
                task for task in context.known_tasks if task.task_id in context.sender_task_ids
            ] or context.known_tasks
            message_context = MessageContext(
                project_id=context.project_id,
                actor_id=context.sender_id,
                occurred_at=context.occurred_at,
                known_tasks={task.task_id: task.title for task in scoped},
                known_members={},
            )
            return self._extractor.extract_from_message(message, message_context)[0]

        extraction = await asyncio.to_thread(_run_extraction)

        items: list[IntentItem] = []
        for claim in extraction.claims:
            items.append(
                IntentItem(
                    intent=IntentType.REPORT_STATUS,
                    task_id=claim.task_id,
                    speech_act=SpeechAct.STATEMENT,
                    polarity=Polarity.AFFIRMATIVE,
                    certainty=Certainty.CERTAIN,
                    health_status=str(
                        claim.health_status.value
                        if hasattr(claim.health_status, "value")
                        else claim.health_status
                    ),
                    expected_completion=claim.expected_completion,
                    blocker=claim.blocker,
                    source_quote=claim.source_quote,
                    confidence=claim.confidence,
                )
            )
        for commitment in extraction.commitments:
            items.append(
                IntentItem(
                    intent=IntentType.MAKE_COMMITMENT,
                    task_id=commitment.target_id,
                    speech_act=SpeechAct.STATEMENT,
                    polarity=Polarity.AFFIRMATIVE,
                    certainty=Certainty.CERTAIN,
                    expected_completion=commitment.due_at,
                    source_quote=commitment.source_quote,
                    confidence=commitment.confidence,
                )
            )
        if not items:
            items.append(
                IntentItem(
                    intent=IntentType.CHITCHAT,
                    speech_act=SpeechAct.STATEMENT,
                    polarity=Polarity.AFFIRMATIVE,
                    certainty=Certainty.CERTAIN,
                    source_quote=message[:50],
                    confidence=0.5,
                )
            )

        return IntentResult(
            items=items,
            answers_pending=(
                context.pending_question is not None and extraction.is_actionable
            ),
            reasoning="heuristic: MockLLMClient keyword rules mapped to intents",
        )


class ScriptedIntentClient:
    """Test double: preset message → outcome, including failure simulations.

    Values in ``responses`` may be ``IntentResult`` (normal), an ``Exception``
    instance (raised), an ``asyncio.TimeoutError`` (raised), or any other object
    (returned as-is to simulate schema-invalid output).
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        default: Any = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.responses = responses or {}
        self.default = default
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, IntentContext]] = []

    async def classify(self, message: str, context: IntentContext) -> Any:
        self.calls.append((message, context))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        outcome = self.responses.get(message, self.default)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            raise ValueError(f"no scripted intent outcome for message: {message!r}")
        return outcome


__all__ = [
    "HeuristicIntentClient",
    "IntentClient",
    "IntentContext",
    "PendingConfirmationView",
    "PendingQuestionView",
    "ScriptedIntentClient",
    "TaskSnapshot",
    "classify_with_timeout",
]

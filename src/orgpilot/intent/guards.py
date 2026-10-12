"""Layer-2 deterministic guardrails (ADR-0008 §3). Pure functions, no I/O.

Each intent item passes through :func:`evaluate_item_guards` in a fixed order
(grounding → task → mood → hedging → confidence → time → role → tier) and comes
back with one verdict:

- ``KEEP``: continue to the intent handler;
- ``DROP``: discard the item silently (logged by the caller);
- ``ASK``: ask the sender a follow-up question (text carried in the outcome);
- ``QUERY``: treat the item as a status query instead of a claim;
- ``NOTED`` / ``UNSUPPORTED`` / ``NO_PERMISSION``: reply accordingly, no claim.

Design decisions recorded in ADR-0008's implementation log:
- time comparison is DATE-ONLY because TemporalResolver does not support "半" or
  Chinese numerals; comparing hours would produce constant false clarifications;
- a negative-polarity report_status WITHOUT a health status asserts nothing
  (e.g. "接口我还没碰") and is dropped rather than turned into a claim.
"""

from dataclasses import dataclass
from datetime import datetime

from orgpilot.extraction.verifier import GroundingVerifier, TemporalResolver
from orgpilot.intent.client import IntentContext
from orgpilot.intent.models import Certainty, IntentItem, IntentType, Polarity, SpeechAct

P1_P2_INTENTS = {
    IntentType.CORRECTION,
    IntentType.DEPENDENCY_WAIT,
    IntentType.UNAVAILABILITY,
    IntentType.TEXT_APPROVAL,
    IntentType.CORRECT_BOT,
    IntentType.PROPOSE_PLAN_CHANGE,
}

# Intents that cannot be processed without knowing which task they concern.
TASK_REQUIRED_INTENTS = {IntentType.REPORT_STATUS, IntentType.MAKE_COMMITMENT}

# Only claim-worthy items risk a side effect; low-confidence smalltalk must not nag.
CONFIDENCE_GATED_INTENTS = TASK_REQUIRED_INTENTS | {IntentType.ANSWER_PENDING}

DEFAULT_CONFIDENCE_THRESHOLD = 0.6


@dataclass(frozen=True)
class GuardOutcome:
    """Result of guard-checking one intent item."""

    verdict: str  # one of KEEP/DROP/ASK/QUERY/NOTED/UNSUPPORTED/NO_PERMISSION
    ask_question: str | None = None
    reason: str = ""

    @property
    def asks(self) -> bool:
        return self.verdict == "ASK"


def check_grounding(item: IntentItem, message: str) -> GuardOutcome:
    """source_quote must be a verbatim substring of the message."""
    if not GroundingVerifier().verify_quote(message, item.source_quote):
        return GuardOutcome(
            verdict="DROP",
            reason=f"grounding failed: quote {item.source_quote!r} not in message",
        )
    return GuardOutcome(verdict="KEEP", reason="grounding ok")


def check_task_reference(item: IntentItem, context: IntentContext) -> GuardOutcome:
    """Task-requiring items need a known task, otherwise ask which one (R7)."""
    pending_tasks = (
        list(context.pending_question.task_ids) if context.pending_question else []
    )
    needs_task = item.intent in TASK_REQUIRED_INTENTS or (
        item.intent == IntentType.ANSWER_PENDING and not pending_tasks
    )
    if not needs_task:
        return GuardOutcome(verdict="KEEP", reason="task not required")
    if item.task_id is None:
        # answer_pending inherits the pending question's target tasks when present.
        if item.intent == IntentType.ANSWER_PENDING and pending_tasks:
            return GuardOutcome(verdict="KEEP", reason="tasks inherited from pending question")
        return GuardOutcome(verdict="ASK", ask_question=_task_question(context), reason="no task")
    known_ids = {task.task_id for task in context.known_tasks}
    if item.task_id not in known_ids:
        return GuardOutcome(
            verdict="ASK", ask_question=_task_question(context), reason="unknown task"
        )
    return GuardOutcome(verdict="KEEP", reason="task ok")


def _task_question(context: IntentContext) -> str:
    titles: list[str] = []
    for task in context.known_tasks:
        if task.task_id in context.sender_task_ids:
            titles.append(task.title)
    if not titles:
        titles = [task.title for task in context.known_tasks]
    listing = "、".join(titles) if titles else "（暂无）"
    return f"是指哪个任务？您相关的任务有：{listing}"


def check_speech_act(item: IntentItem) -> GuardOutcome:
    """Only statements produce claims; questions become queries, hypotheticals noted."""
    if item.speech_act == SpeechAct.HYPOTHETICAL:
        return GuardOutcome(verdict="NOTED", reason="hypothetical never materializes a claim")
    if item.speech_act == SpeechAct.QUESTION and item.intent in TASK_REQUIRED_INTENTS:
        return GuardOutcome(verdict="QUERY", reason="question about status becomes a query")
    return GuardOutcome(verdict="KEEP", reason="mood ok")


def check_hedging(item: IntentItem) -> GuardOutcome:
    """Hedged status without a time asks for one (R6); with a time it stays a claim."""
    if (
        item.intent == IntentType.REPORT_STATUS
        and item.certainty == Certainty.HEDGED
        and not item.time_expression
    ):
        return GuardOutcome(
            verdict="ASK",
            ask_question="大概预计什么时候能完成或恢复？（例如：明天下午5点）",
            reason="hedged without a time",
        )
    return GuardOutcome(verdict="KEEP", reason="certainty ok")


def check_confidence(
    item: IntentItem, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
) -> GuardOutcome:
    if item.intent not in CONFIDENCE_GATED_INTENTS:
        return GuardOutcome(verdict="KEEP", reason="confidence gate not applicable")
    if item.confidence < threshold:
        return GuardOutcome(
            verdict="ASK",
            ask_question="我不太确定理解得对不对：您是在汇报任务状态吗？",
            reason=f"confidence {item.confidence:.2f} below {threshold:.2f}",
        )
    return GuardOutcome(verdict="KEEP", reason="confidence ok")


def check_time_expression(
    item: IntentItem, message: str, occurred_at: datetime
) -> GuardOutcome:
    """time_expression must be verbatim, and its parsed DATE must match the slot.

    Date-only comparison on purpose (ADR-0008 implementation log): the resolver
    cannot parse "半"/Chinese numerals, so comparing hours would over-ask.
    """
    if not item.time_expression:
        return GuardOutcome(verdict="KEEP", reason="no time expression")
    if item.time_expression not in message:
        return GuardOutcome(
            verdict="ASK",
            ask_question="您提到的时间我想确认下：预计什么时候完成或恢复？",
            reason="time expression is not a verbatim substring",
        )
    if item.expected_completion is not None:
        parsed = TemporalResolver.resolve_relative_time(item.time_expression, occurred_at)
        if parsed is not None and parsed.date() != item.expected_completion.date():
            return GuardOutcome(
                verdict="ASK",
                ask_question=(
                    f"您说的“{item.time_expression}”和预计完成时间对不上，"
                    "请确认预计什么时候完成？"
                ),
                reason="parsed date disagrees with the resolved completion",
            )
    return GuardOutcome(verdict="KEEP", reason="time ok")


def check_role(item: IntentItem, context: IntentContext) -> GuardOutcome:
    # A NEGATIVE request_sync ("不用同步进度了") declines a sync rather than
    # attempting one, so the PM-only gate applies to affirmative requests only.
    if (
        item.intent == IntentType.REQUEST_SYNC
        and item.polarity == Polarity.AFFIRMATIVE
        and context.sender_role != "pm"
    ):
        return GuardOutcome(
            verdict="NO_PERMISSION", reason="request_sync is PM-only (ADR-0008 §4)"
        )
    return GuardOutcome(verdict="KEEP", reason="role ok")


def check_tier(item: IntentItem) -> GuardOutcome:
    if item.intent in P1_P2_INTENTS:
        return GuardOutcome(verdict="UNSUPPORTED", reason="P1/P2 recognized but not handled")
    return GuardOutcome(verdict="KEEP", reason="tier ok")


def evaluate_item_guards(
    item: IntentItem,
    message: str,
    context: IntentContext,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> GuardOutcome:
    """Runs all guards in ADR order and returns the first non-KEEP verdict."""
    checks = (
        check_grounding(item, message),
        check_task_reference(item, context),
        check_speech_act(item),
        _check_negative_without_health(item),
        check_hedging(item),
        check_confidence(item, confidence_threshold),
        check_time_expression(item, message, context.occurred_at),
        check_role(item, context),
        check_tier(item),
    )
    for outcome in checks:
        if outcome.verdict != "KEEP":
            return outcome
    return GuardOutcome(verdict="KEEP", reason="all guards passed")


def _check_negative_without_health(item: IntentItem) -> GuardOutcome:
    if (
        item.intent == IntentType.REPORT_STATUS
        and item.polarity == Polarity.NEGATIVE
        and item.health_status is None
    ):
        return GuardOutcome(
            verdict="DROP",
            reason="negative statement without a health status asserts nothing",
        )
    return GuardOutcome(verdict="KEEP", reason="health ok")

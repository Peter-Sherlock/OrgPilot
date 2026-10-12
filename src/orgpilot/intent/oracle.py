"""Oracle intent client: replays dataset annotations as intent results (EVAL ONLY).

This client lives purely on the evaluation code path — it is never selectable
through runtime configuration (the runtime router only knows the heuristic
client until ADR-0008 step 3b lands a real model client). Because the oracle
feeds the pipeline the annotated intents, the layered pipeline's score against
the oracle is the pipeline's UPPER BOUND: everything below it once a real model
arrives is model error, not pipeline error.
"""

from typing import TYPE_CHECKING

from orgpilot.intent.client import IntentContext
from orgpilot.intent.models import IntentItem, IntentResult

ORACLE_CONFIDENCE = 0.95

if TYPE_CHECKING:
    from orgpilot.intent.evaluator import EvalSample


class OracleIntentClient:
    """Maps (sender, message) to the sample's annotated intents."""

    def __init__(self, samples: "list[EvalSample]") -> None:
        self._by_sender_message: dict[tuple[str, str], EvalSample] = {}
        for sample in samples:
            key = (sample.sender, sample.message)
            if key in self._by_sender_message:
                raise ValueError(f"oracle key collision for {key!r}")
            self._by_sender_message[key] = sample
        self.calls = 0

    async def classify(self, message: str, context: IntentContext) -> IntentResult:
        self.calls += 1
        sample = self._by_sender_message.get((context.sender_id, message))
        if sample is None:
            raise KeyError(
                f"no annotated sample for sender/message: {context.sender_id!r}/{message!r}"
            )
        return self.build_result(sample)

    @staticmethod
    def build_result(sample: "EvalSample") -> IntentResult:
        """Builds IntentResult from expected.intents; completion from behavior claims."""
        completions = {
            claim.task_id: claim.expected_completion for claim in sample.expected.behavior.claims
        }
        items: list[IntentItem] = []
        for expected_item in sample.expected.intents.items:
            completion = expected_item.expected_completion
            if completion is None and expected_item.task_id in completions:
                completion = completions[expected_item.task_id]
            items.append(
                IntentItem(
                    intent=expected_item.intent,
                    task_id=expected_item.task_id,
                    speech_act=expected_item.speech_act,
                    polarity=expected_item.polarity,
                    certainty=expected_item.certainty,
                    health_status=expected_item.health_status,
                    time_expression=expected_item.time_expression,
                    expected_completion=completion,
                    blocker=expected_item.blocker,
                    source_quote=expected_item.source_quote or sample.message,
                    confidence=ORACLE_CONFIDENCE,
                )
            )
        return IntentResult(
            items=items,
            answers_pending=bool(sample.expected.intents.answers_pending),
            reasoning="oracle: annotated intents (pipeline upper bound, not a model)",
        )

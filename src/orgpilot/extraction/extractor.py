"""Core ClaimExtractor bridging unstructured text to verified OrgEvents."""

from orgpilot.events.models import (
    CommitmentMadeEvent,
    CommitmentMadePayload,
    EventSource,
    OrgEvent,
    TaskHealthReportedEvent,
    TaskHealthReportedPayload,
)
from orgpilot.extraction.client import LLMClient, MockLLMClient
from orgpilot.extraction.models import ExtractionResult, MessageContext
from orgpilot.extraction.prompts import SYSTEM_PROMPT, build_extraction_prompt
from orgpilot.extraction.verifier import GroundingVerifier


class ClaimExtractor:
    """Extracts structured claims and commitments from natural language text with grounding."""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        verifier: GroundingVerifier | None = None,
    ) -> None:
        self.llm_client = llm_client or MockLLMClient()
        self.verifier = verifier or GroundingVerifier()

    def extract_from_message(
        self, message: str, context: MessageContext
    ) -> tuple[ExtractionResult, list[OrgEvent]]:
        """Analyzes a message, verifies grounding, and generates typed OrgEvents."""
        user_prompt = build_extraction_prompt(message, context)
        raw_result = self.llm_client.extract(SYSTEM_PROMPT, user_prompt, message, context)

        # Enforce grounding and filter hallucinated claims
        verified_result = self.verifier.filter_and_verify(raw_result, message, context)

        events: list[OrgEvent] = []
        event_counter = 1
        ts = int(context.occurred_at.timestamp())

        for claim in verified_result.claims:
            event_id = f"evt-extracted-claim-{context.actor_id}-{ts}-{event_counter}"
            event_counter += 1
            event = TaskHealthReportedEvent(
                project_id=context.project_id,
                event_id=event_id,
                event_type="task.health_reported",
                source=EventSource.MESSAGE,
                source_ref=f"message:{context.actor_id}:{ts}",
                actor_id=context.actor_id,
                occurred_at=context.occurred_at,
                received_at=context.occurred_at,
                payload=TaskHealthReportedPayload(
                    task_id=claim.task_id,
                    health_status=claim.health_status,
                    expected_completion=claim.expected_completion,
                    blocker=claim.blocker,
                    confidence=claim.confidence,
                ),
            )
            events.append(event)

        for commitment in verified_result.commitments:
            commitment_id = f"commitment-{context.actor_id}-{commitment.target_id}-{ts}"
            event_id = f"evt-extracted-cmt-{ts}-{event_counter}"
            event_counter += 1
            event = CommitmentMadeEvent(
                project_id=context.project_id,
                event_id=event_id,
                event_type="commitment.made",
                source=EventSource.MESSAGE,
                source_ref=f"message:{context.actor_id}:{int(context.occurred_at.timestamp())}",
                actor_id=context.actor_id,
                occurred_at=context.occurred_at,
                received_at=context.occurred_at,
                payload=CommitmentMadePayload(
                    commitment_id=commitment_id,
                    target_type="task",
                    target_id=commitment.target_id,
                    predicate=commitment.predicate,
                    expected_value=commitment.expected_value,
                    due_at=commitment.due_at,
                ),
            )
            events.append(event)

        return verified_result, events

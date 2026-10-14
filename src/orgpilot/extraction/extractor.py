"""Core ClaimExtractor bridging unstructured text to verified OrgEvents."""

from orgpilot.events.models import OrgEvent
from orgpilot.extraction.client import LLMClient, MockLLMClient
from orgpilot.extraction.event_builder import (
    build_commitment_made_event,
    build_health_reported_event,
    message_identity,
)
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
        message_key, source_ref = message_identity(
            context.project_id,
            context.actor_id,
            context.occurred_at,
            message,
            context.source_ref,
        )

        for claim in verified_result.claims:
            event = build_health_reported_event(
                project_id=context.project_id,
                actor_id=context.actor_id,
                occurred_at=context.occurred_at,
                source_ref=source_ref,
                event_id=f"evt-extracted-claim-{message_key}-{event_counter}",
                task_id=claim.task_id,
                health_status=claim.health_status,
                expected_completion=claim.expected_completion,
                blocker=claim.blocker,
                confidence=claim.confidence,
            )
            event_counter += 1
            events.append(event)

        for commitment in verified_result.commitments:
            event = build_commitment_made_event(
                project_id=context.project_id,
                actor_id=context.actor_id,
                occurred_at=context.occurred_at,
                source_ref=source_ref,
                event_id=f"evt-extracted-cmt-{message_key}-{event_counter}",
                commitment_id=f"commitment-{commitment.target_id}-{message_key}",
                target_id=commitment.target_id,
                predicate=commitment.predicate,
                expected_value=commitment.expected_value,
                due_at=commitment.due_at,
            )
            event_counter += 1
            events.append(event)

        return verified_result, events

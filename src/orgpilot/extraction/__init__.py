"""LLM-assisted structured claim extraction, confidence scoring, and benchmark evaluation."""

from orgpilot.extraction.client import (
    AnthropicCompatibleLLMClient,
    LLMClient,
    MockLLMClient,
    RecordedReplayClient,
)
from orgpilot.extraction.evaluator import evaluate_extractor, load_gold_dataset
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import (
    EvaluationSample,
    ExtractedCommitment,
    ExtractedHealthClaim,
    ExtractionMetrics,
    ExtractionResult,
    MessageContext,
)
from orgpilot.extraction.verifier import GroundingVerifier, TemporalResolver

__all__ = [
    "ClaimExtractor",
    "AnthropicCompatibleLLMClient",
    "EvaluationSample",
    "ExtractedCommitment",
    "ExtractedHealthClaim",
    "ExtractionMetrics",
    "ExtractionResult",
    "GroundingVerifier",
    "LLMClient",
    "MessageContext",
    "MockLLMClient",
    "RecordedReplayClient",
    "TemporalResolver",
    "evaluate_extractor",
    "load_gold_dataset",
]

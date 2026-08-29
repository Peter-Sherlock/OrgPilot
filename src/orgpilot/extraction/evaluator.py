"""Benchmark evaluator calculating Precision, Recall, F1, and Grounding metrics."""

from pathlib import Path

import yaml

from orgpilot.domain.enums import MessageIntent
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import (
    EvaluationSample,
    ExtractedCommitment,
    ExtractedHealthClaim,
    ExtractionMetrics,
    MessageContext,
)


def load_gold_dataset(path: Path) -> list[EvaluationSample]:
    """Loads benchmark evaluation samples from a YAML dataset."""
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    samples = []
    for item in data.get("samples", []):
        claims = [ExtractedHealthClaim.model_validate(c) for c in item.get("expected_claims", [])]
        commitments = [
            ExtractedCommitment.model_validate(c) for c in item.get("expected_commitments", [])
        ]
        expected_intent = item.get("expected_intent")
        samples.append(
            EvaluationSample(
                sample_id=item["sample_id"],
                message=item["message"],
                actor_id=item.get("actor_id", "alice"),
                occurred_at=item["occurred_at"],
                expected_is_actionable=item.get("expected_is_actionable", True),
                expected_claims=claims,
                expected_commitments=commitments,
                expected_intent=MessageIntent(expected_intent) if expected_intent else None,
            )
        )
    return samples


def evaluate_extractor(
    extractor: ClaimExtractor,
    samples: list[EvaluationSample],
    known_tasks: dict[str, str] | None = None,
    known_members: dict[str, str] | None = None,
) -> ExtractionMetrics:
    """Runs extraction over benchmark samples and computes metrics."""
    tasks = known_tasks or {"backend_api": "Backend API", "frontend_integration": "Frontend"}
    members = known_members or {"alice": "engineer", "bob": "engineer", "carol": "pm"}

    tp_status = 0
    fp_status = 0
    fn_status = 0

    task_matches = 0
    total_expected_claims = 0

    datetime_matches = 0
    total_expected_datetimes = 0

    false_alarms = 0
    total_non_actionable = 0

    grounded_quotes = 0
    total_extracted_quotes = 0

    intent_matches = 0
    total_intent_samples = 0

    for sample in samples:
        context = MessageContext(
            project_id="eval-proj",
            actor_id=sample.actor_id,
            occurred_at=sample.occurred_at,
            known_tasks=tasks,
            known_members=members,
        )

        result, _ = extractor.extract_from_message(sample.message, context)

        # Intent accuracy on samples that declare an expected intent
        if sample.expected_intent is not None:
            total_intent_samples += 1
            if result.intent == sample.expected_intent:
                intent_matches += 1

        # Check false alarms on non-actionable samples
        if not sample.expected_is_actionable:
            total_non_actionable += 1
            if result.is_actionable:
                false_alarms += 1

        # Check grounding for all extracted quotes
        for c in result.claims:
            total_extracted_quotes += 1
            if extractor.verifier.verify_quote(sample.message, c.source_quote):
                grounded_quotes += 1
        for c in result.commitments:
            total_extracted_quotes += 1
            if extractor.verifier.verify_quote(sample.message, c.source_quote):
                grounded_quotes += 1

        # Evaluate claims
        actual_statuses = [c.health_status for c in result.claims]
        expected_statuses = [c.health_status for c in sample.expected_claims]

        for exp_s in expected_statuses:
            if exp_s in actual_statuses:
                tp_status += 1
                actual_statuses.remove(exp_s)
            else:
                fn_status += 1
        fp_status += len(actual_statuses)

        # Evaluate task ID and datetimes
        for exp_c in sample.expected_claims:
            total_expected_claims += 1
            matching_actual = next((c for c in result.claims if c.task_id == exp_c.task_id), None)
            if matching_actual:
                task_matches += 1
                if exp_c.expected_completion is not None:
                    total_expected_datetimes += 1
                    if matching_actual.expected_completion == exp_c.expected_completion:
                        datetime_matches += 1

    precision = tp_status / (tp_status + fp_status) if (tp_status + fp_status) > 0 else 1.0
    recall = tp_status / (tp_status + fn_status) if (tp_status + fn_status) > 0 else 1.0
    f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 1.0

    task_acc = task_matches / total_expected_claims if total_expected_claims > 0 else 1.0
    dt_acc = datetime_matches / total_expected_datetimes if total_expected_datetimes > 0 else 1.0
    fa_rate = false_alarms / total_non_actionable if total_non_actionable > 0 else 0.0
    grounding_rate = grounded_quotes / total_extracted_quotes if total_extracted_quotes > 0 else 1.0
    intent_acc = intent_matches / total_intent_samples if total_intent_samples > 0 else 1.0

    passed = (
        f1 >= 0.90
        and fa_rate <= 0.05
        and grounding_rate == 1.0
        and (intent_acc >= 0.90 or total_intent_samples == 0)
    )

    return ExtractionMetrics(
        total_samples=len(samples),
        health_status_precision=round(precision, 4),
        health_status_recall=round(recall, 4),
        health_status_f1=round(f1, 4),
        task_id_accuracy=round(task_acc, 4),
        slot_datetime_accuracy=round(dt_acc, 4),
        false_alarm_rate=round(fa_rate, 4),
        grounding_valid_rate=round(grounding_rate, 4),
        intent_accuracy=round(intent_acc, 4),
        passed=passed,
    )

"""Tests for extraction benchmark evaluator and gold dataset."""

from pathlib import Path

from orgpilot.extraction.evaluator import evaluate_extractor, load_gold_dataset
from orgpilot.extraction.extractor import ClaimExtractor


def test_gold_dataset_evaluation_details() -> None:
    dataset_path = Path("evals/extraction/gold_dataset.yaml")
    samples = load_gold_dataset(dataset_path)
    extractor = ClaimExtractor()
    tasks = {"backend_api": "Backend API", "frontend_integration": "Frontend"}
    members = {"alice": "engineer", "bob": "engineer", "carol": "pm"}

    metrics = evaluate_extractor(extractor, samples, known_tasks=tasks, known_members=members)

    assert metrics.total_samples == 34
    assert metrics.passed is True
    assert metrics.health_status_f1 == 1.0
    assert metrics.task_id_accuracy == 1.0
    assert metrics.slot_datetime_accuracy == 1.0
    assert metrics.false_alarm_rate == 0.0
    assert metrics.grounding_valid_rate == 1.0
    assert metrics.intent_accuracy == 1.0

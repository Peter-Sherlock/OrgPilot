"""Integrity tests for the committed intent evaluation datasets (ADR-0008 §8)."""

from pathlib import Path

import pytest

from orgpilot.intent.evaluator import (
    category_minimum_violations,
    load_intent_project,
    load_intent_samples,
    validate_samples_against_project,
)

EVAL_DIR = Path(__file__).resolve().parents[1] / "evals" / "intent"


@pytest.fixture(scope="module")
def project():
    return load_intent_project(EVAL_DIR / "project.yaml")


def test_project_fixture_shape(project) -> None:
    assert project.project_id == "intent-eval-project"
    roles = [member["role"] for member in project.members]
    assert roles.count("pm") == 1
    assert len(roles) == 4
    assert len(project.tasks) == 5
    # Confusable name pairs required by the task brief
    titles = {task["title"] for task in project.tasks}
    assert {"支付SDK接入", "支付全链路压测", "收银台页面改版", "商户后台管理端"} <= titles
    owners = {task["owner_id"] for task in project.tasks}
    assert owners <= project.member_ids
    for task in project.tasks:
        for dependency in task.get("dependencies", []):
            assert dependency in {t["task_id"] for t in project.tasks}


def test_dataset_totals_and_split_ratio() -> None:
    dev = load_intent_samples(EVAL_DIR / "dev.yaml")
    test = load_intent_samples(EVAL_DIR / "test.yaml")
    assert len(dev) + len(test) >= 120
    ratio = len(dev) / (len(dev) + len(test))
    assert 0.55 <= ratio <= 0.70
    dev_ids = {sample.id for sample in dev}
    test_ids = {sample.id for sample in test}
    assert not dev_ids & test_ids


def test_category_minimums_across_splits() -> None:
    combined = load_intent_samples(EVAL_DIR / "dev.yaml") + load_intent_samples(
        EVAL_DIR / "test.yaml"
    )
    assert category_minimum_violations(combined) == []


def test_samples_reference_fixture_entities(project) -> None:
    for split in ("dev", "test"):
        samples = load_intent_samples(EVAL_DIR / f"{split}.yaml")
        assert validate_samples_against_project(samples, project) == []


def test_min_category_counts_reported() -> None:
    dev = load_intent_samples(EVAL_DIR / "dev.yaml")
    counts: dict[str, int] = {}
    for sample in dev:
        counts[sample.category] = counts.get(sample.category, 0) + 1
    assert counts["time_chinese"] == 6
    assert counts["multi_turn_topic_shift"] == 3
    assert counts["sync_non_pm"] == 2

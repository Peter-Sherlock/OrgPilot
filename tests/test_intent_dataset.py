"""Integrity tests for the committed intent evaluation datasets (ADR-0008 §8)."""

import hashlib
from pathlib import Path

import pytest

from orgpilot.intent.evaluator import (
    category_minimum_violations,
    load_intent_project,
    load_intent_samples,
    validate_samples_against_project,
)

EVAL_DIR = Path(__file__).resolve().parents[1] / "evals" / "intent"

# The test split is frozen (BASELINE.md): any change requires prior approval.
FROZEN_TEST_SHA256 = "1da96ed7848a964c200f7e52dbbed9bb412bff51b04d6caba59a8151c0ff9dc4"


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


def test_frozen_test_split_unchanged_without_approval() -> None:
    actual = hashlib.sha256((EVAL_DIR / "test.yaml").read_bytes()).hexdigest()
    assert actual == FROZEN_TEST_SHA256, (
        "evals/intent/test.yaml 与冻结版本不一致：test 集已冻结，"
        "修改前需要先经过审批，并同步更新 BASELINE.md 中的 sha256 记录"
    )


def test_min_category_counts_reported() -> None:
    dev = load_intent_samples(EVAL_DIR / "dev.yaml")
    counts: dict[str, int] = {}
    for sample in dev:
        counts[sample.category] = counts.get(sample.category, 0) + 1
    assert counts["time_chinese"] == 6
    assert counts["multi_turn_topic_shift"] == 3
    assert counts["sync_non_pm"] == 2
    assert counts["sync_command"] == 3
    assert counts["confirmation_flow"] == 4


def test_sync_command_samples_cover_roles_and_outcomes(project) -> None:
    combined = load_intent_samples(EVAL_DIR / "dev.yaml") + load_intent_samples(
        EVAL_DIR / "test.yaml"
    )
    commands = [s for s in combined if s.category == "sync_command"]
    pm_sync = [
        s for s in commands if s.message.startswith("/同步") and s.sender == project.pm_id
    ]
    non_pm_sync = [
        s for s in commands if s.message.startswith("/同步") and s.sender != project.pm_id
    ]
    help_commands = [s for s in commands if "/帮助" in s.message]
    assert len(pm_sync) >= 2
    assert len(non_pm_sync) >= 1
    assert len(help_commands) >= 1
    for sample in pm_sync:
        behavior = sample.expected.behavior
        assert behavior.starts_sync and not behavior.sends_confirmation_card
        assert behavior.reply_category.value == "sync_started"
    for sample in non_pm_sync:
        behavior = sample.expected.behavior
        assert not behavior.starts_sync and not behavior.sends_confirmation_card
        assert behavior.reply_category.value == "no_permission"


def test_confirmation_flow_samples_cover_confirm_reject_and_non_pm(project) -> None:
    combined = load_intent_samples(EVAL_DIR / "dev.yaml") + load_intent_samples(
        EVAL_DIR / "test.yaml"
    )
    flows = [s for s in combined if s.category == "confirmation_flow"]
    assert len(flows) >= 6
    for sample in flows:
        confirmation = sample.context.pending_confirmation if sample.context else None
        assert confirmation is not None
        assert confirmation.kind == "sync"
        assert confirmation.initiated_by == project.pm_id
    confirmed = [s for s in flows if s.expected.behavior.starts_sync]
    rejected = [
        s
        for s in flows
        if s.sender == project.pm_id and not s.expected.behavior.starts_sync
    ]
    non_pm = [s for s in flows if s.sender != project.pm_id]
    assert len(confirmed) >= 3
    assert len(rejected) >= 2
    assert len(non_pm) >= 1
    for sample in confirmed:
        assert sample.expected.behavior.reply_category.value == "sync_started"
    for sample in rejected:
        assert sample.expected.behavior.reply_category.value == "noted"
    for sample in non_pm:
        assert sample.expected.behavior.reply_category.value == "smalltalk"

"""Evaluator logic tests on a mini dataset run through the real baseline SUT."""

import json
from pathlib import Path

import pytest
import yaml

from orgpilot.cli import main
from orgpilot.intent.evaluator import (
    BaselineSystemUnderTest,
    ReplyCategory,
    _has_sync_confirmation_marker,
    compare_sample,
    compute_metrics,
    load_intent_project,
    load_intent_samples,
    run_intent_eval,
)

MINI_PROJECT = {
    "schema_version": 1,
    "dataset_id": "mini_fixture",
    "project_id": "proj-mini",
    "members": [
        {"member_id": "ou_pm", "display_name": "PM", "role": "pm"},
        {"member_id": "ou_alice", "display_name": "Alice", "role": "backend"},
    ],
    "tasks": [
        {
            "task_id": "backend_api",
            "title": "Backend API",
            "owner_id": "ou_alice",
            "workflow_status": "doing",
            "deadline": "2026-11-20T18:00:00+08:00",
        }
    ],
}

MINI_SAMPLES = [
    # A clean status report the baseline records correctly.
    {
        "id": "mini_report_001",
        "category": "report_status",
        "message": "支付SDK报错卡住了，明天下午5点修好",
        "sender": "ou_alice",
        "occurred_at": "2026-11-18T10:00:00+08:00",
        "expected": {
            "behavior": {
                "claims": [{"task_id": "backend_api", "health_status": "delayed"}],
                "reply_category": "claim_recorded",
            },
            "intents": {
                "items": [
                    {
                        "intent": "report_status",
                        "task_id": "backend_api",
                        "speech_act": "statement",
                        "polarity": "affirmative",
                        "certainty": "certain",
                        "health_status": "delayed",
                    }
                ]
            },
        },
    },
    # PM natural-language sync request (LABELING.md R8): confirmation card first,
    # NO direct sync. The baseline starts the sync directly, so this pins the
    # unconfirmed-mass-send metric with a deliberate baseline failure.
    {
        "id": "mini_sync_001",
        "category": "request_sync",
        "message": "我要知道当前的项目进度",
        "sender": "ou_pm",
        "occurred_at": "2026-11-18T11:00:00+08:00",
        "expected": {
            "behavior": {
                "starts_sync": False,
                "sends_confirmation_card": True,
                "reply_category": "sync_confirm",
            },
            "intents": {
                "items": [
                    {
                        "intent": "request_sync",
                        "speech_act": "imperative",
                        "polarity": "affirmative",
                        "certainty": "certain",
                    }
                ]
            },
        },
    },
    # Non-PM sync request: expected no_permission, the baseline just greets.
    {
        "id": "mini_sync_002",
        "category": "sync_non_pm",
        "message": "同步一下全组进度",
        "sender": "ou_alice",
        "occurred_at": "2026-11-18T13:00:00+08:00",
        "expected": {
            "behavior": {"reply_category": "no_permission"},
            "intents": {
                "items": [
                    {
                        "intent": "request_sync",
                        "speech_act": "imperative",
                        "polarity": "affirmative",
                        "certainty": "certain",
                    }
                ]
            },
        },
    },
    # Deliberately empty expectation while the message yields a claim, to pin the
    # false-claim-rate arithmetic.
    {
        "id": "mini_trap_001",
        "category": "substring_trap",
        "message": "收银台页面有点问题",
        "sender": "ou_alice",
        "occurred_at": "2026-11-18T14:00:00+08:00",
        "expected": {
            "behavior": {"reply_category": "smalltalk"},
            "intents": {
                "items": [
                    {
                        "intent": "report_status",
                        "task_id": "backend_api",
                        "speech_act": "statement",
                        "polarity": "affirmative",
                        "certainty": "certain",
                    }
                ]
            },
        },
    },
    # Multi-turn answer that completes the pending probe successfully.
    {
        "id": "mini_answer_001",
        "category": "answer_pending",
        "message": "已经恢复正常了",
        "sender": "ou_alice",
        "occurred_at": "2026-11-18T15:00:00+08:00",
        "context": {
            "pending_question": {
                "question": "大概什么时候能恢复？",
                "task_id": "backend_api",
            }
        },
        "expected": {
            "behavior": {
                "claims": [{"task_id": "backend_api", "health_status": "on_track"}],
                "reply_category": "claim_recorded",
            },
            "intents": {
                "answers_pending": True,
                "items": [
                    {
                        "intent": "answer_pending",
                        "task_id": "backend_api",
                        "speech_act": "statement",
                        "polarity": "affirmative",
                        "certainty": "certain",
                        "health_status": "on_track",
                    }
                ],
            },
        },
    },
    # PM confirms a pending sync card (R8): should start the sync, so the reply
    # category is sync_started. The baseline constructs no confirmation state, so
    # the confirmation is lost — expected failure.
    {
        "id": "mini_confirm_001",
        "category": "confirmation_flow",
        "message": "好的，发吧",
        "sender": "ou_pm",
        "occurred_at": "2026-11-18T16:00:00+08:00",
        "context": {
            "pending_confirmation": {"kind": "sync", "initiated_by": "ou_pm"},
        },
        "expected": {
            "behavior": {
                "starts_sync": True,
                "reply_category": "sync_started",
            },
            "intents": {
                "items": [
                    {
                        "intent": "acknowledge",
                        "speech_act": "statement",
                        "polarity": "affirmative",
                        "certainty": "certain",
                    }
                ]
            },
        },
    },
]


@pytest.fixture
def mini_dataset_dir(tmp_path: Path) -> Path:
    dataset_dir = tmp_path / "intent"
    dataset_dir.mkdir()
    (dataset_dir / "project.yaml").write_text(
        yaml.safe_dump(MINI_PROJECT, allow_unicode=True), encoding="utf-8"
    )
    (dataset_dir / "dev.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "dataset_id": "mini_dev",
                "split": "dev",
                "samples": MINI_SAMPLES,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return dataset_dir


async def test_baseline_and_metrics_on_mini_dataset(mini_dataset_dir: Path, tmp_path: Path) -> None:
    project = load_intent_project(mini_dataset_dir / "project.yaml")
    samples = load_intent_samples(mini_dataset_dir / "dev.yaml")
    assert len(samples) == 6

    system = BaselineSystemUnderTest(project)
    comparisons = []
    for sample in samples:
        observed = await system.observe(sample)
        comparisons.append(compare_sample(sample, observed))

    metrics = compute_metrics(comparisons)
    assert metrics.total_samples == 6
    # claims: 2 expected, 3 observed (mini_trap_001 is a deliberate false claim)
    assert metrics.expected_claims_total == 2
    assert metrics.observed_claims_total == 3
    assert metrics.claim_true_positives == 2
    assert metrics.claim_precision == pytest.approx(2 / 3)
    assert metrics.claim_recall == pytest.approx(1.0)
    # four samples expect no claims (both sync samples, mini_trap_001, mini_confirm_001);
    # only mini_trap_001 produces one.
    assert metrics.false_claim_rate == pytest.approx(1 / 4)
    # none of the no-sync-no-card samples shows a sync side effect
    assert metrics.sync_false_trigger_rate == 0.0
    # mini_sync_001 requires a confirmation card first; the heuristic pipeline
    # does not start a sync at all for it (so it cannot mass-send either)
    assert metrics.unconfirmed_sync_expected == 1
    assert metrics.unconfirmed_sync_count == 0
    # reply matches: mini_report_001 and mini_answer_001 only. The baseline starts
    # the sync directly, so even the reply for card-only mini_sync_001 (sync_confirm)
    # diverges (observed sync_started).
    assert metrics.reply_category_accuracy == pytest.approx(2 / 6)
    assert metrics.multi_turn_samples == 1
    assert metrics.multi_turn_success_rate == pytest.approx(1.0)
    assert metrics.intent_metrics is None

    failure_ids = [c.sample_id for c in comparisons if not c.passed]
    assert set(failure_ids) == {
        "mini_sync_001",
        "mini_sync_002",
        "mini_trap_001",
        "mini_confirm_001",
    }

    by_category = {stats.category: stats for stats in metrics.per_category}
    assert by_category["substring_trap"].false_claims == 1
    assert by_category["request_sync"].reply_correct == 0
    assert by_category["sync_non_pm"].reply_correct == 0
    assert by_category["answer_pending"].claim_recall == 1.0
    assert by_category["confirmation_flow"].samples == 1


async def test_pending_confirmation_constructs_no_state(mini_dataset_dir: Path) -> None:
    project = load_intent_project(mini_dataset_dir / "project.yaml")
    samples = {s.id: s for s in load_intent_samples(mini_dataset_dir / "dev.yaml")}
    system = BaselineSystemUnderTest(project)

    observed = await system.observe(samples["mini_confirm_001"])

    # The baseline has no confirmation-card mechanism, so no state is constructed:
    # the PM acknowledgement cannot start a sync and lands as plain smalltalk.
    assert observed.starts_sync is False
    assert observed.sends_confirmation_card is False
    assert observed.reply_category == ReplyCategory.SMALLTALK


def test_confirmation_card_requires_explicit_marker() -> None:
    # Post-side-effect notifications and plain text replies never count.
    assert _has_sync_confirmation_marker(
        [{"msg_type": "text", "content": {"text": "已启动全员进度探针"}}]
    ) is False
    assert _has_sync_confirmation_marker([]) is False
    assert _has_sync_confirmation_marker([{"card": "raw string card"}]) is False
    # The explicit kind=sync_confirmation marker (before the side effect) counts.
    assert _has_sync_confirmation_marker(
        [{"msg_type": "interactive", "content": {"kind": "sync_confirmation"}}]
    ) is True
    assert _has_sync_confirmation_marker([{"card": {"kind": "sync_confirmation"}}]) is True


async def test_run_intent_eval_writes_reports(mini_dataset_dir: Path, tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    metrics, failures = await run_intent_eval(
        split="dev",
        mode="mock",
        dataset_dir=mini_dataset_dir,
        report_dir=report_dir,
    )
    assert metrics.total_samples == 6
    assert len(failures) == 4

    json_path = report_dir / "report-dev-mock.json"
    markdown_path = report_dir / "report-dev-mock.md"
    assert json_path.exists() and markdown_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["split"] == "dev"
    assert payload["metrics"]["total_samples"] == 6
    assert payload["metrics"]["unconfirmed_sync_count"] == 0
    assert {failure["sample_id"] for failure in payload["failures"]} == {
        "mini_sync_001",
        "mini_sync_002",
        "mini_trap_001",
        "mini_confirm_001",
    }

    markdown = markdown_path.read_text(encoding="utf-8")
    assert "按类别统计" in markdown
    assert "失败样本（4 条）" in markdown
    assert "意图层指标 | N/A" in markdown
    assert "未经确认群发次数 | 0" in markdown
    # every mini category has fewer than 5 samples and is marked as such
    assert "样本不足，仅供参考" in markdown


def test_run_intent_eval_rejects_non_mock_mode(mini_dataset_dir: Path, tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="mock"):
        __import__("asyncio").run(
            run_intent_eval(
                split="dev",
                mode="live",
                dataset_dir=mini_dataset_dir,
                report_dir=tmp_path / "reports",
            )
        )


def test_cli_eval_intent_uses_mini_dataset(
    mini_dataset_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    report_dir = tmp_path / "cli-reports"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orgpilot",
            "eval-intent",
            "--split",
            "dev",
            "--mode",
            "mock",
            "--dataset-dir",
            str(mini_dataset_dir),
            "--report-dir",
            str(report_dir),
        ],
    )
    exit_code = main()
    assert exit_code == 0
    assert (report_dir / "report-dev-mock.json").exists()


def test_cli_eval_intent_rejects_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(
        sys, "argv", ["orgpilot", "eval-intent", "--split", "dev", "--mode", "live"]
    )
    with pytest.raises(SystemExit, match="mock"):
        main()

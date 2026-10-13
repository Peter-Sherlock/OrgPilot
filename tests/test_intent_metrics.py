"""Intent-layer metrics tests (3b.1 §8): scoring unit and report display —
all offline. The oracle self-check lives in test_intent_evaluator.py, next to
the mini_dataset_dir fixture it reuses."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orgpilot.intent.evaluator import (
    EvalSample,
    Expected,
    ExpectedBehavior,
    ExpectedIntentItem,
    ExpectedIntents,
    compare_sample,
    compute_metrics,
    render_markdown_report,
)
from orgpilot.intent.metrics import (
    compute_intent_metrics,
    intent_output_view,
)
from orgpilot.intent.models import (
    IntentItem,
    IntentResult,
    Polarity,
)

BJ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=BJ)


def _expected_item(
    intent: str,
    task_id: str | None = None,
    *,
    certainty: str = "certain",
    polarity: str = "affirmative",
    speech_act: str = "statement",
) -> ExpectedIntentItem:
    return ExpectedIntentItem(
        intent=intent,  # type: ignore[arg-type]
        task_id=task_id,
        speech_act=speech_act,  # type: ignore[arg-type]
        polarity=polarity,  # type: ignore[arg-type]
        certainty=certainty,  # type: ignore[arg-type]
    )


def _result_item(
    intent: str,
    task_id: str | None = None,
    *,
    certainty: str = "certain",
    polarity: str = "affirmative",
    speech_act: str = "statement",
) -> IntentItem:
    return IntentItem(
        intent=intent,  # type: ignore[arg-type]
        task_id=task_id,
        speech_act=speech_act,  # type: ignore[arg-type]
        polarity=polarity,  # type: ignore[arg-type]
        certainty=certainty,  # type: ignore[arg-type]
        source_quote="逐字引用",
        confidence=0.9,
    )


def _sample(
    sample_id: str,
    message: str,
    items: list[ExpectedIntentItem],
    answers_pending: bool | None = None,
) -> EvalSample:
    return EvalSample(
        id=sample_id,
        category="unit",
        message=message,
        sender="ou_alice",
        occurred_at=NOW,
        expected=Expected(
            behavior=ExpectedBehavior(reply_category="smalltalk"),
            intents=ExpectedIntents(answers_pending=answers_pending, items=items),
        ),
    )


class _StubRecorder:
    """result_for-only stand-in for RecordingIntentClient."""

    def __init__(self, mapping: dict[tuple[str, str], IntentResult | None]):
        self.mapping = mapping

    def result_for(self, sender_id: str, message: str) -> IntentResult | None:
        return self.mapping.get((sender_id, message))


def _unit_samples() -> list[EvalSample]:
    return [
        # exact match, all attributes equal
        _sample(
            "s1",
            "后端接口联调完成了",
            [_expected_item("report_status", "task_a")],
        ),
        # expected chitchat, model hallucinated a report: set mismatch AND a
        # cross-intent confusion cell
        _sample("s2", "哈哈哈周五了", [_expected_item("chitchat")]),
        # expected an answer, model produced nothing; answers_pending wrong
        _sample(
            "s3",
            "明天下午5点能修好",
            [_expected_item("answer_pending", "task_a")],
            answers_pending=True,
        ),
        # multiset match despite different item order; one polarity mismatch
        _sample(
            "s4",
            "收到，支付SDK我在推进",
            [
                _expected_item("report_status", "task_a"),
                _expected_item("chitchat", certainty="certain"),
            ],
        ),
    ]


def _unit_recorder() -> _StubRecorder:
    mapping = {
        ("ou_alice", "后端接口联调完成了"): IntentResult(
            items=[_result_item("report_status", "task_a")], reasoning="汇报"
        ),
        ("ou_alice", "哈哈哈周五了"): IntentResult(
            items=[_result_item("report_status", "search_api")], reasoning="误判"
        ),
        ("ou_alice", "明天下午5点能修好"): IntentResult(items=[], reasoning="空输出"),
        (
            ("ou_alice", "收到，支付SDK我在推进"),
        ): None,  # placeholder replaced below; keys must be tuples
    }
    mapping[("ou_alice", "收到，支付SDK我在推进")] = IntentResult(
        items=[
            _result_item("chitchat"),
            _result_item("report_status", "task_a"),
        ],
        answers_pending=False,
        reasoning="两条",
    )
    # s4 polarity mismatch on the report_status pair
    mapping[("ou_alice", "收到，支付SDK我在推进")].items[1].polarity = Polarity.NEGATIVE
    return _StubRecorder(mapping)


def test_intent_set_match_per_intent_scores_and_confusion() -> None:
    samples = _unit_samples()
    metrics = compute_intent_metrics(samples, _unit_recorder())  # type: ignore[arg-type]

    assert metrics.samples_with_intents == 4
    assert metrics.intent_set_exact_matches == 2  # s1 and s4
    assert metrics.intent_set_exact_match_rate == pytest.approx(0.5)

    by_intent = {row.intent: row for row in metrics.per_intent}
    # report_status: expected 2 (s1, s4), predicted 3 (s1, s4, s2-hallucination);
    # s1 and s4 match on intent -> P 2/3, R 1, F1 0.8
    report = by_intent["report_status"]
    assert (report.expected, report.predicted, report.true_positives) == (2, 3, 2)
    assert report.precision == pytest.approx(2 / 3)
    assert report.recall == pytest.approx(1.0)
    assert report.f1 == pytest.approx(0.8)
    # chitchat: expected 2 (s2, s4), predicted 1 (s4); matched in s4
    chitchat = by_intent["chitchat"]
    assert (chitchat.expected, chitchat.predicted, chitchat.true_positives) == (2, 1, 1)
    assert chitchat.precision == pytest.approx(1.0)
    assert chitchat.recall == pytest.approx(0.5)
    # answer_pending: expected 1, predicted 0 -> P not measurable, R 0, F1 0
    answer = by_intent["answer_pending"]
    assert answer.precision is None
    assert answer.recall == 0.0
    assert answer.f1 == 0.0
    # macro-F1 over supported intents: (0.8 + 2/3 + 0) / 3
    assert metrics.macro_f1 == pytest.approx((0.8 + 2 / 3) / 3)

    # answers_pending: only s3 annotated -> 0/1
    assert metrics.answers_pending_total == 1
    assert metrics.answers_pending_correct == 0
    assert metrics.answers_pending_accuracy == 0.0

    # matched items: s1 (1 pair) + s4 (2 pairs); speech_act/certainty/task all
    # equal, report_status pair in s4 has the flipped polarity
    assert metrics.matched_items == 3
    assert metrics.speech_act_accuracy == pytest.approx(1.0)
    assert metrics.polarity_accuracy == pytest.approx(2 / 3)
    assert metrics.certainty_accuracy == pytest.approx(1.0)
    assert metrics.task_id_accuracy == pytest.approx(1.0)

    confusion = {(pair.expected, pair.actual): pair.count for pair in metrics.confusion}
    assert confusion[("report_status", "report_status")] == 2  # s1 + s4 (task-aligned)
    assert confusion[("chitchat", "report_status")] == 1  # s2: the real confusion
    assert confusion[("chitchat", "chitchat")] == 1  # s4 positional fallback
    assert confusion[("answer_pending", "（无）")] == 1  # s3: nothing predicted


def test_intent_metrics_on_empty_annotation_set() -> None:
    command_sample = EvalSample(
        id="cmd",
        category="sync_command",
        message="/同步",
        sender="ou_pm",
        occurred_at=NOW,
        expected=Expected(behavior=ExpectedBehavior(reply_category="sync_started")),
    )
    metrics = compute_intent_metrics([command_sample], _StubRecorder({}))  # type: ignore[arg-type]
    assert metrics.samples_with_intents == 0
    assert metrics.intent_set_exact_match_rate == 0.0
    assert metrics.macro_f1 is None
    assert metrics.answers_pending_accuracy is None
    assert metrics.per_intent == []


def test_intent_output_view_handles_missing_result() -> None:
    assert intent_output_view(None) is None
    result = IntentResult(items=[], answers_pending=True, reasoning="只有标记")
    view = intent_output_view(result)
    assert view == {"items": [], "answers_pending": True, "reasoning": "只有标记"}


def _markdown(metrics, failures, **kwargs) -> str:
    return render_markdown_report("dev", "mock", metrics, failures, **kwargs)


def test_report_renders_dash_for_zero_denominators() -> None:
    """§9: an empty run measures nothing — every affected cell shows “—”."""
    text = _markdown(compute_metrics([]), [])
    assert "| claim precision | — |" in text
    assert "| claim recall | — |" in text
    assert "| task_id 准确率 | — |" in text
    assert "| 错误 claim 率 | — " in text
    assert "| 同步误触发率 | — " in text
    assert "| 回复类别准确率 | — |" in text
    assert "| 多轮补全成功率 | — " in text


def test_baseline_note_appears_only_on_baseline_reports() -> None:
    metrics = compute_metrics([])
    assert "baseline 无意图层" in _markdown(metrics, [], is_baseline=True)
    layered_text = _markdown(metrics, [], is_baseline=False)
    assert "baseline 无意图层" not in layered_text
    assert "| 意图层指标 | 未采集" in layered_text


def test_report_includes_intent_metrics_section() -> None:
    intent_metrics = compute_intent_metrics(_unit_samples(), _unit_recorder())
    metrics = compute_metrics([]).model_copy(update={"intent_metrics": intent_metrics})
    text = _markdown(metrics, [])
    assert "## 意图层指标" in text
    assert "| 意图 | 期望 | 预测 | TP | P | R | F1 |" in text
    assert "| answer_pending | 1 | 0 | 0 | — | 0% | 0% |" in text
    assert "意图混淆表（期望 → 实际）" in text
    # the summary row in the overall table carries the headline numbers
    assert "| 意图层指标 | 意图集合完全匹配 50.00%" in text


def test_baseline_metrics_stay_none_without_recorder() -> None:
    metrics = compute_metrics([])
    assert metrics.intent_metrics is None


def test_layer0_command_samples_are_excluded_and_counted() -> None:
    """§4: explicit commands never reach the classifier — excluded from the
    intent metrics even when annotated, and counted separately."""
    command = _sample(
        "cmd_sync",
        "/同步",
        [_expected_item("request_sync")],
    )
    normal = _sample("s_normal", "后端接口联调完成了", [_expected_item("report_status", "task_a")])
    metrics = compute_intent_metrics([command, normal], _StubRecorder({}))  # type: ignore[arg-type]
    assert metrics.layer0_samples == 1
    assert metrics.samples_with_intents == 1  # only the non-command counts
    # the command's annotation must NOT appear as a miss
    assert {row.intent for row in metrics.per_intent} == {"report_status"}


def test_category_sync_column_counts_only_unexpected_triggers() -> None:
    """§4: the per-category column matches the overall口径 — a sample that
    EXPECTS a confirmation card does not count as a false trigger when the
    card is actually sent."""
    from orgpilot.intent.evaluator import ObservedBehavior

    card_sample = _sample("s_card", "来一次全员同步", [_expected_item("request_sync")])
    card_sample = card_sample.model_copy(
        update={
            "expected": card_sample.expected.model_copy(
                update={
                    "behavior": ExpectedBehavior(
                        starts_sync=False,
                        sends_confirmation_card=True,
                        reply_category="sync_confirm",
                    )
                }
            )
        }
    )
    card_observed = ObservedBehavior(
        starts_sync=False,
        sends_confirmation_card=True,
        reply_category="sync_confirm",
    )
    plain_observed = ObservedBehavior(starts_sync=True, reply_category="sync_started")
    comparisons = [
        compare_sample(card_sample, card_observed),
        compare_sample(
            _sample("s_plain", "大家汇报一下进度", [_expected_item("request_sync")]),
            plain_observed,
        ),
    ]
    metrics = compute_metrics(comparisons)
    stats = metrics.per_category[0]
    assert stats.samples == 2
    assert stats.sync_false_triggers == 1  # only the plain sample misfired
    assert comparisons[0].passed

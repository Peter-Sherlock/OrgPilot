"""Intent-layer metrics for the layered evaluation (ADR-0008 §8, step 3b.1).

The behavior metrics in :mod:`orgpilot.intent.evaluator` score the SYSTEM
(claims recorded, syncs, clarifications). These metrics score the CLASSIFIER:
:class:`RecordingIntentClient` is a transparent wrapper that captures every
``IntentResult`` the intent client produces for a sample — pre-guardrails, so
router logic is untouched — and the results are compared against the dataset's
``expected.intents``:

- sample-level intent-set exact match rate;
- per-intent precision / recall / F1 and macro-F1, over the intents that
  actually appear in the split;
- answers_pending accuracy;
- speech_act / polarity / certainty / task_id accuracy over matched items;
- an expected → actual intent confusion table.

Self-check contract: with the oracle intent client every number must be 100%,
because oracle results ARE the annotations.
"""

from collections import Counter
from typing import TYPE_CHECKING, Any

from pydantic import Field

from orgpilot.extraction.models import StrictModel

if TYPE_CHECKING:
    from orgpilot.intent.client import IntentContext
    from orgpilot.intent.evaluator import EvalSample
    from orgpilot.intent.models import IntentResult

MISSING = "（无）"


def _intent_name(intent: Any) -> str:
    return intent.value if hasattr(intent, "value") else str(intent)


class RecordingIntentClient:
    """Transparent ``IntentClient`` wrapper that records results for metrics.

    Results are keyed by ``(sender_id, message)`` — the single-sample splits
    never repeat that pair (the oracle client refuses such collisions), so the
    first result for a key wins.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.results: dict[tuple[str, str], Any] = {}
        self.calls = 0

    async def classify(self, message: str, context: "IntentContext") -> Any:
        self.calls += 1
        result = await self._inner.classify(message, context)
        self.results.setdefault((context.sender_id, message), result)
        return result

    def result_for(self, sender_id: str, message: str) -> "IntentResult | None":
        return self.results.get((sender_id, message))


class PerIntentMetrics(StrictModel):
    """Precision / recall / F1 for one intent; ``None`` marks a zero denominator."""

    intent: str
    expected: int
    predicted: int
    true_positives: int
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None


class ConfusionPair(StrictModel):
    """One cell of the expected → actual confusion table."""

    expected: str
    actual: str
    count: int


class IntentLayerMetrics(StrictModel):
    """Classifier-level scores; ``None`` floats mean "not measurable" (§9)."""

    samples_with_intents: int
    intent_set_exact_matches: int
    intent_set_exact_match_rate: float
    answers_pending_total: int
    answers_pending_correct: int
    answers_pending_accuracy: float | None
    macro_f1: float | None
    matched_items: int
    speech_act_accuracy: float | None
    polarity_accuracy: float | None
    certainty_accuracy: float | None
    task_id_accuracy: float | None
    # layer-0 handled samples (explicit commands, card callbacks) — never
    # scored against the classifier, reported separately (3b 下半 §4)
    layer0_samples: int = 0
    per_intent: list[PerIntentMetrics] = Field(default_factory=list)
    confusion: list[ConfusionPair] = Field(default_factory=list)


def _match_items(
    expected_items: list[Any], predicted_items: list[Any]
) -> list[tuple[int, int]]:
    """Greedy item alignment for attribute scoring: (intent, task_id) pairs
    first, then intent-only pairs, both in dataset order."""
    pairs: list[tuple[int, int]] = []
    used: set[int] = set()

    def _same_intent(expected: Any, predicted: Any) -> bool:
        return _intent_name(expected.intent) == _intent_name(predicted.intent)

    for e_index, expected in enumerate(expected_items):
        found = None
        for p_index, predicted in enumerate(predicted_items):
            if p_index not in used and _same_intent(expected, predicted) and (
                expected.task_id == predicted.task_id
            ):
                found = p_index
                break
        if found is None:
            for p_index, predicted in enumerate(predicted_items):
                if p_index not in used and _same_intent(expected, predicted):
                    found = p_index
                    break
        if found is not None:
            pairs.append((e_index, found))
            used.add(found)
    return pairs


def _confusion_rows(
    expected_items: list[Any], predicted_items: list[Any]
) -> list[tuple[str, str]]:
    """Expected → actual intent pairs aligned by task_id first, then by
    position — deliberately INDEPENDENT of intent so real confusions
    (expected chitchat, actual report_status) stay visible; unmatched items
    become the ``（无）`` pseudo-intent."""
    rows: list[tuple[str, str]] = []
    used: set[int] = set()
    for expected in expected_items:
        target: int | None = None
        if expected.task_id is not None:
            for p_index, predicted in enumerate(predicted_items):
                if p_index not in used and predicted.task_id == expected.task_id:
                    target = p_index
                    break
        if target is None:
            for p_index in range(len(predicted_items)):
                if p_index not in used:
                    target = p_index
                    break
        if target is None:
            rows.append((_intent_name(expected.intent), MISSING))
        else:
            used.add(target)
            rows.append(
                (_intent_name(expected.intent), _intent_name(predicted_items[target].intent))
            )
    for p_index, predicted in enumerate(predicted_items):
        if p_index not in used:
            rows.append((MISSING, _intent_name(predicted.intent)))
    return rows


def _is_layer0(sample: Any) -> bool:
    """Layer 0 handles a sample before the classifier ever sees it: explicit
    commands ("/同步", "/帮助") and card callbacks. Their intent annotations
    describe the command mapping, not classifier output, so they are excluded
    from the intent metrics and counted separately (3b 下半 §4)."""
    return sample.message.strip().startswith("/")


def compute_intent_metrics(
    samples: "list[EvalSample]", recorder: RecordingIntentClient
) -> IntentLayerMetrics:
    """Scores recorded classifier outputs against the sample annotations.

    Only samples WITH intent annotations participate (command turns carry
    none): an item annotation or an answers_pending annotation qualifies.
    Layer-0 samples (commands) are excluded even when annotated.
    """
    layer0_count = sum(1 for sample in samples if _is_layer0(sample))
    annotated = [
        sample
        for sample in samples
        if not _is_layer0(sample)
        and (sample.expected.intents.items or sample.expected.intents.answers_pending is not None)
    ]
    tallies: dict[str, dict[str, int]] = {}
    set_matches = 0
    answers_total = 0
    answers_correct = 0
    matched_items = 0
    attr_hits: Counter[str] = Counter()
    confusion_counter: Counter[tuple[str, str]] = Counter()

    for sample in annotated:
        expected_items = sample.expected.intents.items
        result = recorder.result_for(sample.sender, sample.message)
        predicted_items = list(result.items) if result is not None else []
        expected_names = Counter(_intent_name(item.intent) for item in expected_items)
        predicted_names = Counter(_intent_name(item.intent) for item in predicted_items)
        if expected_names == predicted_names:
            set_matches += 1
        for name, count in expected_names.items():
            tally = tallies.setdefault(name, {"expected": 0, "predicted": 0, "tp": 0})
            tally["expected"] += count
        for name, count in predicted_names.items():
            tally = tallies.setdefault(name, {"expected": 0, "predicted": 0, "tp": 0})
            tally["predicted"] += count

        pairs = _match_items(expected_items, predicted_items)
        matched_items += len(pairs)
        for e_index, p_index in pairs:
            expected_item, predicted_item = expected_items[e_index], predicted_items[p_index]
            tallies[_intent_name(expected_item.intent)]["tp"] += 1
            attr_hits["speech_act"] += int(expected_item.speech_act == predicted_item.speech_act)
            attr_hits["polarity"] += int(expected_item.polarity == predicted_item.polarity)
            attr_hits["certainty"] += int(expected_item.certainty == predicted_item.certainty)
            attr_hits["task_id"] += int(expected_item.task_id == predicted_item.task_id)
        for expected_intent, actual_intent in _confusion_rows(expected_items, predicted_items):
            confusion_counter[(expected_intent, actual_intent)] += 1

        if sample.expected.intents.answers_pending is not None:
            answers_total += 1
            actual = bool(result.answers_pending) if result is not None else False
            answers_correct += int(actual == sample.expected.intents.answers_pending)

    per_intent: list[PerIntentMetrics] = []
    f1_over_supported: list[float] = []
    for name in sorted(tallies):
        tally = tallies[name]
        precision = tally["tp"] / tally["predicted"] if tally["predicted"] else None
        recall = tally["tp"] / tally["expected"] if tally["expected"] else None
        if precision is not None and recall is not None and precision + recall > 0:
            f1 = 2 * precision * recall / (precision + recall)
        elif tally["expected"]:
            f1 = 0.0  # expected but unmatched: F1 is 0, not "not measurable"
        else:
            f1 = None
        if tally["expected"]:
            f1_over_supported.append(f1 if f1 is not None else 0.0)
        per_intent.append(
            PerIntentMetrics(
                intent=name,
                expected=tally["expected"],
                predicted=tally["predicted"],
                true_positives=tally["tp"],
                precision=precision,
                recall=recall,
                f1=f1,
            )
        )

    def _ratio(hits: int, denominator: int) -> float | None:
        return hits / denominator if denominator else None

    return IntentLayerMetrics(
        samples_with_intents=len(annotated),
        intent_set_exact_matches=set_matches,
        intent_set_exact_match_rate=(
            set_matches / len(annotated) if annotated else 0.0
        ),
        answers_pending_total=answers_total,
        answers_pending_correct=answers_correct,
        answers_pending_accuracy=_ratio(answers_correct, answers_total),
        macro_f1=(
            sum(f1_over_supported) / len(f1_over_supported) if f1_over_supported else None
        ),
        matched_items=matched_items,
        speech_act_accuracy=_ratio(attr_hits["speech_act"], matched_items),
        polarity_accuracy=_ratio(attr_hits["polarity"], matched_items),
        certainty_accuracy=_ratio(attr_hits["certainty"], matched_items),
        task_id_accuracy=_ratio(attr_hits["task_id"], matched_items),
        layer0_samples=layer0_count,
        per_intent=per_intent,
        confusion=[
            ConfusionPair(expected=expected_intent, actual=actual_intent, count=count)
            for (expected_intent, actual_intent), count in sorted(confusion_counter.items())
        ],
    )


def intent_output_view(result: "IntentResult | None") -> dict[str, Any] | None:
    """Compact view of what the model actually produced, for failure lists."""
    if result is None:
        return None
    return {
        "items": [
            item.model_dump(mode="json", exclude={"confidence"}) for item in result.items
        ],
        "answers_pending": result.answers_pending,
        "reasoning": result.reasoning,
    }


def _ratio_or_dash(value: float | None, denominator: int) -> str:
    if denominator <= 0 or value is None:
        return "—"
    return f"{value:.2%}"


def render_intent_metrics_section(metrics: IntentLayerMetrics) -> list[str]:
    """Markdown section for the intent-layer block of the evaluation report."""
    lines = ["## 意图层指标", ""]
    if metrics.layer0_samples:
        lines.append(
            f"- 第 0 层处理（显式命令/卡片回调）：{metrics.layer0_samples} 条"
            "（不经过分类器，不计入意图层指标）"
        )
    lines.append(
        "- 样本级意图集合完全匹配率："
        f"{metrics.intent_set_exact_match_rate:.2%} "
        f"（{metrics.intent_set_exact_matches}/{metrics.samples_with_intents}）"
    )
    lines.append(
        "- answers_pending 准确率："
        + _ratio_or_dash(
            metrics.answers_pending_accuracy, metrics.answers_pending_total
        )
        + f"（{metrics.answers_pending_correct}/{metrics.answers_pending_total}）"
    )
    lines.append(
        "- macro-F1："
        + ("—" if metrics.macro_f1 is None else f"{metrics.macro_f1:.2%}")
        + f"（匹配条目 {metrics.matched_items}）"
    )
    lines.append(
        "- 匹配条目属性准确率："
        f"speech_act {_ratio_or_dash(metrics.speech_act_accuracy, metrics.matched_items)}、"
        f"polarity {_ratio_or_dash(metrics.polarity_accuracy, metrics.matched_items)}、"
        f"certainty {_ratio_or_dash(metrics.certainty_accuracy, metrics.matched_items)}、"
        f"task_id {_ratio_or_dash(metrics.task_id_accuracy, metrics.matched_items)}"
    )
    lines.append("")
    lines.append("| 意图 | 期望 | 预测 | TP | P | R | F1 |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in metrics.per_intent:
        def _score(value: float | None, denominator: int) -> str:
            return "—" if denominator == 0 or value is None else f"{value:.0%}"

        lines.append(
            f"| {row.intent} | {row.expected} | {row.predicted} | {row.true_positives} | "
            f"{_score(row.precision, row.predicted)} | {_score(row.recall, row.expected)} | "
            f"{_score(row.f1, row.expected)} |"
        )
    lines.append("")
    if metrics.confusion:
        actual_names = sorted({pair.actual for pair in metrics.confusion})
        expected_names = sorted({pair.expected for pair in metrics.confusion})
        counts = {(pair.expected, pair.actual): pair.count for pair in metrics.confusion}
        lines.append("意图混淆表（期望 → 实际）：")
        lines.append("")
        lines.append("| 期望 \\ 实际 | " + " | ".join(actual_names) + " |")
        lines.append("|---|" + "---|" * len(actual_names))
        for expected_name in expected_names:
            cells = " | ".join(
                str(counts.get((expected_name, actual_name), "—"))
                for actual_name in actual_names
            )
            lines.append(f"| {expected_name} | {cells} |")
        lines.append("")
    return lines

"""Intent behavior evaluation runner and existing-system baseline (ADR-0008 §8).

The evaluation judges the SYSTEM BEHAVIOR (claims recorded, sync started,
clarifications asked, reply category) so the current pipeline and the future
intent layer can be compared on the same samples. The baseline system under
test drives the real entry point — ``FeishuWebhookHandler`` over an in-memory
SQLite database with a mock adapter — and collects behavior from the event
store, the sync coordinator, and the adapter's outbound messages. The baseline
has no intent layer, so intent-level metrics are reported as N/A.
"""

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import Field

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.domain.enums import ProbeMemberStatus
from orgpilot.domain.sync_models import MemberProbeState
from orgpilot.events.models import OrgEvent
from orgpilot.extraction.models import StrictModel
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.service import GatewayService
from orgpilot.intent.models import (
    Certainty,
    IntentType,
    Polarity,
    SpeechAct,
)
from orgpilot.storage.database import Database

FIXTURE_OCCURRED_AT = "2026-10-12T09:00:00+08:00"
FIXTURE_SOURCE_REF = "intent-eval-fixture"

# Category minimums from ADR-0008 §8 sample coverage requirements.
CATEGORY_MINIMUMS: dict[str, int] = {
    "report_status": 8,
    "make_commitment": 8,
    "answer_pending": 8,
    "query_status": 8,
    "request_sync": 8,
    "acknowledge": 8,
    "chitchat": 8,
    "help": 8,
    "out_of_scope": 8,
    "negation": 8,
    "hypothetical": 6,
    "vague": 6,
    "multi_task": 8,
    "disambiguation": 6,
    "time_chinese": 10,
    "substring_trap": 6,
    "p1_p2": 10,
    "sync_near_negative": 6,
    "sync_non_pm": 3,
    "multi_turn_topic_shift": 4,
}


class ReplyCategory(StrEnum):
    """Behavior-level reply category. ``clarify`` extends the exemplified list for
    the follow-up-question turns the ADR requires the future system to send."""

    CLAIM_RECORDED = "claim_recorded"
    QUERY_ANSWER = "query_answer"
    SYNC_CONFIRM = "sync_confirm"
    NO_PERMISSION = "no_permission"
    UNSUPPORTED = "unsupported"
    SMALLTALK = "smalltalk"
    HELP = "help"
    CLARIFY = "clarify"


# ---------------------------------------------------------------------------
# Dataset contracts
# ---------------------------------------------------------------------------


class PendingQuestionContext(StrictModel):
    """The question the bot asked last, and the task it targets."""

    question: str
    task_id: str | None = None


class RecentMessage(StrictModel):
    role: str
    text: str


class SampleContext(StrictModel):
    pending_question: PendingQuestionContext | None = None
    recent_messages: list[RecentMessage] = Field(default_factory=list)


class ExpectedClaim(StrictModel):
    task_id: str
    health_status: str
    expected_completion: datetime | None = None


class ExpectedCommitment(StrictModel):
    task_id: str
    predicate: str = "workflow_status"
    expected_value: str = "done"
    due_at: datetime | None = None


class ExpectedBehavior(StrictModel):
    claims: list[ExpectedClaim] = Field(default_factory=list)
    commitments: list[ExpectedCommitment] = Field(default_factory=list)
    starts_sync: bool = False
    sends_confirmation_card: bool = False
    asks_clarification: bool = False
    reply_category: ReplyCategory


class ExpectedIntentItem(StrictModel):
    """Annotator view of one intent item; unspecified slots mean "don't check"."""

    intent: IntentType
    task_id: str | None = None
    speech_act: SpeechAct
    polarity: Polarity
    certainty: Certainty
    health_status: str | None = None
    time_expression: str | None = None
    expected_completion: datetime | None = None
    blocker: str | None = None
    source_quote: str | None = None


class ExpectedIntents(StrictModel):
    answers_pending: bool | None = None
    items: list[ExpectedIntentItem] = Field(default_factory=list)


class Expected(StrictModel):
    behavior: ExpectedBehavior
    intents: ExpectedIntents


class EvalSample(StrictModel):
    id: str
    category: str
    message: str
    sender: str
    occurred_at: datetime
    context: SampleContext | None = None
    expected: Expected


class IntentProjectFixture(StrictModel):
    """Fixed project the samples run against (1 PM + engineers + tasks)."""

    schema_version: int = 1
    dataset_id: str = ""
    title: str = ""
    project_id: str
    members: list[dict[str, Any]]
    tasks: list[dict[str, Any]]

    @property
    def pm_id(self) -> str:
        for member in self.members:
            if member.get("role") == "pm":
                return str(member["member_id"])
        raise ValueError("intent evaluation project fixture must define a pm member")

    @property
    def member_ids(self) -> set[str]:
        return {str(member["member_id"]) for member in self.members}

    @property
    def task_ids(self) -> set[str]:
        return {str(task["task_id"]) for task in self.tasks}


class IntentDataset(StrictModel):
    schema_version: int
    dataset_id: str
    split: str
    samples: list[EvalSample]


# ---------------------------------------------------------------------------
# Observed behavior and the system-under-test protocol
# ---------------------------------------------------------------------------


class ObservedClaim(StrictModel):
    task_id: str
    health_status: str
    expected_completion: datetime | None = None


class ObservedCommitment(StrictModel):
    task_id: str
    predicate: str
    expected_value: str
    due_at: datetime | None = None


class ObservedBehavior(StrictModel):
    claims: list[ObservedClaim] = Field(default_factory=list)
    commitments: list[ObservedCommitment] = Field(default_factory=list)
    starts_sync: bool = False
    sends_confirmation_card: bool = False
    asks_clarification: bool = False
    reply_category: ReplyCategory = ReplyCategory.SMALLTALK


class IntentSystemUnderTest(Protocol):
    """Anything that can process one evaluation sample and report its behavior."""

    async def observe(self, sample: EvalSample) -> ObservedBehavior: ...


class _RecordingAdapter(MockCollaborationAdapter):
    """Mock adapter that also exposes a Feishu client so the webhook's direct
    replies (greeting, sync confirmation, membership hint) become observable."""

    def __init__(self, project_id: str) -> None:
        super().__init__(project_id=project_id)
        self.client = MockFeishuClient()


class BaselineSystemUnderTest:
    """Existing pipeline driven through FeishuWebhookHandler (ADR-0008 baseline).

    Each sample runs against a fresh in-memory database seeded with the fixed
    project fixture, so samples stay independent.
    """

    def __init__(self, project: IntentProjectFixture) -> None:
        self._project = project

    async def observe(self, sample: EvalSample) -> ObservedBehavior:
        db = Database("sqlite+aiosqlite:///:memory:")
        await db.init_db()
        try:
            adapter = _RecordingAdapter(self._project.project_id)
            service = GatewayService(db, adapter_factory=lambda _project: adapter)
            await service.ingest_raw_events(self._project.project_id, self._fixture_events())
            coordinator = await service.get_sync_coordinator(self._project.project_id)
            if sample.context is not None and sample.context.pending_question is not None:
                self._construct_pending_session(coordinator, sample)

            sessions_before = len(coordinator._sessions)
            sent_before = len(adapter.client.sent_messages)

            handler = FeishuWebhookHandler(service=service, project_id=self._project.project_id)
            response = await handler.handle_event(self._message_payload(sample))

            stored_events = await service.event_store.get_events(self._project.project_id)
            return self._collect_behavior(
                response,
                stored_events,
                coordinator=coordinator,
                sessions_before=sessions_before,
                confirmation_sent=len(adapter.client.sent_messages) > sent_before,
            )
        finally:
            await db.close()

    def _fixture_events(self) -> list[dict[str, Any]]:
        occurred_at = FIXTURE_OCCURRED_AT
        events: list[dict[str, Any]] = []
        for member in self._project.members:
            events.append(
                {
                    "schema_version": 1,
                    "project_id": self._project.project_id,
                    "event_id": f"evt-fixture-member-{member['member_id']}",
                    "event_type": "member.registered",
                    "source": "human",
                    "source_ref": FIXTURE_SOURCE_REF,
                    "occurred_at": occurred_at,
                    "received_at": occurred_at,
                    "payload": {
                        "member_id": member["member_id"],
                        "display_name": member["display_name"],
                        "role": member["role"],
                    },
                }
            )
        for task in self._project.tasks:
            payload: dict[str, Any] = {
                "task_id": task["task_id"],
                "title": task["title"],
                "owner_id": task["owner_id"],
                "workflow_status": task["workflow_status"],
                "deadline": task["deadline"],
            }
            if task.get("dependencies"):
                payload["dependencies"] = list(task["dependencies"])
            events.append(
                {
                    "schema_version": 1,
                    "project_id": self._project.project_id,
                    "event_id": f"evt-fixture-task-{task['task_id']}",
                    "event_type": "task.created",
                    "source": "task",
                    "source_ref": FIXTURE_SOURCE_REF,
                    "occurred_at": occurred_at,
                    "received_at": occurred_at,
                    "payload": payload,
                }
            )
        return events

    def _construct_pending_session(self, coordinator: Any, sample: EvalSample) -> None:
        """Builds the active clarification session a pending-question sample answers."""
        pending = sample.context.pending_question if sample.context else None
        if pending is None:
            return
        session = coordinator.start_sync_session(
            self._project.project_id, initiated_by=self._project.pm_id
        )
        for member_id, probe in session.member_probes.items():
            if member_id == sample.sender:
                probe.status = ProbeMemberStatus.CLARIFYING
                probe.clarification_questions.append(pending.question)
            else:
                probe.status = ProbeMemberStatus.COLLECTED
        if sample.sender not in session.member_probes:
            session.member_probes[sample.sender] = MemberProbeState(
                member_id=sample.sender,
                display_name=sample.sender,
                status=ProbeMemberStatus.CLARIFYING,
                clarification_questions=[pending.question],
            )
        # Anchor the idle-timeout clock to the sample time so the constructed
        # session stays active no matter when the suite runs.
        session.updated_at = sample.occurred_at

    def _message_payload(self, sample: EvalSample) -> dict[str, Any]:
        create_time = str(int(sample.occurred_at.timestamp() * 1000))
        return {
            "header": {
                "event_id": f"evt-{sample.id}",
                "event_type": "im.message.receive_v1",
                "create_time": create_time,
            },
            "event": {
                "sender": {"sender_id": {"open_id": sample.sender}, "sender_type": "user"},
                "message": {
                    "message_id": f"om-{sample.id}",
                    "message_type": "text",
                    "content": json.dumps({"text": sample.message}, ensure_ascii=False),
                    "create_time": create_time,
                    "chat_type": "p2p",
                },
            },
        }

    def _collect_behavior(
        self,
        response: dict[str, Any],
        stored_events: list[OrgEvent],
        *,
        coordinator: Any,
        sessions_before: int,
        confirmation_sent: bool,
    ) -> ObservedBehavior:
        claims: list[ObservedClaim] = []
        commitments: list[ObservedCommitment] = []
        for event in stored_events:
            if event.event_type == "task.health_reported":
                health = event.payload.health_status
                claims.append(
                    ObservedClaim(
                        task_id=event.payload.task_id,
                        health_status=str(health.value if hasattr(health, "value") else health),
                        expected_completion=event.payload.expected_completion,
                    )
                )
            elif event.event_type == "commitment.made":
                commitments.append(
                    ObservedCommitment(
                        task_id=event.payload.target_id,
                        predicate=str(event.payload.predicate),
                        expected_value=str(event.payload.expected_value),
                        due_at=event.payload.due_at,
                    )
                )

        msg = response.get("msg")
        starts_sync = msg == "sync_session_started" or len(coordinator._sessions) > sessions_before
        # ADR-0008 §4: the confirmation card belongs to the sync entry flow.
        sends_confirmation_card = msg == "sync_session_started" and confirmation_sent
        asks_clarification = msg == "clarification_sent"

        if msg == "sync_session_started":
            reply_category = ReplyCategory.SYNC_CONFIRM
        elif claims or commitments:
            reply_category = ReplyCategory.CLAIM_RECORDED
        elif msg == "clarification_sent":
            reply_category = ReplyCategory.CLARIFY
        elif msg in {"sender_not_registered", "message_rejected"}:
            reply_category = ReplyCategory.NO_PERMISSION
        else:
            reply_category = ReplyCategory.SMALLTALK

        return ObservedBehavior(
            claims=claims,
            commitments=commitments,
            starts_sync=starts_sync,
            sends_confirmation_card=sends_confirmation_card,
            asks_clarification=asks_clarification,
            reply_category=reply_category,
        )


# ---------------------------------------------------------------------------
# Comparison and metrics
# ---------------------------------------------------------------------------


class SampleComparison(StrictModel):
    sample_id: str
    category: str
    has_pending_question: bool = False
    expected: ExpectedBehavior
    observed: ObservedBehavior
    issues: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


class CategoryStats(StrictModel):
    category: str
    samples: int
    false_claims: int
    sync_false_triggers: int
    reply_correct: int
    claim_precision: float
    claim_recall: float


class IntentEvalMetrics(StrictModel):
    total_samples: int
    expected_claims_total: int
    observed_claims_total: int
    claim_true_positives: int
    claim_precision: float
    claim_recall: float
    task_id_accuracy: float
    health_status_accuracy: float
    time_accuracy: float
    false_claim_samples: int
    no_claim_expected_samples: int
    false_claim_rate: float
    sync_false_trigger_rate: float
    reply_category_accuracy: float
    multi_turn_samples: int
    multi_turn_success_rate: float
    per_category: list[CategoryStats]
    intent_metrics: None = Field(
        default=None,
        description="N/A for the baseline: it has no intent layer to score yet",
    )


def _match_claims(
    expected_claims: list[ExpectedClaim],
    observed_claims: list[ObservedClaim],
) -> tuple[list[tuple[ExpectedClaim, ObservedClaim]], list[ObservedClaim]]:
    """Greedy one-to-one match by task_id. Returns (matched pairs, unmatched observed)."""
    remaining = list(observed_claims)
    pairs: list[tuple[ExpectedClaim, ObservedClaim]] = []
    for expected in expected_claims:
        for candidate in remaining:
            if candidate.task_id == expected.task_id:
                pairs.append((expected, candidate))
                remaining.remove(candidate)
                break
    return pairs, remaining


def compare_sample(sample: EvalSample, observed: ObservedBehavior) -> SampleComparison:
    expected = sample.expected.behavior
    issues: list[str] = []
    pairs, unmatched_observed = _match_claims(expected.claims, observed.claims)

    for observed_claim in unmatched_observed:
        issues.append(f"错误 claim：不应在 {observed_claim.task_id} 上生成状态")
    for expected_claim, observed_claim in pairs:
        if observed_claim.health_status != expected_claim.health_status:
            issues.append(
                f"健康度不符：{expected_claim.task_id} 期望 {expected_claim.health_status}，"
                f"实际 {observed_claim.health_status}"
            )
        if expected_claim.expected_completion is not None:
            if observed_claim.expected_completion is None:
                issues.append(f"时间缺失：{expected_claim.task_id} 未解析出完成时间")
            elif observed_claim.expected_completion != expected_claim.expected_completion:
                issues.append(
                    f"时间不符：{expected_claim.task_id} 期望 "
                    f"{expected_claim.expected_completion.isoformat()}，"
                    f"实际 {observed_claim.expected_completion.isoformat()}"
                )
    for expected_claim in expected.claims:
        if all(pair[0] is not expected_claim for pair in pairs):
            issues.append(f"遗漏 claim：{expected_claim.task_id} 未生成状态")

    observed_commitments = {
        (c.task_id, c.predicate, c.expected_value) for c in observed.commitments
    }
    expected_commitments = {
        (c.task_id, c.predicate, c.expected_value) for c in expected.commitments
    }
    for commitment in sorted(observed_commitments - expected_commitments):
        issues.append(f"错误 commitment：{commitment}")
    for commitment in sorted(expected_commitments - observed_commitments):
        issues.append(f"遗漏 commitment：{commitment}")

    if observed.starts_sync != expected.starts_sync:
        issues.append(
            f"同步触发不符：期望 {'发起' if expected.starts_sync else '不发起'}，"
            f"实际 {'发起了' if observed.starts_sync else '未发起'}"
        )
    if observed.sends_confirmation_card != expected.sends_confirmation_card:
        issues.append(
            f"确认卡片不符：期望 {'发送' if expected.sends_confirmation_card else '不发送'}，"
            f"实际 {'发送了' if observed.sends_confirmation_card else '未发送'}"
        )
    if observed.asks_clarification != expected.asks_clarification:
        issues.append(
            f"追问不符：期望 {'追问' if expected.asks_clarification else '不追问'}，"
            f"实际 {'追问了' if observed.asks_clarification else '未追问'}"
        )
    if observed.reply_category != expected.reply_category:
        issues.append(
            f"回复类别不符：期望 {expected.reply_category.value}，"
            f"实际 {observed.reply_category.value}"
        )

    return SampleComparison(
        sample_id=sample.id,
        category=sample.category,
        has_pending_question=(
            sample.context is not None and sample.context.pending_question is not None
        ),
        expected=expected,
        observed=observed,
        issues=issues,
    )


def compute_metrics(comparisons: list[SampleComparison]) -> IntentEvalMetrics:
    total_observed_claims = sum(len(c.observed.claims) for c in comparisons)
    total_expected_claims = sum(len(c.expected.claims) for c in comparisons)
    total_task_matches = 0
    total_health_matches = 0
    total_time_matches = 0
    total_time_expected = 0
    total_true_positives = 0
    false_claim_samples = 0
    no_claim_expected_samples = 0
    sync_false_triggers = 0
    no_sync_expected = 0
    reply_correct = 0
    multi_turn_total = 0
    multi_turn_success = 0

    per_category: dict[str, CategoryStats] = {}
    for comparison in comparisons:
        expected = comparison.expected
        observed = comparison.observed
        pairs, unmatched_observed = _match_claims(expected.claims, observed.claims)
        task_matches = len(pairs)
        health_matches = sum(
            1 for expected_claim, observed_claim in pairs
            if expected_claim.health_status == observed_claim.health_status
        )
        time_pairs = [
            (expected_claim, observed_claim)
            for expected_claim, observed_claim in pairs
            if expected_claim.expected_completion is not None
        ]
        time_matches = sum(
            1
            for expected_claim, observed_claim in time_pairs
            if observed_claim.expected_completion == expected_claim.expected_completion
        )
        total_task_matches += task_matches
        total_health_matches += health_matches
        total_time_matches += time_matches
        total_time_expected += len(time_pairs)
        total_true_positives += health_matches

        is_false_claim = not expected.claims and bool(observed.claims)
        if not expected.claims:
            no_claim_expected_samples += 1
            if is_false_claim:
                false_claim_samples += 1
        if not expected.starts_sync:
            no_sync_expected += 1
            if observed.starts_sync:
                sync_false_triggers += 1
        reply_ok = observed.reply_category == expected.reply_category
        if reply_ok:
            reply_correct += 1
        if comparison.has_pending_question:
            multi_turn_total += 1
            if (
                reply_ok
                and not is_false_claim
                and observed.asks_clarification == expected.asks_clarification
            ):
                multi_turn_success += 1

        stats = per_category.setdefault(
            comparison.category,
            CategoryStats(
                category=comparison.category,
                samples=0,
                false_claims=0,
                sync_false_triggers=0,
                reply_correct=0,
                claim_precision=0.0,
                claim_recall=0.0,
            ),
        )
        stats.samples += 1
        stats.false_claims += int(is_false_claim)
        stats.sync_false_triggers += int(not expected.starts_sync and observed.starts_sync)
        stats.reply_correct += int(reply_ok)

    for stats in per_category.values():
        category_comparisons = [c for c in comparisons if c.category == stats.category]
        cat_observed = sum(len(c.observed.claims) for c in category_comparisons)
        cat_expected = sum(len(c.expected.claims) for c in category_comparisons)
        cat_tp = sum(
            1
            for c in category_comparisons
            for expected_claim, observed_claim in _match_claims(
                c.expected.claims, c.observed.claims
            )[0]
            if expected_claim.health_status == observed_claim.health_status
        )
        stats.claim_precision = cat_tp / cat_observed if cat_observed else 0.0
        stats.claim_recall = cat_tp / cat_expected if cat_expected else 0.0

    return IntentEvalMetrics(
        total_samples=len(comparisons),
        expected_claims_total=total_expected_claims,
        observed_claims_total=total_observed_claims,
        claim_true_positives=total_true_positives,
        claim_precision=(
            total_true_positives / total_observed_claims if total_observed_claims else 0.0
        ),
        claim_recall=total_true_positives / total_expected_claims if total_expected_claims else 0.0,
        task_id_accuracy=(
            total_task_matches / total_observed_claims if total_observed_claims else 0.0
        ),
        health_status_accuracy=(
            total_health_matches / total_task_matches if total_task_matches else 0.0
        ),
        time_accuracy=total_time_matches / total_time_expected if total_time_expected else 0.0,
        false_claim_samples=false_claim_samples,
        no_claim_expected_samples=no_claim_expected_samples,
        false_claim_rate=(
            false_claim_samples / no_claim_expected_samples if no_claim_expected_samples else 0.0
        ),
        sync_false_trigger_rate=(
            sync_false_triggers / no_sync_expected if no_sync_expected else 0.0
        ),
        reply_category_accuracy=reply_correct / len(comparisons) if comparisons else 0.0,
        multi_turn_samples=multi_turn_total,
        multi_turn_success_rate=multi_turn_success / multi_turn_total if multi_turn_total else 0.0,
        per_category=sorted(per_category.values(), key=lambda s: s.category),
    )


# ---------------------------------------------------------------------------
# Dataset loading and validation
# ---------------------------------------------------------------------------


def load_intent_project(path: Path) -> IntentProjectFixture:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"intent project fixture must be a mapping: {path}")
    return IntentProjectFixture.model_validate(data)


def load_intent_samples(path: Path) -> list[EvalSample]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"intent dataset must be a mapping: {path}")
    dataset = IntentDataset.model_validate(data)
    seen: set[str] = set()
    for sample in dataset.samples:
        if sample.id in seen:
            raise ValueError(f"duplicate sample id: {sample.id}")
        seen.add(sample.id)
    return dataset.samples


def category_minimum_violations(samples: list[EvalSample]) -> list[str]:
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample.category] = counts.get(sample.category, 0) + 1
    violations: list[str] = []
    for category, minimum in CATEGORY_MINIMUMS.items():
        actual = counts.get(category, 0)
        if actual < minimum:
            violations.append(f"{category}: {actual} < {minimum}")
    unknown = sorted(set(counts) - set(CATEGORY_MINIMUMS))
    if unknown:
        violations.append(f"unknown categories: {unknown}")
    return violations


def validate_samples_against_project(
    samples: list[EvalSample], project: IntentProjectFixture
) -> list[str]:
    issues: list[str] = []
    for sample in samples:
        if sample.sender not in project.member_ids:
            issues.append(f"{sample.id}: unknown sender {sample.sender}")
        for claim in sample.expected.behavior.claims:
            if claim.task_id not in project.task_ids:
                issues.append(f"{sample.id}: unknown task {claim.task_id}")
        for commitment in sample.expected.behavior.commitments:
            if commitment.task_id not in project.task_ids:
                issues.append(f"{sample.id}: unknown task {commitment.task_id}")
        for item in sample.expected.intents.items:
            if item.task_id is not None and item.task_id not in project.task_ids:
                issues.append(f"{sample.id}: unknown task {item.task_id}")
    return issues


# ---------------------------------------------------------------------------
# Report writing and entry point
# ---------------------------------------------------------------------------


def _behavior_view(behavior: ExpectedBehavior | ObservedBehavior) -> dict[str, Any]:
    return {
        "claims": [claim.model_dump(mode="json") for claim in behavior.claims],
        "commitments": [c.model_dump(mode="json") for c in behavior.commitments],
        "starts_sync": behavior.starts_sync,
        "sends_confirmation_card": behavior.sends_confirmation_card,
        "asks_clarification": behavior.asks_clarification,
        "reply_category": behavior.reply_category.value,
    }


def render_markdown_report(
    split: str,
    mode: str,
    metrics: IntentEvalMetrics,
    failures: list[SampleComparison],
) -> str:
    lines: list[str] = []
    lines.append(f"# 意图行为评测报告（split={split}, mode={mode}）")
    lines.append("")
    lines.append("评测以系统行为为准（ADR-0008 §8）；baseline 无意图层，意图指标记为 N/A。")
    lines.append("")
    lines.append("## 总体指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 样本数 | {metrics.total_samples} |")
    lines.append(f"| claim precision | {metrics.claim_precision:.2%} |")
    lines.append(f"| claim recall | {metrics.claim_recall:.2%} |")
    lines.append(f"| task_id 准确率 | {metrics.task_id_accuracy:.2%} |")
    lines.append(f"| 健康状态准确率 | {metrics.health_status_accuracy:.2%} |")
    lines.append(f"| 时间准确率 | {metrics.time_accuracy:.2%} |")
    lines.append(
        f"| 错误 claim 率 | {metrics.false_claim_rate:.2%} "
        f"({metrics.false_claim_samples}/{metrics.no_claim_expected_samples}) |"
    )
    lines.append(f"| 同步误触发率 | {metrics.sync_false_trigger_rate:.2%} |")
    lines.append(f"| 回复类别准确率 | {metrics.reply_category_accuracy:.2%} |")
    lines.append(
        f"| 多轮补全成功率 | {metrics.multi_turn_success_rate:.2%} "
        f"({metrics.multi_turn_samples} 条多轮样本) |"
    )
    lines.append("| 意图层指标 | N/A（baseline 无意图层） |")
    lines.append("")
    lines.append("## 按类别统计")
    lines.append("")
    lines.append("| 类别 | 样本 | 错误 claim | 同步误触发 | 回复正确 | claim P | claim R |")
    lines.append("|---|---|---|---|---|---|---|")
    for stats in metrics.per_category:
        lines.append(
            f"| {stats.category} | {stats.samples} | {stats.false_claims} | "
            f"{stats.sync_false_triggers} | {stats.reply_correct} | "
            f"{stats.claim_precision:.0%} | {stats.claim_recall:.0%} |"
        )
    lines.append("")
    lines.append(f"## 失败样本（{len(failures)} 条）")
    lines.append("")
    for failure in failures:
        lines.append(f"### {failure.sample_id}（{failure.category}）")
        lines.append("")
        for issue in failure.issues:
            lines.append(f"- {issue}")
        expected_view = json.dumps(_behavior_view(failure.expected), ensure_ascii=False)
        observed_view = json.dumps(_behavior_view(failure.observed), ensure_ascii=False)
        lines.append(f"- 期望：`{expected_view}`")
        lines.append(f"- 实际：`{observed_view}`")
        lines.append("")
    if not failures:
        lines.append("（无失败样本）")
        lines.append("")
    return "\n".join(lines)


def write_reports(
    split: str,
    mode: str,
    metrics: IntentEvalMetrics,
    failures: list[SampleComparison],
    report_dir: Path,
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"report-{split}-{mode}.json"
    markdown_path = report_dir / f"report-{split}-{mode}.md"
    payload = {
        "split": split,
        "mode": mode,
        "generated_at": datetime.now(UTC).isoformat(),
        "metrics": metrics.model_dump(mode="json"),
        "failures": [
            {
                "sample_id": failure.sample_id,
                "category": failure.category,
                "issues": failure.issues,
                "expected": _behavior_view(failure.expected),
                "observed": _behavior_view(failure.observed),
            }
            for failure in failures
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(
        render_markdown_report(split, mode, metrics, failures), encoding="utf-8"
    )
    return json_path, markdown_path


async def run_intent_eval(
    split: str,
    mode: str,
    dataset_dir: Path,
    report_dir: Path,
    system: IntentSystemUnderTest | None = None,
) -> tuple[IntentEvalMetrics, list[SampleComparison]]:
    """Loads the dataset, runs the system under test, and writes both reports."""
    project = load_intent_project(dataset_dir / "project.yaml")
    samples = load_intent_samples(dataset_dir / f"{split}.yaml")
    validation_issues = validate_samples_against_project(samples, project)
    if validation_issues:
        raise ValueError(
            "dataset inconsistent with project fixture: " + "; ".join(validation_issues)
        )

    if system is None:
        if mode != "mock":
            raise ValueError(
                f"mode {mode!r} 需要意图层或在线模型；"
                "当前步骤仅支持 mock（ADR-0008 实施顺序第 1 步）"
            )
        system = BaselineSystemUnderTest(project)

    comparisons: list[SampleComparison] = []
    for sample in samples:
        observed = await system.observe(sample)
        comparisons.append(compare_sample(sample, observed))

    metrics = compute_metrics(comparisons)
    failures = [c for c in comparisons if not c.passed]
    write_reports(split, mode, metrics, failures, report_dir)
    return metrics, failures


__all__ = [
    "BaselineSystemUnderTest",
    "CATEGORY_MINIMUMS",
    "IntentEvalMetrics",
    "IntentSystemUnderTest",
    "ObservedBehavior",
    "ReplyCategory",
    "SampleComparison",
    "category_minimum_violations",
    "compare_sample",
    "compute_metrics",
    "load_intent_project",
    "load_intent_samples",
    "run_intent_eval",
    "validate_samples_against_project",
    "write_reports",
]

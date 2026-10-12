"""Multi-turn conversation evaluation (ADR-0008 3a.1).

Each conversation runs on a fresh fixture project through the REAL webhook entry
with an injectable intent client, exactly like the single-sample SUTs. Per-turn
expectations reuse the behavior comparison; each script additionally asserts the
final state (claims, probe collection, briefing flags). Oracle gate: every
conversation passes.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.extraction.models import StrictModel
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.service import GatewayService
from orgpilot.intent.client import IntentClient
from orgpilot.intent.evaluator import (
    EvalSample,
    Expected,
    IntentProjectFixture,
    ObservedBehavior,
    ReplyCategory,
    _collect_claim_events,
    _has_sync_confirmation_marker,
    compare_sample,
)
from orgpilot.storage.database import Database


class ConversationSetup(StrictModel):
    """Initial state for one conversation, applied through public APIs only."""

    start_sync_by: str | None = None
    extra_tasks: list[dict[str, Any]] = Field(default_factory=list)


class ConversationTurn(StrictModel):
    sender: str
    message: str
    after_minutes: int = 0
    message_id: str | None = None
    duplicate: bool = False
    expected: Expected


class ConversationFinal(StrictModel):
    collected: list[str] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    briefing_generated: bool | None = None
    briefing_marks_incomplete: bool | None = None


class ConversationScript(StrictModel):
    id: str
    description: str = ""
    occurred_at_base: datetime
    setup: ConversationSetup = Field(default_factory=ConversationSetup)
    turns: list[ConversationTurn]
    final: ConversationFinal = Field(default_factory=ConversationFinal)


class ConversationDataset(StrictModel):
    schema_version: int
    dataset_id: str
    conversations: list[ConversationScript]


class TurnObservation(StrictModel):
    turn_id: str
    duplicate: bool = False
    observed: ObservedBehavior


class ConversationRunResult(StrictModel):
    script_id: str
    turn_results: list[TurnObservation]
    final_claims: list[dict[str, Any]]
    collected: list[str] = Field(default_factory=list)
    briefing_generated: bool = False
    briefing_marks_incomplete: bool = False
    issues: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


class ConversationSystemUnderTest:
    """Drives one conversation script through the real webhook + MessageRouter."""

    def __init__(
        self,
        project: IntentProjectFixture,
        intent_client: IntentClient | Any,
    ) -> None:
        """``intent_client`` may be a client instance or a per-script factory
        (the oracle builds one client per conversation to scope message keys)."""
        self._project = project
        self._intent_client = intent_client

    def _client_for(self, script: ConversationScript) -> IntentClient:
        if callable(self._intent_client):
            return self._intent_client(script)
        return self._intent_client

    async def run(self, script: ConversationScript) -> ConversationRunResult:
        db = Database("sqlite+aiosqlite:///:memory:")
        await db.init_db()
        try:
            adapter = MockCollaborationAdapter(self._project.project_id)
            adapter.client = MockFeishuClient()
            service = GatewayService(db, adapter_factory=lambda _project: adapter)
            await service.ingest_raw_events(
                self._project.project_id, self._fixture_events(script)
            )
            from orgpilot.intent.router import MessageRouter

            router = MessageRouter(service=service, intent_client=self._client_for(script))
            handler = FeishuWebhookHandler(
                service=service, project_id=self._project.project_id, router=router
            )

            if script.setup.start_sync_by:
                await router.start_sync(
                    self._project.project_id,
                    initiated_by=script.setup.start_sync_by,
                    occurred_at=script.occurred_at_base,
                )

            coordinator = await service.get_sync_coordinator(self._project.project_id)
            turn_results: list[TurnObservation] = []
            issues: list[str] = []
            cursor = script.occurred_at_base
            known_event_ids: set[str] = {
                event.event_id
                for event in await service.event_store.get_events(self._project.project_id)
            }
            outbound_msgs = 0
            outbound_cards = 0

            for index, turn in enumerate(script.turns, start=1):
                cursor = cursor + timedelta(minutes=turn.after_minutes)
                turn_id = f"{script.id}-t{index:02d}"
                sessions_before = len(coordinator._sessions)
                msgs_before, cards_before = outbound_msgs, outbound_cards

                message_id = turn.message_id or f"{script.id}-auto-{index}"
                payload = self._turn_payload(
                    turn.sender, turn.message, occurred_at=cursor, message_id=message_id
                )
                response = await handler.handle_event(payload)

                stored = await service.event_store.get_events(self._project.project_id)
                fresh = [event for event in stored if event.event_id not in known_event_ids]
                known_event_ids.update(event.event_id for event in fresh)
                claims, _commitments = _collect_claim_events(fresh)
                turn_records = (
                    adapter.client.sent_messages[msgs_before:]
                    + adapter.client.sent_cards[cards_before:]
                )
                outbound_msgs, outbound_cards = len(adapter.client.sent_messages), len(
                    adapter.client.sent_cards
                )
                data = response.get("data", {}) or {}
                duplicate = response.get("msg") == "duplicate_suppressed"
                try:
                    reply_category = ReplyCategory(str(data.get("reply_category", "smalltalk")))
                except ValueError:
                    reply_category = ReplyCategory.SMALLTALK
                observed = ObservedBehavior(
                    claims=claims,
                    commitments=[],
                    starts_sync=(
                        response.get("msg") == "sync_session_started"
                        or len(coordinator._sessions) > sessions_before
                    ),
                    sends_confirmation_card=_has_sync_confirmation_marker(turn_records),
                    asks_clarification=(
                        bool(data.get("asks_clarification"))
                        or response.get("msg") == "clarification_sent"
                    ),
                    reply_category=reply_category,
                )
                turn_results.append(
                    TurnObservation(
                        turn_id=turn_id, duplicate=duplicate, observed=observed
                    )
                )

                sample = EvalSample(
                    id=turn_id,
                    category="conversation",
                    message=turn.message,
                    sender=turn.sender,
                    occurred_at=cursor,
                    context=None,
                    expected=turn.expected,
                )
                comparison = compare_sample(sample, observed)
                if turn.duplicate != duplicate:
                    comparison.issues.append(
                        "幂等不符：" + ("预期重复投递" if turn.duplicate else "预期首次处理")
                    )
                if comparison.issues:
                    issues.extend(
                        f"{turn_id}: {issue}" for issue in comparison.issues
                    )

            final_claims, collected, briefing_generated, marks = await self._final_state(
                service, script
            )
            final_issues = self._check_final(
                script, final_claims, collected, briefing_generated, marks
            )
            issues.extend(final_issues)
            return ConversationRunResult(
                script_id=script.id,
                turn_results=turn_results,
                final_claims=final_claims,
                collected=collected,
                briefing_generated=briefing_generated,
                briefing_marks_incomplete=marks,
                issues=issues,
            )
        finally:
            await db.close()

    def _fixture_events(self, script: ConversationScript) -> list[dict[str, Any]]:
        occurred = script.occurred_at_base.isoformat()
        events: list[dict[str, Any]] = []
        for member in self._project.members:
            events.append(
                {
                    "schema_version": 1,
                    "project_id": self._project.project_id,
                    "event_id": f"evt-fixture-member-{member['member_id']}",
                    "event_type": "member.registered",
                    "source": "human",
                    "source_ref": "intent-eval-fixture",
                    "occurred_at": occurred,
                    "received_at": occurred,
                    "payload": {
                        "member_id": member["member_id"],
                        "display_name": member["display_name"],
                        "role": member["role"],
                    },
                }
            )
        tasks = list(self._project.tasks) + list(script.setup.extra_tasks)
        for task in tasks:
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
                    "source_ref": "intent-eval-fixture",
                    "occurred_at": occurred,
                    "received_at": occurred,
                    "payload": payload,
                }
            )
        return events

    @staticmethod
    def _turn_payload(
        sender: str, message: str, *, occurred_at: datetime, message_id: str
    ) -> dict[str, Any]:
        create_time = str(int(occurred_at.timestamp() * 1000))
        return {
            "header": {
                "event_id": f"evt-{message_id}",
                "event_type": "im.message.receive_v1",
                "create_time": create_time,
            },
            "event": {
                "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
                "message": {
                    "message_id": message_id,
                    "message_type": "text",
                    "content": json.dumps({"text": message}, ensure_ascii=False),
                    "create_time": create_time,
                    "chat_type": "p2p",
                },
            },
        }

    async def _final_state(
        self, service: GatewayService, script: ConversationScript
    ) -> tuple[list[dict[str, Any]], list[str], bool, bool]:
        stored = await service.event_store.get_events(self._project.project_id)
        claims, _ = _collect_claim_events(stored)
        claim_views = [
            {
                "task_id": claim.task_id,
                "health_status": claim.health_status,
                "expected_completion": claim.expected_completion,
            }
            for claim in claims
        ]
        collected: list[str] = []
        briefing_generated = False
        marks = False
        coordinator = await service.get_sync_coordinator(self._project.project_id)
        for session in reversed(list(coordinator._sessions.values())):
            if session.project_id != self._project.project_id:
                continue
            collected = [
                member_id
                for member_id, probe in session.member_probes.items()
                if probe.status.value == "collected"
            ]
            briefing_generated = session.briefing is not None
            marks = any(
                getattr(member, "incomplete", False)
                for member in (session.briefing.member_statuses if session.briefing else [])
            )
            break
        return claim_views, collected, briefing_generated, marks

    @staticmethod
    def _check_final(
        script: ConversationScript,
        final_claims: list[dict[str, Any]],
        collected: list[str],
        briefing_generated: bool,
        marks: bool,
    ) -> list[str]:
        issues: list[str] = []
        expected = script.final
        for member_id in expected.collected:
            if member_id not in collected:
                issues.append(f"最终状态：{member_id} 未收集完成")
        for expected_claim in expected.claims:
            task_id = expected_claim["task_id"]
            matches = [c for c in final_claims if c["task_id"] == task_id]
            if not matches:
                issues.append(f"最终状态：{task_id} 缺少 claim")
                continue
            latest = matches[-1]
            if latest["health_status"] != expected_claim["health_status"]:
                issues.append(
                    f"最终状态：{task_id} 健康度期望 {expected_claim['health_status']}，"
                    f"实际 {latest['health_status']}"
                )
            expected_completion = expected_claim.get("expected_completion")
            if expected_completion is not None and latest[
                "expected_completion"
            ] != datetime.fromisoformat(str(expected_completion)):
                issues.append(f"最终状态：{task_id} 完成时间不符")
        if expected.briefing_generated is not None and (
            expected.briefing_generated != briefing_generated
        ):
            issues.append(
                f"最终状态：简报生成期望 {expected.briefing_generated}，实际 {briefing_generated}"
            )
        if expected.briefing_marks_incomplete is not None and (
            expected.briefing_marks_incomplete != marks
        ):
            issues.append(
                "最终状态：简报「信息不全」标注期望 "
                f"{expected.briefing_marks_incomplete}，实际 {marks}"
            )
        return issues


def load_conversations(path: Path) -> list[ConversationScript]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"conversation dataset must be a mapping: {path}")
    dataset = ConversationDataset.model_validate(data)
    seen: set[str] = set()
    for script in dataset.conversations:
        if script.id in seen:
            raise ValueError(f"duplicate conversation id: {script.id}")
        seen.add(script.id)
    return dataset.conversations


def compare_conversation(script: ConversationScript, result: ConversationRunResult) -> list[str]:
    return list(result.issues)


def render_conversation_report(
    results: list[ConversationRunResult],
    title_suffix: str | None = None,
) -> str:
    lines: list[str] = ["# 多轮对话评测报告"]
    if title_suffix:
        lines.append(f"—— {title_suffix}")
    lines.append("")
    passed = sum(1 for result in results if result.passed)
    total_turns = sum(len(result.turn_results) for result in results)
    lines.append(f"- 对话数：{len(results)}（通过 {passed}，失败 {len(results) - passed}）")
    lines.append(f"- 总轮次：{total_turns}")
    lines.append("")
    lines.append("| 对话 | 轮次 | 结果 | 问题 |")
    lines.append("|---|---|---|---|")
    for result in results:
        status = "✅ 通过" if result.passed else "❌ 失败"
        lines.append(
            f"| {result.script_id} | {len(result.turn_results)} | {status} | "
            f"{'; '.join(result.issues) if result.issues else '—'} |"
        )
    lines.append("")
    for result in results:
        if result.passed:
            continue
        lines.append(f"## {result.script_id}")
        lines.append("")
        for turn in result.turn_results:
            lines.append(
                f"- {turn.turn_id}: duplicate={turn.duplicate}, "
                f"reply={turn.observed.reply_category.value}, "
                f"asks={turn.observed.asks_clarification}, claims={len(turn.observed.claims)}"
            )
        lines.append("")
    return "\n".join(lines)


def write_conversation_reports(
    results: list[ConversationRunResult],
    report_dir: Path,
    title_suffix: str | None = None,
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "report-conversations-mock.json"
    markdown_path = report_dir / "report-conversations-mock.md"
    payload = {
        "title_suffix": title_suffix,
        "conversations": [
            {
                "script_id": result.script_id,
                "passed": result.passed,
                "issues": result.issues,
                "collected": result.collected,
                "briefing_generated": result.briefing_generated,
                "briefing_marks_incomplete": result.briefing_marks_incomplete,
                "turns": [
                    {
                        "turn_id": turn.turn_id,
                        "duplicate": turn.duplicate,
                        "observed": turn.observed.model_dump(mode="json"),
                    }
                    for turn in result.turn_results
                ],
            }
            for result in results
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(
        render_conversation_report(results, title_suffix), encoding="utf-8"
    )
    return json_path, markdown_path


async def run_conversation_eval(
    conversations_path: Path,
    report_dir: Path,
    project: IntentProjectFixture,
    intent_client: IntentClient,
    title_suffix: str | None = None,
) -> tuple[int, int, list[ConversationRunResult]]:
    """Runs every conversation; returns (passed, total, results)."""
    conversations = load_conversations(conversations_path)
    system = ConversationSystemUnderTest(project, intent_client)
    results: list[ConversationRunResult] = []
    for script in conversations:
        results.append(await system.run(script))
    write_conversation_reports(results, report_dir, title_suffix=title_suffix)
    passed = sum(1 for result in results if result.passed)
    return passed, len(results), results

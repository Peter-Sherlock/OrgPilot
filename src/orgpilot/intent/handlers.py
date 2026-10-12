"""Layer-2 intent handlers (ADR-0008 §3-§4): turn guarded intent items into effects.

Every item reaches a handler only after ``evaluate_item_guards`` returned KEEP;
guard verdicts ASK/QUERY/NOTED/UNSUPPORTED/NO_PERMISSION short-circuit to canned
replies inside :meth:`IntentHandlers.process_result`. Handlers never talk to
Feishu directly — they contribute reply text and reply categories to one merged
:class:`RouterOutcome`, and the router sends it.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from orgpilot.domain.enums import ClaimStatus, HealthStatus, WorkflowStatus
from orgpilot.domain.models import MemberState, OrgState, TaskState
from orgpilot.extraction.clarification import SlotCompletenessEvaluator
from orgpilot.extraction.event_builder import (
    build_commitment_made_event,
    build_health_reported_event,
    message_identity,
)
from orgpilot.extraction.models import ExtractedHealthClaim, ExtractionResult
from orgpilot.feishu.cards import build_sync_confirmation_card
from orgpilot.intent.client import IntentContext
from orgpilot.intent.dialogue import (
    CLARIFICATION_SOURCE_CLARIFY,
    DialogueStateStore,
    PendingClaimDraft,
)
from orgpilot.intent.guards import evaluate_item_guards
from orgpilot.intent.models import IntentItem, IntentResult, IntentType, Polarity
from orgpilot.intent.replies import (
    HELP_TEXT,
    ReplyKind,
    RouterOutcome,
    merge_reply_kinds,
)

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService

logger = logging.getLogger(__name__)

HEALTH_CN = {
    HealthStatus.ON_TRACK.value: "正常",
    HealthStatus.AT_RISK.value: "有风险",
    HealthStatus.DELAYED.value: "延期",
    HealthStatus.UNKNOWN.value: "未知",
}

WORKFLOW_CN = {
    WorkflowStatus.TODO.value: "未开始",
    WorkflowStatus.DOING.value: "进行中",
    WorkflowStatus.BLOCKED.value: "受阻",
    WorkflowStatus.REVIEW.value: "验收中",
    WorkflowStatus.DONE.value: "已完成",
}


class IntentHandlers:
    """Executes guarded intent items against the coordination kernel."""

    def __init__(
        self,
        service: "GatewayService",
        dialogue: DialogueStateStore,
        confidence_threshold: float = 0.6,
    ) -> None:
        self.service = service
        self.dialogue = dialogue
        self.confidence_threshold = confidence_threshold
        self._send_text: Callable[[str, str], Awaitable[None]] | None = None
        self._send_card: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

    def bind_senders(
        self,
        send_text: Callable[[str, str], Awaitable[None]],
        send_card: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Binds the outbound messengers (bound per project by the router)."""
        self._send_text = send_text
        self._send_card = send_card

    # ------------------------------------------------------------------ entry

    async def process_result(
        self,
        *,
        project_id: str,
        member: MemberState,
        message: str,
        occurred_at: datetime,
        result: IntentResult,
        context: IntentContext,
        message_id: str | None,
    ) -> RouterOutcome:
        parts: list[str] = []
        kinds: list[ReplyKind] = []
        asks = False
        started_sync = False
        data: dict[str, Any] = {"is_actionable": False}
        message_key, source_ref = message_identity(
            project_id, member.member_id, occurred_at, message, message_id
        )
        agent = await self.service.get_or_replay_agent(project_id)
        event_counter = 0

        for item in result.items:
            guard = evaluate_item_guards(item, message, context, self.confidence_threshold)
            if guard.verdict == "DROP":
                logger.info("Dropped intent item %s: %s", item.intent, guard.reason)
                continue
            if guard.verdict == "ASK":
                parts.append(guard.ask_question or "")
                kinds.append(ReplyKind.CLARIFY)
                asks = True
                self._remember_guard_clarification(
                    project_id, member.member_id, item, guard, occurred_at
                )
                continue
            if guard.verdict == "QUERY":
                parts.append(self._query_answer(agent.projector.state, item.task_id, occurred_at))
                kinds.append(ReplyKind.QUERY_ANSWER)
                continue
            if guard.verdict == "NOTED":
                parts.append("了解，如果确认需要调整请告诉我")
                kinds.append(ReplyKind.NOTED)
                continue
            if guard.verdict == "UNSUPPORTED":
                parts.append("这个需求已记录，暂不支持自动处理。")
                kinds.append(ReplyKind.UNSUPPORTED)
                continue
            if guard.verdict == "NO_PERMISSION":
                parts.append("只有项目负责人（PM）可以发起全员同步。")
                kinds.append(ReplyKind.NO_PERMISSION)
                continue

            # KEEP — dispatch by intent
            if (
                item.intent == IntentType.ANSWER_PENDING
                and result.answers_pending
                and context.pending_question is not None
            ):
                event_counter += 1
                asks = await self._handle_answer(
                    project_id=project_id,
                    member=member,
                    message=message,
                    occurred_at=occurred_at,
                    item=item,
                    source_ref=source_ref,
                    message_key=message_key,
                    event_counter=event_counter,
                    parts=parts,
                    kinds=kinds,
                    data=data,
                    asks=asks,
                )
            elif item.intent in (
                IntentType.REPORT_STATUS,
                IntentType.MAKE_COMMITMENT,
                IntentType.ANSWER_PENDING,
            ):
                event_counter += 1
                await self._record_claim(
                    project_id=project_id,
                    member=member,
                    occurred_at=occurred_at,
                    item=item,
                    source_ref=source_ref,
                    message_key=message_key,
                    event_counter=event_counter,
                    parts=parts,
                    kinds=kinds,
                    data=data,
                )
            elif item.intent == IntentType.QUERY_STATUS:
                parts.append(self._query_answer(agent.projector.state, item.task_id, occurred_at))
                kinds.append(ReplyKind.QUERY_ANSWER)
            elif item.intent == IntentType.REQUEST_SYNC:
                started = await self._handle_request_sync(
                    project_id=project_id,
                    member=member,
                    occurred_at=occurred_at,
                    item=item,
                    state=agent.projector.state,
                    parts=parts,
                    kinds=kinds,
                )
                started_sync = started_sync or started
            elif item.intent == IntentType.ACKNOWLEDGE:
                started = await self._handle_acknowledge(
                    project_id=project_id,
                    member=member,
                    occurred_at=occurred_at,
                    item=item,
                    parts=parts,
                    kinds=kinds,
                )
                started_sync = started_sync or started
            elif item.intent == IntentType.CHITCHAT:
                parts.append("收到～")
                kinds.append(ReplyKind.SMALLTALK)
            elif item.intent == IntentType.HELP:
                parts.append(HELP_TEXT)
                kinds.append(ReplyKind.HELP)
            else:  # OUT_OF_SCOPE and any defensive fall-through
                parts.append("这个需求已记录，暂不支持自动处理。")
                kinds.append(ReplyKind.UNSUPPORTED)

        # R9 topic switch: a pending question exists but this message did not
        # answer it — re-ask at the end WITHOUT counting another round.
        pending = self.dialogue.get_pending_clarification(
            project_id, member.member_id, now=occurred_at
        )
        if pending is not None and not result.answers_pending:
            parts.append(f"另外，还想追问一下：{pending.question}")
            asks = True

        merged = merge_reply_kinds(kinds) or ReplyKind.SMALLTALK
        msg = "success"
        if started_sync:
            msg = "sync_session_started"
        elif data.get("rejected"):
            msg = "message_rejected"
        elif asks and merged == ReplyKind.CLARIFY:
            msg = "clarification_sent"
        data["reply_category"] = merged.value
        data["asks_clarification"] = asks
        return RouterOutcome(
            reply_category=merged,
            reply_text="\n".join(fragment for fragment in parts if fragment),
            asks_clarification=asks,
            msg=msg,
            data=data,
        )

    # ------------------------------------------------------- claim recording

    async def _record_claim(
        self,
        *,
        project_id: str,
        member: MemberState,
        occurred_at: datetime,
        item: IntentItem,
        source_ref: str,
        message_key: str,
        event_counter: int,
        parts: list[str],
        kinds: list[ReplyKind],
        data: dict[str, Any],
    ) -> None:
        if item.intent == IntentType.MAKE_COMMITMENT:
            event = build_commitment_made_event(
                project_id=project_id,
                actor_id=member.member_id,
                occurred_at=occurred_at,
                source_ref=source_ref,
                event_id=f"evt-intent-cmt-{message_key}-{event_counter}",
                commitment_id=f"commitment-{item.task_id}-{message_key}",
                target_id=item.task_id or "",
                due_at=item.expected_completion,
            )
        else:
            event = build_health_reported_event(
                project_id=project_id,
                actor_id=member.member_id,
                occurred_at=occurred_at,
                source_ref=source_ref,
                event_id=f"evt-intent-claim-{message_key}-{event_counter}",
                task_id=item.task_id or "",
                health_status=item.health_status or HealthStatus.UNKNOWN.value,
                expected_completion=item.expected_completion,
                blocker=item.blocker,
                confidence=item.confidence,
            )
        agent, reason, round_num, error = await self.service.commit_extracted_events(
            project_id,
            [event],
            actor_id=member.member_id,
            occurred_at=occurred_at,
        )
        if error is not None:
            data["rejected"] = error
            parts.append(f"消息处理失败：{error}")
            kinds.append(ReplyKind.NOTED)
            return
        data["is_actionable"] = True
        data["turn_termination_reason"] = reason
        data["turn_round_number"] = round_num
        parts.append(self._summarize_item(item, agent.projector.state, occurred_at))
        kinds.append(ReplyKind.CLAIM_RECORDED)

    def _summarize_item(
        self, item: IntentItem, state: OrgState, occurred_at: datetime
    ) -> str:
        title = item.task_id or ""
        if item.task_id in state.tasks:
            title = state.tasks[item.task_id].title

        def _fmt(moment: datetime | None) -> str:
            if moment is None:
                return ""
            tz = occurred_at.tzinfo
            return moment.astimezone(tz).strftime("%m/%d %H:%M")

        if item.intent == IntentType.MAKE_COMMITMENT:
            suffix = (
                f"，承诺 {_fmt(item.expected_completion)} 前完成"
                if item.expected_completion
                else ""
            )
            return f"已记录：{title} → 已收到承诺{suffix}"
        health = HEALTH_CN.get(item.health_status or "", item.health_status or "未知")
        text = f"已记录：{title} → {health}"
        if item.expected_completion is not None:
            text += f"，预计 {_fmt(item.expected_completion)} 完成"
        if item.blocker:
            text += f"（阻塞：{item.blocker}）"
        return text

    # ------------------------------------------------------- answer merging

    async def _handle_answer(
        self,
        *,
        project_id: str,
        member: MemberState,
        message: str,
        occurred_at: datetime,
        item: IntentItem,
        source_ref: str,
        message_key: str,
        event_counter: int,
        parts: list[str],
        kinds: list[ReplyKind],
        data: dict[str, Any],
        asks: bool,
    ) -> bool:
        entry = self.dialogue.get_pending_clarification(
            project_id, member.member_id, now=occurred_at
        )
        draft = entry.claim if entry is not None and entry.claim else PendingClaimDraft()
        target_task = item.task_id or (entry.task_id if entry else None) or draft.task_id
        health = item.health_status or draft.health_status or HealthStatus.AT_RISK.value
        completion = item.expected_completion or draft.expected_completion
        blocker = item.blocker or draft.blocker

        event = build_health_reported_event(
            project_id=project_id,
            actor_id=member.member_id,
            occurred_at=occurred_at,
            source_ref=source_ref,
            event_id=f"evt-intent-claim-{message_key}-{event_counter}",
            task_id=target_task or "",
            health_status=health,
            expected_completion=completion,
            blocker=blocker,
            confidence=item.confidence,
        )
        agent, reason, round_num, error = await self.service.commit_extracted_events(
            project_id,
            [event],
            actor_id=member.member_id,
            occurred_at=occurred_at,
        )
        if error is not None:
            data["rejected"] = error
            parts.append(f"消息处理失败：{error}")
            kinds.append(ReplyKind.NOTED)
            return asks
        data["is_actionable"] = True
        data["turn_termination_reason"] = reason
        data["turn_round_number"] = round_num

        merged = PendingClaimDraft(
            task_id=target_task,
            health_status=health,
            expected_completion=completion,
            blocker=blocker,
        )
        extraction_view = ExtractionResult(
            is_actionable=True,
            claims=[
                ExtractedHealthClaim(
                    task_id=target_task or "",
                    health_status=HealthStatus(health),
                    expected_completion=completion,
                    blocker=blocker,
                    confidence=1.0,
                    source_quote=item.source_quote,
                )
            ],
        )
        complete, missing = SlotCompletenessEvaluator.evaluate_completeness(
            extraction_view, message
        )
        title = target_task or ""
        if target_task in agent.projector.state.tasks:
            title = agent.projector.state.tasks[target_task].title
        item_for_summary = item.model_copy(update={"task_id": target_task})
        summary = self._summarize_item(item_for_summary, agent.projector.state, occurred_at)

        if complete:
            self.dialogue.clear_pending_clarification(project_id, member.member_id)
            parts.append(f"{summary}，信息完整，谢谢！")
        elif entry is None or entry.asked_rounds < self.dialogue.max_clarification_rounds:
            question = SlotCompletenessEvaluator.generate_clarification_question(
                title, missing, message
            )
            if entry is None:
                self.dialogue.register_pending_clarification(
                    project_id,
                    member.member_id,
                    question=question,
                    task_id=target_task,
                    source=CLARIFICATION_SOURCE_CLARIFY,
                    claim=merged,
                    missing_slots=missing,
                    created_at=occurred_at,
                )
            else:
                entry.claim = merged
                entry.missing_slots = missing
                entry.asked_rounds += 1
            parts.append(f"{summary}。{question}")
            asks = True
        else:
            self.dialogue.clear_pending_clarification(project_id, member.member_id)
            parts.append(f"{summary}（已达追问上限，先按当前信息记录。）")
        kinds.append(ReplyKind.CLAIM_RECORDED)
        return asks

    def _remember_guard_clarification(
        self,
        project_id: str,
        sender_id: str,
        item: IntentItem,
        guard: Any,
        occurred_at: datetime,
    ) -> None:
        entry = self.dialogue.get_pending_clarification(
            project_id, sender_id, now=occurred_at
        )
        if entry is not None:
            return
        self.dialogue.register_pending_clarification(
            project_id,
            sender_id,
            question=guard.ask_question or "",
            task_id=item.task_id,
            source=CLARIFICATION_SOURCE_CLARIFY,
            claim=PendingClaimDraft(
                task_id=item.task_id, health_status=item.health_status
            ),
            created_at=occurred_at,
        )

    # ------------------------------------------------------- queries

    def _query_answer(
        self, state: OrgState, task_id: str | None, occurred_at: datetime
    ) -> str:
        def _fmt(moment: datetime | None) -> str:
            if moment is None:
                return "未设置"
            return moment.astimezone(occurred_at.tzinfo).strftime("%m/%d %H:%M")

        if task_id and task_id in state.tasks:
            return self._describe_task(state, state.tasks[task_id], _fmt)
        unfinished = [
            task for task in state.tasks.values() if task.workflow_status != WorkflowStatus.DONE
        ]
        if not unfinished:
            return "当前没有未完成的任务。"
        header = f"当前共 {len(unfinished)} 个未完成任务："
        lines = []
        for task in unfinished:
            workflow_cn = WORKFLOW_CN.get(task.workflow_status.value, task.workflow_status.value)
            health_cn = HEALTH_CN.get(task.health_status.value, task.health_status.value)
            lines.append(f"• {task.title}（{workflow_cn}，健康度 {health_cn}）")
        body = "\n".join(lines)
        return f"{header}\n{body}"

    def _describe_task(
        self, state: OrgState, task: TaskState, fmt: Callable[[datetime | None], str]
    ) -> str:
        workflow_cn = WORKFLOW_CN.get(task.workflow_status.value, task.workflow_status.value)
        health_cn = HEALTH_CN.get(task.health_status.value, task.health_status.value)
        lines = [
            f"{task.title}：{workflow_cn}，健康度 {health_cn}，截止 {fmt(task.deadline)}"
        ]
        active_claims = [
            claim
            for claim in state.health_claims.values()
            if claim.task_id == task.task_id and claim.status == ClaimStatus.ACTIVE
        ]
        if active_claims:
            latest = max(active_claims, key=lambda claim: claim.occurred_at)
            speaker = latest.stated_by or "未知成员"
            member_state = state.members.get(latest.stated_by or "")
            if member_state is not None:
                speaker = member_state.display_name
            detail = f"最新进展由 {speaker} 于 {fmt(latest.occurred_at)} 提供"
            if latest.expected_completion is not None:
                detail += f"，预计 {fmt(latest.expected_completion)} 完成"
            lines.append(detail)
        return "\n".join(lines)

    # ------------------------------------------------------- sync flows

    async def _handle_request_sync(
        self,
        *,
        project_id: str,
        member: MemberState,
        occurred_at: datetime,
        item: IntentItem,
        state: OrgState,
        parts: list[str],
        kinds: list[ReplyKind],
    ) -> bool:
        if item.polarity == Polarity.NEGATIVE:
            parts.append("好的，本次不发起全员同步。")
            kinds.append(ReplyKind.NOTED)
            return False
        member_ids = sorted(
            {
                task.owner_id
                for task in state.tasks.values()
                if task.workflow_status != WorkflowStatus.DONE
            }
            - {member.member_id}
        )
        card = build_sync_confirmation_card(
            initiated_by=member.member_id,
            member_ids=member_ids,
            requested_at_str=occurred_at.isoformat(),
        )
        if self._send_card is not None:
            await self._send_card(member.member_id, card)
        self.dialogue.register_pending_sync_confirmation(
            project_id,
            member.member_id,
            initiated_by=member.member_id,
            member_ids=member_ids,
            created_at=occurred_at,
        )
        parts.append(
            f"已生成同步确认卡片：确认后将向 {len(member_ids)} 位负责人发送进度探针。"
        )
        kinds.append(ReplyKind.SYNC_CONFIRM)
        return False

    async def _handle_acknowledge(
        self,
        *,
        project_id: str,
        member: MemberState,
        occurred_at: datetime,
        item: IntentItem,
        parts: list[str],
        kinds: list[ReplyKind],
    ) -> bool:
        confirmation = self.dialogue.get_pending_sync_confirmation(
            project_id, member.member_id, now=occurred_at
        )
        if confirmation is None:
            parts.append("收到。")
            kinds.append(ReplyKind.SMALLTALK)
            return False
        if item.polarity == Polarity.AFFIRMATIVE:
            session = await self.service.start_progress_sync(
                project_id, initiated_by=confirmation.initiated_by
            )
            self.dialogue.clear_pending_sync_confirmation(project_id, member.member_id)
            parts.append(
                f"已发起全员进度同步：已向 {len(session.member_probes)} 位负责人发送进度探针。"
            )
            kinds.append(ReplyKind.SYNC_STARTED)
            return True
        self.dialogue.clear_pending_sync_confirmation(project_id, member.member_id)
        parts.append("好的，已取消本次同步确认。")
        kinds.append(ReplyKind.NOTED)
        return False

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
    CLARIFICATION_SOURCE_PROBE,
    DialogueStateStore,
    PendingClaimDraft,
)
from orgpilot.intent.guards import evaluate_item_guards
from orgpilot.intent.models import (
    Certainty,
    IntentItem,
    IntentResult,
    IntentType,
    Polarity,
    SpeechAct,
)
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
        self._session_registrar: Callable[[str, Any, datetime], None] | None = None

    def bind_senders(
        self,
        send_text: Callable[[str, str], Awaitable[None]],
        send_card: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Binds the outbound messengers (bound per project by the router)."""
        self._send_text = send_text
        self._send_card = send_card

    def bind_session_registrar(
        self, registrar: Callable[[str, Any, datetime], None] | None
    ) -> None:
        """Binds the hook that registers probe clarifications for a new session."""
        self._session_registrar = registrar

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
        counter = [0]
        answer_items: list[IntentItem] = []

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

            # KEEP — dispatch by intent. Answers to a pending clarification are
            # collected and merged in ONE batch so a multi-task probe asks only
            # once about whatever is still missing (3a.1). The intent client may
            # label the items report_status (heuristic) or answer_pending
            # (oracle/model): the result-level answers_pending flag decides.
            if (
                result.answers_pending
                and context.pending_question is not None
                and item.intent
                in (IntentType.REPORT_STATUS, IntentType.ANSWER_PENDING)
            ):
                answer_items.append(item)
                continue
            elif item.intent in (
                IntentType.REPORT_STATUS,
                IntentType.MAKE_COMMITMENT,
                IntentType.ANSWER_PENDING,
            ):
                counter[0] += 1
                await self._record_claim(
                    project_id=project_id,
                    member=member,
                    occurred_at=occurred_at,
                    item=item,
                    source_ref=source_ref,
                    message_key=message_key,
                    event_counter=counter[0],
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

        # Answers to a pending clarification merge in one batch (3a.1).
        if answer_items:
            asks = await self._handle_answer_batch(
                project_id=project_id,
                member=member,
                message=message,
                occurred_at=occurred_at,
                items=answer_items,
                source_ref=source_ref,
                message_key=message_key,
                counter=counter,
                agent=agent,
                parts=parts,
                kinds=kinds,
                data=data,
                asks=asks,
            )

        # R9 topic switch: a pending question exists but this message did not
        # answer it — re-ask at the end WITHOUT counting another round.
        pending = self.dialogue.get_pending_clarification(
            project_id, member.member_id, now=occurred_at
        )
        if pending is not None and not result.answers_pending:
            parts.append(f"另外，还想追问一下：{pending.question}")
            asks = True
            if pending.source == CLARIFICATION_SOURCE_PROBE:
                data["probe_reply"] = True

        merged = merge_reply_kinds(kinds) or ReplyKind.SMALLTALK
        msg = "success"
        if started_sync:
            msg = "sync_session_started"
        elif data.get("rejected"):
            msg = "message_rejected"
        elif asks:
            # Any turn that ends with a follow-up question counts as a
            # clarification turn, even when a claim was also recorded (3a.1).
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

    async def _handle_answer_batch(
        self,
        *,
        project_id: str,
        member: MemberState,
        message: str,
        occurred_at: datetime,
        items: list[IntentItem],
        source_ref: str,
        message_key: str,
        counter: list[int],
        agent: Any,
        parts: list[str],
        kinds: list[ReplyKind],
        data: dict[str, Any],
        asks: bool,
    ) -> bool:
        """Merges one message's answers into the pending clarification (3a.1).

        Scope: the pending entry's target tasks (a probe targets ALL tasks the
        member owns). An item naming a task updates that task; an item WITHOUT a
        task (e.g. "都正常") applies to every target task. Completion requires
        each target task to carry a complete claim: on_track needs nothing,
        at_risk needs a completion time, delayed needs time AND blocker.
        """
        entry = self.dialogue.get_pending_clarification(
            project_id, member.member_id, now=occurred_at
        )
        probe_source = entry is not None and entry.source == CLARIFICATION_SOURCE_PROBE
        drafts: dict[str, PendingClaimDraft] = dict(entry.claims) if entry else {}
        target_tasks = list(entry.task_ids) if entry else []
        if not target_tasks:
            target_tasks = [item.task_id for item in items if item.task_id]
        if not target_tasks:
            target_tasks = [""]

        # merge each answer into the per-task drafts; a task-less answer applies
        # its health/time/blocker to every target task ("都正常")
        touched: list[str] = []
        for item in items:
            touched_tasks = [item.task_id] if item.task_id else list(target_tasks)
            for task_id in touched_tasks:
                if task_id not in touched:
                    touched.append(task_id)
                base = drafts.get(task_id, PendingClaimDraft(task_id=task_id))
                drafts[task_id] = PendingClaimDraft(
                    task_id=task_id,
                    health_status=(
                        item.health_status or base.health_status or HealthStatus.AT_RISK.value
                    ),
                    expected_completion=item.expected_completion or base.expected_completion,
                    blocker=item.blocker or base.blocker,
                )

        # persist one health_reported event per touched task, validated in batch
        events = []
        for task_id in touched:
            counter[0] += 1
            draft = drafts[task_id]
            events.append(
                build_health_reported_event(
                    project_id=project_id,
                    actor_id=member.member_id,
                    occurred_at=occurred_at,
                    source_ref=source_ref,
                    event_id=f"evt-intent-claim-{message_key}-{counter[0]}",
                    task_id=task_id,
                    health_status=draft.health_status or HealthStatus.AT_RISK.value,
                    expected_completion=draft.expected_completion,
                    blocker=draft.blocker,
                    confidence=items[0].confidence,
                )
            )
        agent, reason, round_num, error = await self.service.commit_extracted_events(
            project_id,
            events,
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
        if probe_source:
            data["probe_reply"] = True

        # per-task completeness over the full target scope. Router-asked
        # clarifications (R6/R7) only ever asked for the missing TIME, so for
        # clarify-source entries the probe-specific blocker slot is not required
        # (ADR-0008 implementation log).
        scope = target_tasks or touched
        missing: dict[str, list[str]] = {}
        for task_id in scope:
            slots = self._task_missing_slots(drafts.get(task_id))
            if slots and not probe_source:
                slots = [slot for slot in slots if "时间" in slot]
            if slots:
                missing[task_id] = slots

        summary = self._answers_summary(agent.projector.state, drafts, scope, occurred_at)

        if not missing:
            self.dialogue.clear_pending_clarification(project_id, member.member_id)
            session = None
            if probe_source:
                session = await self._notify_probe(
                    project_id, member.member_id, complete=True, occurred_at=occurred_at
                )
                if session is not None:
                    data["session_id"] = session.session_id
            parts.append(f"{summary}，信息完整，谢谢！")
            kinds.append(ReplyKind.CLAIM_RECORDED)
            return asks

        asked = entry.asked_rounds if entry else 0
        exhausted = asked >= self.dialogue.max_clarification_rounds
        if probe_source and exhausted:
            # 多轮追问仍不完整：如实标注"信息不全"，交给协调器在简报中体现
            session = await self._notify_probe(
                project_id,
                member.member_id,
                complete=False,
                occurred_at=occurred_at,
                note="信息不全",
            )
            if session is not None:
                data["session_id"] = session.session_id
            self.dialogue.clear_pending_clarification(project_id, member.member_id)
            parts.append(f"{summary}（多轮追问后仍有缺失，简报中将标注「信息不全」。）")
            kinds.append(ReplyKind.CLAIM_RECORDED)
            return asks
        if not probe_source and exhausted:
            self.dialogue.clear_pending_clarification(project_id, member.member_id)
            parts.append(f"{summary}（已达追问上限，先按当前信息记录。）")
            kinds.append(ReplyKind.CLAIM_RECORDED)
            return asks

        question = self._missing_question(agent.projector.state, missing)
        if entry is None:
            self.dialogue.register_pending_clarification(
                project_id,
                member.member_id,
                question=question,
                task_ids=[task_id for task_id in scope if task_id],
                source=CLARIFICATION_SOURCE_CLARIFY,
                claims={task_id: drafts[task_id] for task_id in drafts},
                missing=missing,
                asked_rounds=asked + 1,
                created_at=occurred_at,
            )
        else:
            entry.claims = {task_id: drafts[task_id] for task_id in drafts}
            entry.missing = missing
            entry.task_ids = [task_id for task_id in scope if task_id]
            entry.asked_rounds = asked + 1
        parts.append(f"{summary}。{question}")
        kinds.append(ReplyKind.CLAIM_RECORDED)
        return True

    def _task_missing_slots(self, draft: PendingClaimDraft | None) -> list[str]:
        """Per-task completeness slots (3a.1): on_track none, at_risk time,
        delayed time + blocker — the same rules as SlotCompletenessEvaluator."""
        if draft is None or draft.health_status is None:
            return ["当前具体进展与预计完成时间"]
        view = ExtractionResult(
            is_actionable=True,
            claims=[
                ExtractedHealthClaim(
                    task_id=draft.task_id or "",
                    health_status=HealthStatus(draft.health_status),
                    expected_completion=draft.expected_completion,
                    blocker=draft.blocker,
                    confidence=1.0,
                    source_quote=" ",
                )
            ],
        )
        _, missing = SlotCompletenessEvaluator.evaluate_completeness(view, " ")
        return missing

    def _answers_summary(
        self,
        state: OrgState,
        drafts: dict[str, PendingClaimDraft],
        scope: list[str],
        occurred_at: datetime,
    ) -> str:
        fragments: list[str] = []
        for task_id in scope:
            if task_id not in drafts:
                continue
            draft = drafts[task_id]
            item_view = IntentItem(
                intent=IntentType.REPORT_STATUS,
                task_id=task_id,
                speech_act=SpeechAct.STATEMENT,
                polarity=Polarity.AFFIRMATIVE,
                certainty=Certainty.CERTAIN,
                health_status=draft.health_status,
                expected_completion=draft.expected_completion,
                blocker=draft.blocker,
                source_quote=" ",
                confidence=1.0,
            )
            fragments.append(self._summarize_item(item_view, state, occurred_at))
        return "；".join(fragment for fragment in fragments if fragment)

    def _missing_question(self, state: OrgState, missing: dict[str, list[str]]) -> str:
        """Asks ONLY about the tasks and slots that still lack information (3a.1)."""
        lines: list[str] = []
        for task_id, slots in missing.items():
            title = state.tasks[task_id].title if task_id in state.tasks else task_id
            lines.append(f"【{title}】还差：{'、'.join(slots)}")
        return "还需要补充：" + "；".join(lines)

    async def _notify_probe(
        self,
        project_id: str,
        member_id: str,
        *,
        complete: bool,
        occurred_at: datetime,
        note: str | None = None,
    ) -> Any:
        coordinator = await self.service.get_sync_coordinator(project_id)
        return coordinator.notify_probe_finished(
            project_id,
            member_id,
            complete=complete,
            occurred_at=occurred_at,
            note=note,
        )

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
            task_ids=[item.task_id] if item.task_id else [],
            source=CLARIFICATION_SOURCE_CLARIFY,
            claims={
                task_id: PendingClaimDraft(
                    task_id=task_id, health_status=item.health_status
                )
                for task_id in [item.task_id]
                if task_id
            },
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
            coordinator = await self.service.get_sync_coordinator(project_id)
            active = coordinator.get_active_session(project_id, now=occurred_at)
            self.dialogue.clear_pending_sync_confirmation(project_id, member.member_id)
            if active is not None:
                # A sync is already running: confirm nothing, report progress (3a.1).
                parts.append(
                    f"已有进行中的全员同步（{active.progress_summary()}），本次确认未重复发起。"
                )
                kinds.append(ReplyKind.NOTED)
                return False
            session = await self.service.start_progress_sync(
                project_id, initiated_by=confirmation.initiated_by
            )
            if self._session_registrar is not None:
                self._session_registrar(project_id, session, occurred_at)
            parts.append(
                f"已发起全员进度同步：已向 {len(session.member_probes)} 位负责人发送进度探针。"
            )
            kinds.append(ReplyKind.SYNC_STARTED)
            return True
        self.dialogue.clear_pending_sync_confirmation(project_id, member.member_id)
        parts.append("好的，已取消本次同步确认。")
        kinds.append(ReplyKind.NOTED)
        return False

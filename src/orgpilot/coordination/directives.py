"""Directive lifecycle manager: issue, relay, acknowledge, complete, remind, escalate.

Directives are the execution side of intent routing: a PM order like「告诉Alice，
必须在明天下午5点之前完成」becomes an event-sourced ``directive.issued`` record,
relayed to the target member through the collaboration adapter. The target's chat
replies are intercepted to drive ``directive.acknowledged`` / ``directive.completed``
transitions, and unanswered directives escalate back to the issuer.

The manager is stateless: it reads the projected ``OrgState`` handed to it and
returns events to append plus outbound notices. Persistence and replay come free
from the event log.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.domain.enums import ActionType, DirectiveStatus
from orgpilot.domain.errors import DomainInvariantError
from orgpilot.domain.models import (
    ActionCommand,
    DirectiveState,
    OrgState,
    PendingDirectiveClarification,
)
from orgpilot.events.models import (
    DirectiveAcknowledgedEvent,
    DirectiveClarificationRequestedEvent,
    DirectiveClarificationResolvedEvent,
    DirectiveCompletedEvent,
    DirectiveEscalatedEvent,
    DirectiveIssuedEvent,
    DirectiveRemindedEvent,
    EventSource,
    OrgEvent,
)
from orgpilot.extraction.intent import _TIME_EXPR_RE, PRIVILEGED_ROLES
from orgpilot.extraction.models import IntentHint
from orgpilot.extraction.verifier import TemporalResolver

_ACK_RE = re.compile(r"收到|好的?|明白|了解|没问题|马上[去处]|OK|okay", re.IGNORECASE)
_COMPLETE_RE = re.compile(r"完成|搞定|已交付|\bdone\b", re.IGNORECASE)
_COMPLETE_VETO_RE = re.compile(r"没|不|延期|卡|问题|阻塞|受阻")
# 「上午12点」类表达存在正午/午夜歧义，下达前必须追问。
_AMBIGUOUS_TIME_RE = re.compile(r"(上午|早上)\s*12\s*[点时]")
# 「算了/取消」放弃一个待补全的指令澄清。
_CLARIFY_CANCEL_RE = re.compile(r"算了|取消|不下了|撤销|收回")

_MAX_CLARIFY_TARGETS = 4


@dataclass
class DirectiveNotice:
    """A bot message to be rendered for a specific member (cross-pane in sandbox)."""

    actor_id: str
    text: str


@dataclass
class DirectiveOutcome:
    """Result of a directive chain step for gateway surfacing."""

    # issued | acknowledged | completed | clarify | declined | reminded | escalated | none
    kind: str
    bot_reply: str | None = None
    notices: list[DirectiveNotice] = field(default_factory=list)
    events: list[OrgEvent] = field(default_factory=list)
    directive: DirectiveState | None = None
    # Adapter commands to execute only after the events have been persisted.
    outbound: list[ActionCommand] = field(default_factory=list)


class DirectiveManager:
    """Drives the directive lifecycle on top of projected state."""

    def __init__(
        self,
        adapter: CollaborationAdapter,
        reminder_after_minutes: int = 60,
        escalation_after_minutes: int = 1440,
        reference_timezone: str = "Asia/Shanghai",
    ) -> None:
        self.adapter = adapter
        self.reminder_after_minutes = reminder_after_minutes
        self.escalation_after_minutes = escalation_after_minutes
        self.reference_timezone = reference_timezone

    # ------------------------------------------------------------------ issue

    def handle_directive_intent(
        self,
        message: str,
        actor_id: str,
        hints: IntentHint | None,
        state: OrgState,
        occurred_at: datetime,
    ) -> DirectiveOutcome:
        """Turns a routed directive intent into a relayed directive, or a
        clarification/decline when slots or authority are missing."""
        issuer = state.members.get(actor_id)
        if issuer is None:
            return DirectiveOutcome(kind="declined", bot_reply="未识别的发送者，无法下达指令。")
        if issuer.role not in PRIVILEGED_ROLES:
            return DirectiveOutcome(
                kind="declined",
                bot_reply=(
                    f"已识别为执行指令，但您当前角色（{issuer.role}）无下达权限。"
                    "请联系项目负责人 (PM) 下达，或直接与我沟通任务安排。"
                ),
            )

        hints = hints or IntentHint()
        targets = [mid for mid in hints.mentioned_member_ids if mid != actor_id]
        unknown = [mid for mid in targets if mid not in state.members]
        if not targets or unknown:
            if unknown:
                question = (
                    f"目标成员 {unknown[0]} 不在项目成员目录中，请回复正确的成员名称，"
                    "我会立即下达。"
                )
            else:
                question = "请问这条指令要下达给谁？请指明目标成员（例如：告诉Alice…）。"
            return self._clarify_directive(
                state,
                actor_id,
                message,
                occurred_at,
                targets=tuple(t for t in targets if t in state.members),
                missing_slots=("target",),
                time_expr=hints.raw_time_expr,
                question=question,
            )

        task_id = self._resolve_task(hints, targets[0], state)
        if isinstance(task_id, DirectiveOutcome):
            return self._clarify_directive(
                state,
                actor_id,
                message,
                occurred_at,
                targets=tuple(targets),
                missing_slots=("task",),
                time_expr=hints.raw_time_expr,
                question=task_id.bot_reply or "请指明这条指令针对哪一项任务。",
            )

        if hints.raw_time_expr and _AMBIGUOUS_TIME_RE.search(hints.raw_time_expr):
            return self._clarify_directive(
                state,
                actor_id,
                message,
                occurred_at,
                targets=tuple(targets),
                task_id=task_id,
                missing_slots=("deadline",),
                time_expr=hints.raw_time_expr,
                question=(
                    f"时间「{hints.raw_time_expr}」有歧义：您指的是中午 12:00 还是凌晨 00:00？"
                    "确认后我立即下达指令。"
                ),
            )

        deadline = self._resolve_deadline(hints.raw_time_expr, occurred_at)
        return self._issue_directive_outcome(
            state, actor_id, message, tuple(targets), task_id, deadline, occurred_at
        )

    # ----------------------------------------------------------------- reply

    def handle_member_reply(
        self,
        actor_id: str,
        message: str,
        state: OrgState,
        occurred_at: datetime,
    ) -> DirectiveOutcome | None:
        """Intercepts a target member's reply to an open directive (ack / complete)."""
        open_dirs = self._open_directives_for(state, actor_id)
        if not open_dirs:
            return None
        if len(open_dirs) > 1:
            directive = self._bind_directive_by_reply(open_dirs, message, state)
            if directive is None:
                return self._disambiguation_question(open_dirs, state, actor_id)
        else:
            directive = open_dirs[0]

        lower = message.lower().strip()
        issuer_name = self._display_name(state, directive.issuer_id)
        target_name = self._display_name(state, actor_id)

        if _COMPLETE_RE.search(lower) and not _COMPLETE_VETO_RE.search(lower):
            event = DirectiveCompletedEvent(
                project_id=state.project_id,
                event_id=f"evt-directive-done-{directive.directive_id}",
                event_type="directive.completed",
                source=EventSource.MESSAGE,
                source_ref=f"message:{directive.directive_id}",
                actor_id=actor_id,
                occurred_at=occurred_at,
                received_at=occurred_at,
                payload={
                    "directive_id": directive.directive_id,
                    "completed_by": actor_id,
                    "note": message.strip(),
                },
            )
            ack_event = None
            if directive.status is DirectiveStatus.ISSUED:
                ack_event = self._ack_event(
                    state.project_id, directive, actor_id, message, occurred_at
                )
            events = [e for e in (ack_event, event) if e is not None]
            return DirectiveOutcome(
                kind="completed",
                bot_reply=(f"🏁 已记录：您已完成指令任务。感谢反馈，已同步给 {issuer_name}。"),
                notices=[
                    DirectiveNotice(
                        actor_id=directive.issuer_id,
                        text=f"🏁 {target_name} 已完成指令：「{directive.text[:50]}」",
                    )
                ],
                events=events,
                directive=directive,
            )

        if _ACK_RE.search(lower) and len(lower) <= 40:
            if directive.status is DirectiveStatus.ISSUED:
                event = self._ack_event(state.project_id, directive, actor_id, message, occurred_at)
                return DirectiveOutcome(
                    kind="acknowledged",
                    bot_reply=(
                        f"✅ 已确认收到，谢谢！完成后请回复「已完成」，我会同步给 {issuer_name}。"
                    ),
                    notices=[
                        DirectiveNotice(
                            actor_id=directive.issuer_id,
                            text=f"✅ {target_name} 已确认指令：「{directive.text[:50]}」",
                        )
                    ],
                    events=[event],
                    directive=directive,
                )
            # Already acknowledged: gentle nudge without duplicate events.
            return DirectiveOutcome(
                kind="none",
                bot_reply=f"已确认过该指令。完成后请回复「已完成」，我会同步给 {issuer_name}。",
            )

        # Non-matching replies flow through the normal extraction pipeline untouched.
        return None

    # ----------------------------------------------------- clarification loop

    def resolve_pending_clarification(
        self,
        actor_id: str,
        message: str,
        state: OrgState,
        occurred_at: datetime,
    ) -> DirectiveOutcome | None:
        """Merges an issuer's reply into their pending directive clarification.

        Returns None when the member has no pending clarification so normal
        intent handling proceeds untouched. When all slots are filled the
        original draft finally issues with its full restored context.
        """
        pending = self._pending_clarification_for(state, actor_id)
        if pending is None:
            return None
        lower = message.lower().strip()

        if _CLARIFY_CANCEL_RE.search(lower):
            resolved = self._clarification_resolved_event(
                state, actor_id, pending.clarification_id, directive_id="", occurred_at=occurred_at
            )
            return DirectiveOutcome(
                kind="cancelled",
                bot_reply="好的，已取消这条待补充的指令。",
                events=[resolved],
            )

        targets = pending.targets
        if not targets:
            detected = self._detect_reply_members(lower, state, actor_id)
            if detected:
                targets = tuple(detected)

        task_id = pending.task_id
        if task_id is None and "task" in pending.missing_slots:
            task_id = self._match_reply_task(message, state)

        time_expr = pending.time_expr
        if "deadline" in pending.missing_slots:
            # Prefer a fresh, unambiguous expression from the reply itself.
            reply_time = self._extract_time_expr(message)
            if reply_time and not _AMBIGUOUS_TIME_RE.search(reply_time):
                time_expr = reply_time
            elif time_expr and _AMBIGUOUS_TIME_RE.search(time_expr):
                time_expr = self._disambiguate_time(time_expr, lower)

        # Re-ask for whatever is still unresolved — the pending draft persists.
        if not targets:
            return DirectiveOutcome(
                kind="clarify",
                bot_reply="还没认出目标成员，请直接回复成员名称（例如：Alice）。",
            )
        if "task" in pending.missing_slots and task_id is None:
            return DirectiveOutcome(
                kind="clarify",
                bot_reply="请指明这条指令针对哪一项任务（可直接回复任务名称）。",
            )
        if time_expr and _AMBIGUOUS_TIME_RE.search(time_expr):
            return DirectiveOutcome(
                kind="clarify",
                bot_reply=(f"时间「{time_expr}」仍有歧义：您指的是中午 12:00 还是凌晨 00:00？"),
            )

        deadline = self._resolve_deadline(time_expr, occurred_at)
        return self._issue_directive_outcome(
            state,
            actor_id,
            pending.draft_text,
            tuple(targets),
            task_id,
            deadline,
            occurred_at,
            clarification_id=pending.clarification_id,
        )

    # --------------------------------------------------------- remind / sweep

    def remind_open_directives(
        self,
        state: OrgState,
        operator_id: str,
        occurred_at: datetime,
    ) -> DirectiveOutcome:
        """Manually nudges every still-unacknowledged directive."""
        operator = state.members.get(operator_id)
        if operator is None or operator.role not in PRIVILEGED_ROLES:
            raise DomainInvariantError(
                f"member {operator_id!r} is not authorized to remind directives"
            )
        notices: list[DirectiveNotice] = []
        outbound: list[ActionCommand] = []
        events: list[OrgEvent] = []
        reminded = 0
        for directive in state.directives.values():
            if directive.status is not DirectiveStatus.ISSUED:
                continue
            index = directive.reminder_count + 1
            events.append(
                DirectiveRemindedEvent(
                    project_id=state.project_id,
                    event_id=f"evt-directive-remind-{directive.directive_id}-{index}",
                    event_type="directive.reminded",
                    source=EventSource.MESSAGE,
                    source_ref=f"message:{directive.directive_id}",
                    actor_id=operator_id,
                    occurred_at=occurred_at,
                    received_at=occurred_at,
                    payload={
                        "directive_id": directive.directive_id,
                        "reminded_by": operator_id,
                        "reminder_index": index,
                    },
                )
            )
            text = (
                f"⏰ 提醒：请确认来自 {self._display_name(state, directive.issuer_id)} 的指令："
                f"「{directive.text[:60]}」回复「收到」即可。"
            )
            outbound.append(
                self._relay_command(
                    state.project_id,
                    directive.directive_id,
                    directive.target_id,
                    text,
                    occurred_at,
                    idem_suffix=f":r{index}",
                )
            )
            notices.append(DirectiveNotice(actor_id=directive.target_id, text=text))
            reminded += 1

        if reminded == 0:
            return DirectiveOutcome(kind="none", bot_reply="当前没有待确认的指令。")
        return DirectiveOutcome(
            kind="reminded",
            bot_reply=f"📣 已向 {reminded} 位成员催办未确认指令。",
            notices=notices,
            events=events,
            outbound=outbound,
        )

    def sweep_timeouts(self, state: OrgState, now: datetime) -> DirectiveOutcome:
        """Auto-reminds and auto-escalates directives by age. Called opportunistically."""
        notices: list[DirectiveNotice] = []
        outbound: list[ActionCommand] = []
        events: list[OrgEvent] = []
        for directive in state.directives.values():
            if directive.status is not DirectiveStatus.ISSUED:
                continue
            age_minutes = (now - directive.issued_at).total_seconds() / 60
            if not directive.escalated and age_minutes >= self.escalation_after_minutes:
                events.append(
                    DirectiveEscalatedEvent(
                        project_id=state.project_id,
                        event_id=f"evt-directive-escal-{directive.directive_id}",
                        event_type="directive.escalated",
                        source=EventSource.MESSAGE,
                        source_ref=f"message:{directive.directive_id}",
                        actor_id=directive.issuer_id,
                        occurred_at=now,
                        received_at=now,
                        payload={
                            "directive_id": directive.directive_id,
                            "reason": (
                                f"超过 {self.escalation_after_minutes} 分钟未获 "
                                f"{self._display_name(state, directive.target_id)} 确认"
                            ),
                        },
                    )
                )
                text = (
                    f"🚨 指令升级：{self._display_name(state, directive.target_id)} 长时间未确认"
                    f"「{directive.text[:50]}」，请直接跟进。"
                )
                outbound.append(
                    self._relay_command(
                        state.project_id,
                        directive.directive_id,
                        directive.issuer_id,
                        text,
                        now,
                        idem_suffix=":esc",
                    )
                )
                notices.append(DirectiveNotice(actor_id=directive.issuer_id, text=text))
            elif directive.reminder_count == 0 and age_minutes >= self.reminder_after_minutes:
                events.append(
                    DirectiveRemindedEvent(
                        project_id=state.project_id,
                        event_id=f"evt-directive-remind-{directive.directive_id}-1",
                        event_type="directive.reminded",
                        source=EventSource.MESSAGE,
                        source_ref=f"message:{directive.directive_id}",
                        actor_id=directive.issuer_id,
                        occurred_at=now,
                        received_at=now,
                        payload={
                            "directive_id": directive.directive_id,
                            "reminded_by": "system",
                            "reminder_index": 1,
                        },
                    )
                )
                text = (
                    f"⏰ 自动提醒：请确认来自 {self._display_name(state, directive.issuer_id)} "
                    f"的指令：「{directive.text[:60]}」"
                )
                outbound.append(
                    self._relay_command(
                        state.project_id,
                        directive.directive_id,
                        directive.target_id,
                        text,
                        now,
                        idem_suffix=":r1",
                    )
                )
                notices.append(DirectiveNotice(actor_id=directive.target_id, text=text))
        if not events:
            return DirectiveOutcome(kind="none")
        return DirectiveOutcome(kind="swept", notices=notices, events=events, outbound=outbound)

    # --------------------------------------------------------------- internals

    def _resolve_task(
        self, hints: IntentHint, target_id: str, state: OrgState
    ) -> str | DirectiveOutcome:
        if hints.mentioned_task_ids:
            return hints.mentioned_task_ids[0]
        owned = [t.task_id for t in state.tasks.values() if t.owner_id == target_id]
        if len(owned) == 1:
            return owned[0]
        if len(owned) > 1:
            titles = "、".join(
                f"【{state.tasks[tid].title}】" for tid in owned[:_MAX_CLARIFY_TARGETS]
            )
            return DirectiveOutcome(
                kind="clarify",
                bot_reply=f"该成员负责多项任务，请指明这条指令针对哪一个：{titles}",
            )
        return None

    def _open_directives_for(self, state: OrgState, actor_id: str) -> list[DirectiveState]:
        open_dirs = [
            d
            for d in state.directives.values()
            if d.target_id == actor_id
            and d.status
            in (
                DirectiveStatus.ISSUED,
                DirectiveStatus.ACKNOWLEDGED,
            )
        ]
        open_dirs.sort(key=lambda d: d.issued_at)
        return open_dirs

    def _bind_directive_by_reply(
        self, open_dirs: list[DirectiveState], message: str, state: OrgState
    ) -> DirectiveState | None:
        """Binds a reply to exactly one directive by task-title or text overlap;
        None when the binding is ambiguous and a disambiguation question is needed."""
        compact = message.lower().replace(" ", "")
        stripped = re.sub(rf"{_ACK_RE.pattern}|[，。!！?？、]", "", compact)
        bound = []
        for d in open_dirs:
            task_title = (
                state.tasks[d.task_id].title.lower().replace(" ", "")
                if d.task_id and d.task_id in state.tasks
                else ""
            )
            text_key = d.text.lower().replace(" ", "")[:10]
            title_hit = task_title and (
                task_title in compact or (stripped and stripped in task_title)
            )
            text_hit = len(text_key) >= 4 and text_key in compact
            if title_hit or text_hit:
                bound.append(d)
        return bound[0] if len(bound) == 1 else None

    def _disambiguation_question(
        self, open_dirs: list[DirectiveState], state: OrgState, actor_id: str
    ) -> DirectiveOutcome:
        listing = "；".join(
            f"{index}.「{d.text[:30]}」"
            + (
                f"（{state.tasks[d.task_id].title}）"
                if d.task_id and d.task_id in state.tasks
                else ""
            )
            for index, d in enumerate(open_dirs, start=1)
        )
        return DirectiveOutcome(
            kind="none",
            bot_reply=(
                f"{self._display_name(state, actor_id)}，您有多条进行中的指令：{listing}。"
                "请回复任务名称或指令内容，我会对应确认。"
            ),
        )

    def _clarify_directive(
        self,
        state: OrgState,
        actor_id: str,
        message: str,
        occurred_at: datetime,
        *,
        targets: tuple[str, ...],
        missing_slots: tuple[str, ...],
        question: str,
        task_id: str | None = None,
        time_expr: str | None = None,
    ) -> DirectiveOutcome:
        """Persists a pending clarification so the issuer's answer restores the
        original draft (target/task/deadline) instead of being lost mid-ask."""
        clarification_id = self._clarification_id(state.project_id, actor_id, message, occurred_at)
        if clarification_id in state.pending_directive_clarifications:
            # Identical retry (e.g. a webhook replay): re-ask without a new event.
            return DirectiveOutcome(kind="clarify", bot_reply=question)
        event = DirectiveClarificationRequestedEvent(
            project_id=state.project_id,
            event_id=f"evt-directive-clarify-{clarification_id}",
            event_type="directive.clarification_requested",
            source=EventSource.MESSAGE,
            source_ref=f"message:{clarification_id}",
            actor_id=actor_id,
            occurred_at=occurred_at,
            received_at=occurred_at,
            payload={
                "clarification_id": clarification_id,
                "issuer_id": actor_id,
                "draft_text": message.strip(),
                "missing_slots": missing_slots,
                "targets": targets,
                "task_id": task_id,
                "time_expr": time_expr,
            },
        )
        return DirectiveOutcome(kind="clarify", bot_reply=question, events=[event])

    def _issue_directive_outcome(
        self,
        state: OrgState,
        actor_id: str,
        directive_message: str,
        targets: tuple[str, ...],
        task_id: str | None,
        deadline: datetime | None,
        occurred_at: datetime,
        clarification_id: str | None = None,
    ) -> DirectiveOutcome:
        directive_id = self._directive_id(
            state.project_id, actor_id, directive_message, occurred_at
        )
        target_name = self._display_name(state, targets[0])
        task_title = (
            f"【{state.tasks[task_id].title}】" if task_id and task_id in state.tasks else ""
        )
        deadline_str = self._format_deadline(deadline)
        directive_text = directive_message.strip()

        event = DirectiveIssuedEvent(
            project_id=state.project_id,
            event_id=f"evt-directive-issue-{directive_id}",
            event_type="directive.issued",
            source=EventSource.MESSAGE,
            source_ref=f"message:{directive_id}",
            actor_id=actor_id,
            occurred_at=occurred_at,
            received_at=occurred_at,
            payload={
                "directive_id": directive_id,
                "text": directive_text,
                "issuer_id": actor_id,
                "target_id": targets[0],
                "task_id": task_id,
                "deadline": deadline,
            },
        )
        events: list[OrgEvent] = [event]
        if clarification_id:
            events.append(
                self._clarification_resolved_event(
                    state,
                    actor_id,
                    clarification_id,
                    directive_id=directive_id,
                    occurred_at=occurred_at,
                )
            )

        relay_text = (
            f"📩 **指令**（来自 {self._display_name(state, actor_id)}）：{directive_text}\n"
            f"关联任务：{task_title or '未指定'}　截止：{deadline_str or '未指定'}\n"
            "请回复「收到」确认，完成后回复「已完成」。"
        )
        outbound = self._relay_command(
            state.project_id, directive_id, targets[0], relay_text, occurred_at
        )

        reply = (
            f"📩 指令已下达给 {target_name}：{directive_text}\n"
            f"（任务：{task_title or '未指定'}　截止：{deadline_str or '未指定'}）\n"
            f"等待 {target_name} 回复确认；未确认可点击「📣 催办未确认指令」。"
        )
        return DirectiveOutcome(
            kind="issued",
            bot_reply=reply,
            notices=[DirectiveNotice(actor_id=targets[0], text=relay_text)],
            events=events,
            outbound=[outbound],
            directive=self._preview_directive(event),
        )

    def _resolve_deadline(self, time_expr: str | None, occurred_at: datetime) -> datetime | None:
        if not time_expr:
            return None
        # Resolve relative times against the TEAM reference timezone, not the
        # storage timezone of the event timestamp (UTC), or "明天下午5点" drifts.
        try:
            anchor = occurred_at.astimezone(ZoneInfo(self.reference_timezone))
        except Exception:
            anchor = occurred_at
        return TemporalResolver.resolve_relative_time(time_expr, anchor)

    def _clarification_resolved_event(
        self,
        state: OrgState,
        actor_id: str,
        clarification_id: str,
        *,
        directive_id: str,
        occurred_at: datetime,
    ) -> DirectiveClarificationResolvedEvent:
        return DirectiveClarificationResolvedEvent(
            project_id=state.project_id,
            event_id=f"evt-directive-clarify-resolved-{clarification_id}",
            event_type="directive.clarification_resolved",
            source=EventSource.MESSAGE,
            source_ref=f"message:{clarification_id}",
            actor_id=actor_id,
            occurred_at=occurred_at,
            received_at=occurred_at,
            payload={"clarification_id": clarification_id, "directive_id": directive_id},
        )

    @staticmethod
    def _pending_clarification_for(
        state: OrgState, actor_id: str
    ) -> PendingDirectiveClarification | None:
        pending = [
            c for c in state.pending_directive_clarifications.values() if c.issuer_id == actor_id
        ]
        if not pending:
            return None
        pending.sort(key=lambda c: c.last_update_at)
        return pending[-1]

    def _detect_reply_members(self, lower: str, state: OrgState, actor_id: str) -> list[str]:
        """Finds member ids mentioned in a clarification answer (id, directory
        name, or open_id suffix token), excluding the speaker."""
        found: list[str] = []
        for member_id, member in state.members.items():
            if member_id == actor_id:
                continue
            candidates = {member_id.lower(), member.display_name.lower()}
            suffix = member_id.lower().split("_")[-1]
            if len(suffix) >= 3 and suffix.isascii() and suffix.isalpha():
                candidates.add(suffix)
            if any(token and token in lower for token in candidates):
                found.append(member_id)
        return found

    @staticmethod
    def _match_reply_task(message: str, state: OrgState) -> str | None:
        compact = message.lower().replace(" ", "")
        for task_id, task in state.tasks.items():
            if task_id.lower() in compact or task.title.lower().replace(" ", "") in compact:
                return task_id
        return None

    @staticmethod
    def _extract_time_expr(message: str) -> str | None:
        match = _TIME_EXPR_RE.search(message)
        return match.group(0).strip() if match else None

    @staticmethod
    def _disambiguate_time(time_expr: str, lower_reply: str) -> str:
        """Merges a noon/midnight answer back into the ambiguous draft expression."""
        day = re.search(
            r"(今天|明天|后天|大后天|周[一二三四五六日天]|下周[一二三四五六日天]?|\d+\s*天[之]?后)",
            time_expr,
        )
        prefix = day.group(1) if day else ""
        if re.search(r"中午|正午", lower_reply):
            return f"{prefix}中午12点"
        if re.search(r"凌晨|夜里|晚上|半夜", lower_reply):
            return f"{prefix}凌晨0点"
        return time_expr

    @staticmethod
    def _clarification_id(
        project_id: str, actor_id: str, message: str, occurred_at: datetime
    ) -> str:
        identity = f"{project_id}|{actor_id}|{occurred_at.isoformat()}|{message}"
        return f"clari-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"

    def _ack_event(
        self,
        project_id: str,
        directive: DirectiveState,
        actor_id: str,
        message: str,
        occurred_at: datetime,
    ) -> DirectiveAcknowledgedEvent:
        return DirectiveAcknowledgedEvent(
            project_id=project_id,
            event_id=f"evt-directive-ack-{directive.directive_id}",
            event_type="directive.acknowledged",
            source=EventSource.MESSAGE,
            source_ref=f"message:{directive.directive_id}",
            actor_id=actor_id,
            occurred_at=occurred_at,
            received_at=occurred_at,
            payload={
                "directive_id": directive.directive_id,
                "ack_by": actor_id,
                "response_text": message.strip()[:200],
            },
        )

    def _relay_command(
        self,
        project_id: str,
        directive_id: str,
        target_id: str,
        text: str,
        occurred_at: datetime,
        idem_suffix: str = "",
    ) -> ActionCommand:
        return ActionCommand(
            command_id=f"cmd:directive:{directive_id}:{target_id}:{int(occurred_at.timestamp())}",
            action_id=f"action:directive:{directive_id}",
            action_type=ActionType.SEND_DIRECTIVE,
            targets=[target_id],
            payload={"text": text, "directive_id": directive_id},
            created_at=occurred_at,
            # Reminders/escalations must not collide with the original relay key,
            # or the outbox would dedupe the nudge away.
            idempotency_key=f"idem:directive:{directive_id}:{target_id}{idem_suffix}",
        )

    def _preview_directive(self, event: DirectiveIssuedEvent) -> DirectiveState:
        payload = event.payload
        return DirectiveState(
            directive_id=payload.directive_id,
            text=payload.text,
            issuer_id=payload.issuer_id,
            target_id=payload.target_id,
            task_id=payload.task_id,
            deadline=payload.deadline,
            issued_at=event.occurred_at,
            last_update_at=event.occurred_at,
        )

    @staticmethod
    def _directive_id(project_id: str, actor_id: str, message: str, occurred_at: datetime) -> str:
        identity = f"{project_id}|{actor_id}|{occurred_at.isoformat()}|{message}"
        return f"dir-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _display_name(state: OrgState, member_id: str) -> str:
        member = state.members.get(member_id)
        return member.display_name if member else member_id

    @staticmethod
    def _format_deadline(deadline: datetime | None) -> str | None:
        if deadline is None:
            return None
        try:
            local = deadline.astimezone(ZoneInfo("Asia/Shanghai"))
        except Exception:
            return deadline.isoformat()
        return local.strftime("%m-%d %H:%M")

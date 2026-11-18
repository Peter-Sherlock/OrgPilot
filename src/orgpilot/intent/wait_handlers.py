"""Message handling for dependency waits (ADR-0016).

The frozen intent prompt files "页面在等支付接口" under report_status, so this layer
recognizes the statement deterministically when it names a formal upstream task.
Replies from the upstream owner are matched through the case ledger, never through
dialogue state, so a pending probe or clarification stays untouched.
"""

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from orgpilot.coordination.dependency_wait import (
    UpstreamOutcome,
    active_wait_for_pair,
    active_waits,
    awaited_waits,
    cancels_wait,
    format_moment,
    is_bare_cancel,
    mention_score,
    names_task_exactly,
    resolve_upstream,
    wait_case_id,
    wait_statement_tasks,
)
from orgpilot.domain.enums import (
    ActionType,
    CoordinationCaseStatus,
    DependencyWaitStatus,
    HealthStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import DependencyWait, MemberState, OrgState, TaskState
from orgpilot.events.models import (
    DependencyDeliveryEstimatedEvent,
    DependencyDeliveryEstimatedPayload,
    DependencyWaitCancelledEvent,
    DependencyWaitCancelledPayload,
    DependencyWaitReportedEvent,
    DependencyWaitReportedPayload,
    EventSource,
)
from orgpilot.extraction.verifier import TemporalResolver
from orgpilot.intent.dialogue import DialogueStateStore
from orgpilot.intent.guards import check_grounding, check_time_expression
from orgpilot.intent.models import (
    Certainty,
    IntentItem,
    IntentResult,
    IntentType,
    Polarity,
    SpeechAct,
)
from orgpilot.intent.replies import ReplyKind

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService

logger = logging.getLogger(__name__)

_REPLY_INTENTS = {
    IntentType.REPORT_STATUS,
    IntentType.ANSWER_PENDING,
    IntentType.MAKE_COMMITMENT,
}
_ASK_FOR_ESTIMATE = "方便告诉我预计什么时候可以交付或联调吗？例如：明天下午3点。"
_ACK_WITHOUT_ESTIMATE = "收到，确定交付时间后告诉我就行。"
_MAX_ESTIMATE_FOLLOW_UPS = 1

_CLAUSE_SPLIT = re.compile(r"[，。,.!！；;\n]")
_DELIVERY_CUE = re.compile(
    r"联调|交付|提测|上线|发布|合并|完成|做完|搞定|修好|修完|恢复|解决|好了|能好|可以给|能给|给你|ready",
    re.IGNORECASE,
)
_OTHER_EVENT_CUE = re.compile(r"开始|启动|评审|演示|开会|会议|讨论|定位|同步|请假|出差")
_TIME_WORDS = re.compile(
    r"明天|后天|今天|今晚|下?周[一二三四五六日天]|(?:星期|礼拜)[一二三四五六日天]"
    r"|上午|下午|晚上|早上|中午|\d+[点时](?:\d+分?|半)?|\d+:\d+|\d+[天日]后"
    r"|左右|之前|前|吧|大概|应该|估计|差不多|可能"
)


def _delivery_time(item: IntentItem, message: str, anchor: datetime) -> datetime | None:
    """The delivery time this reply states; dates of meetings or kick-offs do not count."""
    stated: datetime | None = None
    for clause in _CLAUSE_SPLIT.split(message):
        if _OTHER_EVENT_CUE.search(clause):
            continue
        moment = TemporalResolver.resolve_relative_time(clause, anchor)
        bare_time = not _TIME_WORDS.sub("", re.sub(r"\s+", "", clause))
        if moment is not None and (_DELIVERY_CUE.search(clause) or bare_time):
            stated = moment
            break

    if item.intent != IntentType.ANSWER_PENDING:
        return item.expected_completion or stated
    # The heuristic client fills an answer's time from any date in the message, so
    # trust it only for a time the resolver could not read in the message at all.
    if stated is None and TemporalResolver.resolve_relative_time(message, anchor) is None:
        return item.expected_completion
    return (item.expected_completion or stated) if stated is not None else None


def _wait_item(quote: str, task_id: str | None, polarity: Polarity) -> IntentItem:
    return IntentItem(
        intent=IntentType.DEPENDENCY_WAIT,
        task_id=task_id,
        speech_act=SpeechAct.STATEMENT,
        polarity=polarity,
        certainty=Certainty.CERTAIN,
        source_quote=quote,
        confidence=1.0,
    )


def _only_repeats_a_problem(item: IntentItem) -> bool:
    return (
        item.intent == IntentType.REPORT_STATUS
        and item.health_status in {None, HealthStatus.UNKNOWN, HealthStatus.AT_RISK}
        and item.expected_completion is None
    )


def _adds_nothing_to_wait(item: IntentItem, foreign_upstreams: set[str]) -> bool:
    """Items that are never kept next to a wait, whether or not the wait opens.

    Saying "I am waiting on X" says nothing about X's health, so it must not become
    the waiter's risk claim on someone else's task.
    """
    if item.intent == IntentType.CHITCHAT:
        return True
    return _only_repeats_a_problem(item) and (
        item.task_id is None or item.task_id in foreign_upstreams
    )


def restates_opened_wait(item: IntentItem, opened_task_ids: set[str]) -> bool:
    """A report on a task whose wait just opened: recording it would question the waiter."""
    return _only_repeats_a_problem(item) and item.task_id in opened_task_ids


def describe_waits(state: OrgState, task: TaskState) -> list[str]:
    """Status lines for a task's open waits, so an unanswered wait stays queryable."""
    lines: list[str] = []
    for wait in active_waits(state):
        upstream = state.tasks.get(wait.upstream_task_id)
        if wait.waiting_task_id != task.task_id or upstream is None:
            continue
        owner = _name(state, upstream.owner_id)
        if wait.status is DependencyWaitStatus.ESTIMATED:
            lines.append(
                f"正在等【{upstream.title}】：{owner} 预计 "
                f"{format_moment(wait.estimated_at)} 可交付或联调"
            )
        else:
            lines.append(f"正在等【{upstream.title}】：{owner} 的交付时间待确认")
    return lines


def _name(state: OrgState, member_id: str) -> str:
    member = state.members.get(member_id)
    return member.display_name if member is not None else member_id


class DependencyWaitHandler:
    """Turns wait statements and upstream replies into events and honest replies."""

    def __init__(
        self,
        service: "GatewayService",
        dialogue: DialogueStateStore,
        confidence_threshold: float,
    ) -> None:
        self.service = service
        self.dialogue = dialogue
        self.confidence_threshold = confidence_threshold

    def _has_pending_question(self, project_id: str, member_id: str, occurred_at: datetime) -> bool:
        return (
            self.dialogue.get_pending_clarification(project_id, member_id, now=occurred_at)
            is not None
        )

    def promote(
        self,
        result: IntentResult,
        *,
        project_id: str,
        member: MemberState,
        message: str,
        state: OrgState,
        occurred_at: datetime,
    ) -> IntentResult:
        """Adds the wait item the frozen prompt does not produce for plain statements."""
        # A probe answer that mentions waiting is still an answer to the probe.
        if self._has_pending_question(project_id, member.member_id, occurred_at):
            return result

        wait_items = [item for item in result.items if item.intent == IntentType.DEPENDENCY_WAIT]
        if not wait_items:
            quote = message.strip()
            tasks = wait_statement_tasks(state, member.member_id, message)
            if cancels_wait(message) and any(
                wait.reported_by == member.member_id for wait in active_waits(state)
            ):
                wait_items = [_wait_item(quote, None, Polarity.NEGATIVE)]
            elif tasks:
                named = sorted({item.task_id for item in result.items if item.task_id in tasks})
                task_id = named[0] if len(named) == 1 else (tasks[0] if len(tasks) == 1 else None)
                wait_items = [_wait_item(quote, task_id, Polarity.AFFIRMATIVE)]
            else:
                return result

        foreign_upstreams = {
            dependency
            for task in self._owned_with_dependencies(state, member)
            for dependency in task.dependencies
            if dependency in state.tasks and state.tasks[dependency].owner_id != member.member_id
        }
        # Wait items go first so later items can see whether the wait really opened.
        others = [
            item
            for item in result.items
            if item.intent != IntentType.DEPENDENCY_WAIT
            and not _adds_nothing_to_wait(item, foreign_upstreams)
        ]
        return result.model_copy(update={"items": [*wait_items, *others]})

    async def bind_replies(
        self,
        result: IntentResult,
        *,
        project_id: str,
        member: MemberState,
        message: str,
        occurred_at: datetime,
        agent: Any,
        message_key: str,
        source_ref: str,
        parts: list[str],
        kinds: list[ReplyKind],
        data: dict[str, Any],
    ) -> tuple[IntentResult, bool]:
        """Consumes the upstream owner's estimate; returns what is left and whether we asked."""
        if self._has_pending_question(project_id, member.member_id, occurred_at):
            return result, False
        state = agent.projector.state
        awaited = awaited_waits(agent.case_ledger.get_all_cases(), state, member.member_id)
        if not awaited:
            return result, False

        remaining: list[IntentItem] = []
        estimates: dict[str, tuple[datetime, str]] = {}
        asks = False
        answered_without_time = False
        awaited_upstreams = sorted({wait.upstream_task_id for wait in awaited})
        for item in result.items:
            # An answer inherits the task we asked about; it must never reach the
            # claim path without one.
            if item.intent == IntentType.ANSWER_PENDING and item.task_id is None:
                if len(awaited_upstreams) > 1:
                    titles = "、".join(state.tasks[task].title for task in awaited_upstreams)
                    parts.append(f"你说的是哪个任务？正在等你回复的有：{titles}")
                    kinds.append(ReplyKind.CLARIFY)
                    asks = True
                    continue
                item = item.model_copy(update={"task_id": awaited_upstreams[0]})

            targets = [w for w in awaited if item.task_id in (None, w.upstream_task_id)]
            if (
                item.intent not in _REPLY_INTENTS
                or item.speech_act != SpeechAct.STATEMENT
                or item.polarity == Polarity.NEGATIVE
                or not targets
                or check_grounding(item, message).verdict != "KEEP"
                or check_time_expression(item, message, occurred_at).verdict != "KEEP"
            ):
                remaining.append(item)
                continue

            upstream_ids = sorted({wait.upstream_task_id for wait in targets})
            if len(upstream_ids) > 1:
                titles = "、".join(state.tasks[task_id].title for task_id in upstream_ids)
                parts.append(f"你说的是哪个任务？正在等你回复的有：{titles}")
                kinds.append(ReplyKind.CLARIFY)
                asks = True
                continue

            estimate = _delivery_time(item, message, occurred_at)
            if estimate is None or estimate <= occurred_at:
                if item.intent != IntentType.ANSWER_PENDING:
                    remaining.append(item)
                    continue
                # at_risk is the heuristic default for any answer, so it says nothing.
                if item.blocker or item.health_status in {
                    HealthStatus.ON_TRACK,
                    HealthStatus.DELAYED,
                }:
                    remaining.append(item)
                if answered_without_time:
                    continue
                answered_without_time = True
                cases = [
                    case
                    for wait in targets
                    if (case := agent.case_ledger.get_case(wait_case_id(wait.wait_id)))
                ]
                if all(case.round_count < _MAX_ESTIMATE_FOLLOW_UPS for case in cases):
                    for case in cases:
                        case.round_count += 1
                    await self.service.save_agent_state(agent)
                    parts.append(_ASK_FOR_ESTIMATE)
                    kinds.append(ReplyKind.CLARIFY)
                    asks = True
                else:
                    parts.append(_ACK_WITHOUT_ESTIMATE)
                    kinds.append(ReplyKind.NOTED)
                continue

            for wait in targets:
                estimates[wait.wait_id] = (estimate, item.source_quote)
            # A delivery time alone is not a risk report: classifiers label "X 点能好"
            # at_risk by rule. Only an explicit delay, a stated blocker or a personal
            # commitment still goes through the normal claim path.
            if (
                item.intent == IntentType.MAKE_COMMITMENT
                or item.health_status == HealthStatus.DELAYED
                or item.blocker
            ):
                remaining.append(item)

        remainder = result.model_copy(update={"items": remaining, "answers_pending": False})
        if not estimates:
            return remainder, asks

        events = [
            DependencyDeliveryEstimatedEvent(
                project_id=project_id,
                event_id=f"evt-dep-estimate-{message_key}-{index}",
                event_type="dependency.delivery_estimated",
                source=EventSource.MESSAGE,
                source_ref=source_ref,
                actor_id=member.member_id,
                occurred_at=occurred_at,
                received_at=occurred_at,
                payload=DependencyDeliveryEstimatedPayload(
                    wait_id=wait_id, estimated_at=estimate, quote=quote
                ),
            )
            for index, (wait_id, (estimate, quote)) in enumerate(sorted(estimates.items()))
        ]
        committed, reason, round_num, error = await self.service.commit_extracted_events(
            project_id, events, actor_id=member.member_id, occurred_at=occurred_at
        )
        if error is not None:
            data["rejected"] = error
            parts.append(f"消息处理失败：{error}")
            kinds.append(ReplyKind.NOTED)
            return remainder, asks

        data["is_actionable"] = True
        data["turn_termination_reason"] = reason
        data["turn_round_number"] = round_num
        wait = state.dependency_waits[sorted(estimates)[0]]
        title = state.tasks[wait.upstream_task_id].title
        moment = format_moment(estimates[wait.wait_id][0])
        failed = any(
            (case := committed.case_ledger.get_case(wait_case_id(wait_id))) is not None
            and case.status is CoordinationCaseStatus.ESCALATED
            for wait_id in estimates
        )
        outcome = "但转告等待方失败，请直接联系对方" if failed else "已转告等待方"
        parts.append(
            f"已记录：【{title}】预计 {moment} 可交付或联调，{outcome}。"
            "这是预计时间，不代表已经交付。"
        )
        kinds.append(ReplyKind.CLAIM_RECORDED)
        return remainder, asks

    async def handle_item(
        self,
        item: IntentItem,
        *,
        project_id: str,
        member: MemberState,
        message: str,
        occurred_at: datetime,
        message_key: str,
        source_ref: str,
        counter: list[int],
        opened: set[str],
        parts: list[str],
        kinds: list[ReplyKind],
        data: dict[str, Any],
    ) -> bool:
        """Handles one wait item; returns True when the sender was asked something.

        ``opened`` collects the waiting tasks whose wait is now on record.
        """

        def reply(text: str, kind: ReplyKind) -> bool:
            parts.append(text)
            kinds.append(kind)
            return kind == ReplyKind.CLARIFY

        agent = await self.service.get_or_replay_agent(project_id)
        state = agent.projector.state

        if item.speech_act == SpeechAct.QUESTION:
            return reply("如果已经在等，可以直接说「某任务在等某任务」。", ReplyKind.NOTED)
        if item.polarity == Polarity.NEGATIVE:
            return await self._cancel(
                item,
                state=state,
                project_id=project_id,
                member=member,
                message=message,
                occurred_at=occurred_at,
                message_key=message_key,
                source_ref=source_ref,
                counter=counter,
                reply=reply,
                data=data,
            )
        if item.certainty == Certainty.HEDGED or item.confidence < self.confidence_threshold:
            return reply("想确认一下：你现在确实在等上游交付吗？在等哪个任务？", ReplyKind.CLARIFY)

        waiting = self._waiting_task(item, state, member, message)
        if waiting is None:
            owned = [task.title for task in self._owned_with_dependencies(state, member)]
            if not owned:
                return reply(
                    "你负责的任务没有登记上游依赖，我不能自动联系其他成员，"
                    "可以请 PM 补充依赖关系。",
                    ReplyKind.NOTED,
                )
            return reply(f"是哪个任务在等？你负责的有：{'、'.join(owned)}", ReplyKind.CLARIFY)
        if waiting.workflow_status == WorkflowStatus.DONE:
            return reply(
                f"【{waiting.title}】的正式状态已经是完成，不再登记等待。", ReplyKind.NOTED
            )

        resolution = resolve_upstream(state, waiting, message)
        titles = "、".join(state.tasks[task_id].title for task_id in resolution.candidates)
        match resolution.outcome:
            case UpstreamOutcome.NO_DEPENDENCY:
                return reply(
                    f"【{waiting.title}】没有登记上游依赖，我不能自动联系其他成员，"
                    "可以请 PM 补充依赖关系。",
                    ReplyKind.NOTED,
                )
            case UpstreamOutcome.ALREADY_DONE:
                return reply(
                    f"【{titles}】的正式状态已经是完成，请确认是否还在等。", ReplyKind.NOTED
                )
            case UpstreamOutcome.UNNAMED | UpstreamOutcome.AMBIGUOUS:
                return reply(
                    f"【{waiting.title}】在等哪个任务？它的上游有：{titles}", ReplyKind.CLARIFY
                )
            case UpstreamOutcome.SELF_OWNED:
                title = state.tasks[resolution.task_id or ""].title
                return reply(f"【{title}】也由你负责，不需要我去联系其他人。", ReplyKind.NOTED)

        upstream = state.tasks[resolution.task_id or ""]
        existing = active_wait_for_pair(state, waiting.task_id, upstream.task_id)
        wait_id = (
            existing.wait_id
            if existing is not None
            else f"wait:{waiting.task_id}:{upstream.task_id}:{message_key[:12]}"
        )
        counter[0] += 1
        event = DependencyWaitReportedEvent(
            project_id=project_id,
            event_id=f"evt-dep-wait-{message_key}-{counter[0]}",
            event_type="dependency.wait_reported",
            source=EventSource.MESSAGE,
            source_ref=source_ref,
            actor_id=member.member_id,
            occurred_at=occurred_at,
            received_at=occurred_at,
            payload=DependencyWaitReportedPayload(
                wait_id=wait_id,
                waiting_task_id=waiting.task_id,
                upstream_task_id=upstream.task_id,
                quote=item.source_quote,
            ),
        )
        committed, reason, round_num, error = await self.service.commit_extracted_events(
            project_id, [event], actor_id=member.member_id, occurred_at=occurred_at
        )
        if error is not None:
            data["rejected"] = error
            return reply(f"消息处理失败：{error}", ReplyKind.NOTED)

        opened.add(waiting.task_id)
        data["is_actionable"] = True
        data["dependency_wait_id"] = wait_id
        data["turn_termination_reason"] = reason
        data["turn_round_number"] = round_num
        text = self._opened_text(committed, existing, wait_id, waiting, upstream)
        return reply(text, ReplyKind.CLAIM_RECORDED)

    @staticmethod
    def _owned_with_dependencies(state: OrgState, member: MemberState) -> list[TaskState]:
        return [
            task
            for task in sorted(state.tasks.values(), key=lambda item: item.task_id)
            if task.owner_id == member.member_id
            and task.dependencies
            and task.workflow_status != WorkflowStatus.DONE
        ]

    def _waiting_task(
        self, item: IntentItem, state: OrgState, member: MemberState, message: str
    ) -> TaskState | None:
        """The sender's own task that is waiting; other people's tasks never qualify."""
        owned = self._owned_with_dependencies(state, member)
        if item.task_id is not None:
            named = state.tasks.get(item.task_id)
            if named is not None and named.owner_id == member.member_id:
                return named
            # The model may name the upstream task instead of the waiting one.
            dependents = [task for task in owned if item.task_id in task.dependencies]
            return dependents[0] if len(dependents) == 1 else None
        matched = [
            task
            for task in owned
            if resolve_upstream(state, task, message).outcome is UpstreamOutcome.MATCHED
        ]
        if len(matched) == 1:
            return matched[0]
        # Two of the sender's tasks can wait on the same upstream: the name decides.
        named_here = [task for task in matched if names_task_exactly(task, message)]
        if len(named_here) == 1:
            return named_here[0]
        return owned[0] if len(owned) == 1 else None

    @staticmethod
    def _opened_text(
        agent: Any,
        existing: DependencyWait | None,
        wait_id: str,
        waiting: TaskState,
        upstream: TaskState,
    ) -> str:
        state = agent.projector.state
        owner = _name(state, upstream.owner_id)
        case = agent.case_ledger.get_case(wait_case_id(wait_id))
        if existing is not None and (
            case is None or case.status is not CoordinationCaseStatus.ESCALATED
        ):
            if existing.status is DependencyWaitStatus.ESTIMATED:
                moment = format_moment(existing.estimated_at)
                return f"这个等待已经登记过了，{owner} 预计 {moment} 可交付或联调。"
            return f"这个等待已经登记过了，仍在等 {owner} 回复，这条消息已追加为记录。"

        if case is None or case.status is CoordinationCaseStatus.ESCALATED:
            return (
                f"已记录【{waiting.title}】在等【{upstream.title}】，"
                f"但没能联系到 {owner}，请直接联系对方。"
            )
        commands = case.executed_commands
        asked = any(cmd.action_type is ActionType.ASK_DEPENDENCY_ETA for cmd in commands)
        notices = [cmd for cmd in commands if cmd.action_type is ActionType.NOTIFY_WAIT_OPENED]
        pm_failed = any(cmd.action_id in case.delivery_failures for cmd in notices)
        contact = f"已向 {owner} 确认交付时间" if asked else f"{owner} 已被问过同一任务的交付时间"
        pm_note = "，但通知 PM 失败" if pm_failed else ("，并告知 PM" if notices else "")
        return (
            f"已记录：【{waiting.title}】在等【{upstream.title}】。"
            f"{contact}{pm_note}，有回复后我会转告你。"
        )

    async def _cancel(
        self,
        item: IntentItem,
        *,
        state: OrgState,
        project_id: str,
        member: MemberState,
        message: str,
        occurred_at: datetime,
        message_key: str,
        source_ref: str,
        counter: list[int],
        reply: Any,
        data: dict[str, Any],
    ) -> bool:
        mine = [wait for wait in active_waits(state) if wait.reported_by == member.member_id]
        ambiguous_subject = False
        if item.task_id is not None:
            mine = [
                wait
                for wait in mine
                if item.task_id in (wait.waiting_task_id, wait.upstream_task_id)
            ]
        else:
            named = [
                wait
                for wait in mine
                if mention_score(state.tasks[wait.upstream_task_id], message) >= 2
            ]
            ambiguous_subject = not named and not is_bare_cancel(message)
            mine = named or mine
        if not mine:
            return reply("当前没有你登记的等待。", ReplyKind.NOTED)
        if len(mine) > 1 or ambiguous_subject:
            titles = "、".join(state.tasks[wait.upstream_task_id].title for wait in mine)
            return reply(f"要取消哪个等待？你正在等：{titles}", ReplyKind.CLARIFY)

        wait = mine[0]
        counter[0] += 1
        event = DependencyWaitCancelledEvent(
            project_id=project_id,
            event_id=f"evt-dep-cancel-{message_key}-{counter[0]}",
            event_type="dependency.wait_cancelled",
            source=EventSource.MESSAGE,
            source_ref=source_ref,
            actor_id=member.member_id,
            occurred_at=occurred_at,
            received_at=occurred_at,
            payload=DependencyWaitCancelledPayload(wait_id=wait.wait_id, quote=item.source_quote),
        )
        _, reason, round_num, error = await self.service.commit_extracted_events(
            project_id, [event], actor_id=member.member_id, occurred_at=occurred_at
        )
        if error is not None:
            data["rejected"] = error
            return reply(f"消息处理失败：{error}", ReplyKind.NOTED)
        data["is_actionable"] = True
        data["turn_termination_reason"] = reason
        data["turn_round_number"] = round_num
        title = state.tasks[wait.upstream_task_id].title
        return reply(f"好的，已取消对【{title}】的等待。", ReplyKind.CLAIM_RECORDED)

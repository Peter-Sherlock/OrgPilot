"""Task operation manager: NL task creation, reassignment, and deadline changes.

The intent router classifies the supported task operations; the extractor
produces a verbatim-slot ``TaskProposal`` in the same LLM call. This manager
grounds the proposal against the live directory and task ledger, then gates the
operation behind a human approval (governance red line: NL task operations are
never executed directly). Settling an approved proposal emits the kernel's own
``task.created`` / ``task.updated`` events, so creation and reassignment are
event-sourced like everything else — replay, recovery, and DAG updates are free.

Like DirectiveManager, the manager is stateless: pending proposals live in the
ApprovalManager (persisted side store), and all state mutation flows through
typed events.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.coordination.directives import DirectiveNotice
from orgpilot.domain.enums import ActionType, ApprovalStatus
from orgpilot.domain.models import (
    ActionCommand,
    ApprovalRequest,
    CoordinationAction,
    OrgState,
)
from orgpilot.events.models import (
    EventSource,
    OrgEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
)
from orgpilot.extraction.intent import PRIVILEGED_ROLES
from orgpilot.extraction.models import ExtractionResult
from orgpilot.extraction.verifier import TemporalResolver


@dataclass
class TaskOutcome:
    """Result of a task-operation step for gateway surfacing."""

    # proposed | declined | clarify | created | reassigned | deadline_changed | rejected | none
    kind: str
    bot_reply: str | None = None
    notices: list[DirectiveNotice] = field(default_factory=list)
    events: list[OrgEvent] = field(default_factory=list)
    outbound: list[ActionCommand] = field(default_factory=list)
    approval: ApprovalRequest | None = None


class TaskManager:
    """Grounds NL task proposals and settles their approvals into kernel events."""

    def __init__(
        self,
        adapter: CollaborationAdapter,
        approval_manager,
        reference_timezone: str = "Asia/Shanghai",
    ) -> None:
        self.adapter = adapter
        self.approval_manager = approval_manager
        self.reference_timezone = reference_timezone

    # ----------------------------------------------------------------- create

    def handle_task_create_intent(
        self,
        message: str,
        actor_id: str,
        result: ExtractionResult,
        state: OrgState,
        occurred_at: datetime,
    ) -> TaskOutcome:
        """Turns a task_create intent into a gated proposal, or a decline/clarify."""
        gate = self._authority_gate(state, actor_id, "创建任务")
        if gate is not None:
            return gate

        proposal = result.task_proposal
        if proposal is not None and proposal.operation != "create":
            proposal = None
        title = (proposal.title if proposal else None) or ""
        title = title.strip()
        if not title:
            return TaskOutcome(
                kind="clarify",
                bot_reply="请告诉我新任务的名称（例如：新增任务：网关压测，David 负责）。",
            )
        if not self._is_verbatim_slot(title, message):
            return TaskOutcome(
                kind="clarify",
                bot_reply="提取出的任务名称无法与原消息逐字对齐，请重新明确任务名称。",
            )

        owner_id, owner_note = self._resolve_owner(state, proposal, result, message)
        if owner_id is None:
            return TaskOutcome(
                kind="clarify",
                bot_reply=(
                    f"新任务「{title}」的负责人没认出来（{owner_note}）。"
                    "请用成员目录中的名称重新说明。"
                ),
            )

        conflict = self._task_id_for_title(state, title)
        if conflict is not None:
            return TaskOutcome(
                kind="clarify",
                bot_reply=(
                    f"任务「{title}」已存在（{conflict}），不能重复创建。"
                    "如需调整请直接说明（改期或改派）。"
                ),
            )

        task_id = self._new_task_id(state.project_id, title)
        deadline = self._resolve_deadline(
            (proposal.deadline_expr if proposal else None)
            or (result.hints.raw_time_expr if result.hints else None),
            occurred_at,
        )

        payload = {
            "proposal_kind": "task_create",
            "task_id": task_id,
            "task_title": title,
            "owner_id": owner_id,
            "owner_name": self._display_name(state, owner_id),
            "deadline": deadline.isoformat() if deadline else None,
            "proposed_by": actor_id,
        }
        approval, command = self._create_approval(
            state,
            actor_id=actor_id,
            action_id=self._proposal_action_id(
                "task-create", task_id, actor_id, payload, occurred_at
            ),
            payload=payload,
            occurred_at=occurred_at,
        )

        owner_name = self._display_name(state, owner_id)
        deadline_str = self._format_deadline(deadline)
        reply = (
            f"🆕 已生成任务创建提案，等待您在审批卡确认：\n"
            f"• 任务：{title}\n• 负责人：{owner_name}\n"
            f"• 截止：{deadline_str or '未指定'}\n"
            "批准后任务立即进入项目 DAG，并通知负责人。"
        )
        notice = DirectiveNotice(
            actor_id=actor_id,
            text=f"🆕 任务创建提案待审批：{title} → {owner_name}",
        )
        return TaskOutcome(
            kind="proposed",
            bot_reply=reply,
            notices=[notice],
            outbound=[command],
            approval=approval,
        )

    # -------------------------------------------------------------- reassign

    def handle_task_reassign_intent(
        self,
        message: str,
        actor_id: str,
        result: ExtractionResult,
        state: OrgState,
        occurred_at: datetime,
    ) -> TaskOutcome:
        """Turns a task_reassign intent into a gated proposal, or a decline/clarify."""
        gate = self._authority_gate(state, actor_id, "改派任务")
        if gate is not None:
            return gate

        proposal = result.task_proposal
        if proposal is not None and proposal.operation != "reassign":
            proposal = None
        task_id = self._resolve_task_ref(state, proposal, result, message)
        if task_id is None:
            return TaskOutcome(
                kind="clarify",
                bot_reply=(
                    "没认出要改派的任务，请指明任务名称或 ID（例如：把收银台前端结账转给 Alice）。"
                ),
            )

        owner_id, owner_note = self._resolve_owner(state, proposal, result, message)
        if owner_id is None:
            return TaskOutcome(
                kind="clarify",
                bot_reply=f"新负责人没认出来（{owner_note}）。请用成员目录中的名称重新说明。",
            )
        if owner_id == state.tasks[task_id].owner_id:
            return TaskOutcome(
                kind="none",
                bot_reply=(
                    f"「{state.tasks[task_id].title}」的负责人本来就是 "
                    f"{self._display_name(state, owner_id)}，无需改派。"
                ),
            )

        payload = {
            "proposal_kind": "task_reassign",
            "task_id": task_id,
            "task_title": state.tasks[task_id].title,
            "owner_id": owner_id,
            "owner_name": self._display_name(state, owner_id),
            "previous_owner_id": state.tasks[task_id].owner_id,
            "previous_owner_name": self._display_name(state, state.tasks[task_id].owner_id),
            "proposed_by": actor_id,
        }
        approval, command = self._create_approval(
            state,
            actor_id=actor_id,
            action_id=self._proposal_action_id(
                "task-reassign", task_id, actor_id, payload, occurred_at
            ),
            payload=payload,
            occurred_at=occurred_at,
        )

        task_title = state.tasks[task_id].title
        reply = (
            "🔄 已生成任务改派提案，等待您在审批卡确认：\n"
            f"• 任务：{task_title}\n"
            f"• 负责人：{self._display_name(state, state.tasks[task_id].owner_id)} → "
            f"{self._display_name(state, owner_id)}\n"
            "批准后立即生效并通知双方。"
        )
        notice = DirectiveNotice(
            actor_id=actor_id,
            text=f"🔄 任务改派提案待审批：{task_title}",
        )
        return TaskOutcome(
            kind="proposed",
            bot_reply=reply,
            notices=[notice],
            outbound=[command],
            approval=approval,
        )

    # -------------------------------------------------------- deadline change

    def handle_deadline_change_intent(
        self,
        message: str,
        actor_id: str,
        result: ExtractionResult,
        state: OrgState,
        occurred_at: datetime,
    ) -> TaskOutcome:
        """Turns a deadline-change intent into a grounded, gated proposal."""
        gate = self._authority_gate(state, actor_id, "变更任务截止期")
        if gate is not None:
            return gate

        proposal = result.task_proposal
        if proposal is not None and proposal.operation != "deadline_change":
            proposal = None
        task_id = self._resolve_task_ref(state, proposal, result, message)
        if task_id is None:
            return TaskOutcome(
                kind="clarify",
                bot_reply="没认出要改期的任务，请指明任务名称或 ID。",
            )

        proposed_time = proposal.deadline_expr if proposal else None
        if proposed_time and not self._is_verbatim_slot(proposed_time, message):
            proposed_time = None
        time_expr = proposed_time or (result.hints.raw_time_expr if result.hints else None)
        deadline = self._resolve_deadline(time_expr or message, occurred_at)
        if deadline is None:
            return TaskOutcome(
                kind="clarify",
                bot_reply=(
                    f"已找到任务「{state.tasks[task_id].title}」，但没认出新的截止时间。"
                    "请说明完整日期和时刻，例如“改到后天下午 5 点”。"
                ),
            )

        task = state.tasks[task_id]
        if task.deadline == deadline:
            return TaskOutcome(
                kind="none",
                bot_reply=(
                    f"「{task.title}」当前截止时间已经是 {self._format_deadline(deadline)}，"
                    "无需改期。"
                ),
            )

        impacted_tasks = self._downstream_tasks(state, task_id)
        conflicts = tuple(
            impacted_id
            for impacted_id in impacted_tasks
            if state.tasks[impacted_id].deadline is not None
            and state.tasks[impacted_id].deadline <= deadline
        )
        payload = {
            "proposal_kind": "deadline_change",
            "task_id": task_id,
            "task_title": task.title,
            "owner_id": task.owner_id,
            "owner_name": self._display_name(state, task.owner_id),
            "previous_deadline": task.deadline.isoformat() if task.deadline else None,
            "new_deadline": deadline.isoformat(),
            "impacted_tasks": list(impacted_tasks),
            "conflicting_tasks": list(conflicts),
            "risk_level": "HIGH" if conflicts else "MEDIUM",
            "proposed_by": actor_id,
        }
        approval, command = self._create_approval(
            state,
            actor_id=actor_id,
            action_id=self._proposal_action_id(
                "deadline-change", task_id, actor_id, payload, occurred_at
            ),
            payload=payload,
            occurred_at=occurred_at,
        )

        impact_note = "无下游任务"
        if impacted_tasks:
            impact_note = f"影响 {len(impacted_tasks)} 个下游任务"
            if conflicts:
                impact_note += f"，其中 {len(conflicts)} 个存在截止冲突"
            else:
                impact_note += "，暂未发现截止冲突"
        reply = (
            "📅 已生成任务改期提案，等待您在审批卡确认：\n"
            f"• 任务：{task.title}\n"
            f"• 截止：{self._format_deadline(task.deadline) or '未指定'} → "
            f"{self._format_deadline(deadline)}\n"
            f"• 依赖分析：{impact_note}\n"
            "批准后立即更新任务账本，并通知受影响负责人。"
        )
        return TaskOutcome(
            kind="proposed",
            bot_reply=reply,
            notices=[DirectiveNotice(actor_id=actor_id, text=f"📅 改期提案待审批：{task.title}")],
            outbound=[command],
            approval=approval,
        )

    # ---------------------------------------------------------------- settle

    def settle_approval(
        self,
        state: OrgState,
        request: ApprovalRequest,
        operator_id: str,
        occurred_at: datetime,
    ) -> TaskOutcome | None:
        """Settles an approved/rejected task-op approval into kernel events.

        Returns None for approvals that are not task operations or were already
        settled. Approved proposals emit ``task.created`` / ``task.updated``;
        both outcomes notify the affected members.
        """
        action_type = request.proposed_command.action_type
        payload = request.proposed_command.payload
        proposal_kind = payload.get("proposal_kind")
        expected_types = {
            "task_create": ActionType.TASK_CREATE,
            "task_reassign": ActionType.TASK_REASSIGN,
            "deadline_change": ActionType.PROPOSE_RESCHEDULE,
        }
        if expected_types.get(proposal_kind) is not action_type:
            return None

        task_title = str(payload.get("task_title", payload.get("task_id", "")))

        if request.status is ApprovalStatus.REJECTED:
            request.consumed = True
            return TaskOutcome(
                kind="rejected",
                bot_reply=f"已拒绝任务提案「{task_title}」，未做任何变更。",
                notices=[
                    DirectiveNotice(
                        actor_id=str(payload.get("proposed_by", operator_id)),
                        text=f"❌ 任务提案「{task_title}」已被拒绝。",
                    )
                ],
            )

        if request.status is not ApprovalStatus.APPROVED or request.consumed:
            return None
        self.approval_manager.consume(request.approval_id, occurred_at)

        if action_type is ActionType.TASK_CREATE:
            deadline_raw = payload.get("deadline")
            deadline = datetime.fromisoformat(deadline_raw) if deadline_raw else None
            event = TaskCreatedEvent(
                project_id=state.project_id,
                event_id=f"evt-task-create-{payload['task_id']}",
                event_type="task.created",
                source=EventSource.TASK,
                source_ref=f"approval:{request.approval_id}",
                actor_id=operator_id,
                occurred_at=occurred_at,
                received_at=occurred_at,
                payload={
                    "task_id": payload["task_id"],
                    "title": payload["task_title"],
                    "owner_id": payload["owner_id"],
                    "deadline": deadline,
                },
            )
            notify_text = (
                f"📩 新任务分配（来自 {self._display_name(state, operator_id)}）："
                f"【{task_title}】"
                f"{'　截止：' + self._format_deadline(deadline) if deadline else ''}\n"
                "请回复「收到」确认，完成后同步进度。"
            )
            return TaskOutcome(
                kind="created",
                bot_reply=f"✅ 任务「{task_title}」已创建并进入项目 DAG，已通知负责人。",
                notices=[DirectiveNotice(actor_id=str(payload["owner_id"]), text=notify_text)],
                events=[event],
                outbound=[
                    self._notify_command(
                        state.project_id, payload["owner_id"], notify_text, occurred_at
                    )
                ],
            )

        if proposal_kind == "deadline_change":
            deadline = datetime.fromisoformat(str(payload["new_deadline"]))
            event_suffix = hashlib.sha256(request.approval_id.encode("utf-8")).hexdigest()[:10]
            event = TaskUpdatedEvent(
                project_id=state.project_id,
                event_id=f"evt-task-deadline-{payload['task_id']}-{event_suffix}",
                event_type="task.updated",
                source=EventSource.TASK,
                source_ref=f"approval:{request.approval_id}",
                actor_id=operator_id,
                occurred_at=occurred_at,
                received_at=occurred_at,
                payload={"task_id": payload["task_id"], "deadline": deadline},
            )
            notices: list[DirectiveNotice] = []
            outbound: list[ActionCommand] = []
            affected_by_owner: dict[str, list[str]] = {}
            affected_by_owner.setdefault(str(payload["owner_id"]), []).append(task_title)
            for impacted_id in payload.get("impacted_tasks", []):
                impacted = state.tasks.get(str(impacted_id))
                if impacted is not None:
                    affected_by_owner.setdefault(impacted.owner_id, []).append(impacted.title)
            for owner_id, titles in affected_by_owner.items():
                if owner_id == str(payload["owner_id"]):
                    text = (
                        f"📅 任务「{task_title}」截止时间已调整为 "
                        f"{self._format_deadline(deadline)}，请据此更新计划。"
                    )
                else:
                    text = (
                        f"⚠️ 上游任务「{task_title}」已改期至 "
                        f"{self._format_deadline(deadline)}，可能影响：{'、'.join(titles)}。"
                    )
                notices.append(DirectiveNotice(actor_id=owner_id, text=text))
                outbound.append(self._notify_command(state.project_id, owner_id, text, occurred_at))
            return TaskOutcome(
                kind="deadline_changed",
                bot_reply=(
                    f"✅ 任务「{task_title}」截止时间已更新为 "
                    f"{self._format_deadline(deadline)}，已通知受影响负责人。"
                ),
                notices=notices,
                events=[event],
                outbound=outbound,
            )

        # Reassignment
        event_suffix = hashlib.sha256(request.approval_id.encode("utf-8")).hexdigest()[:10]
        event = TaskUpdatedEvent(
            project_id=state.project_id,
            event_id=f"evt-task-reassign-{payload['task_id']}-{event_suffix}",
            event_type="task.updated",
            source=EventSource.TASK,
            source_ref=f"approval:{request.approval_id}",
            actor_id=operator_id,
            occurred_at=occurred_at,
            received_at=occurred_at,
            payload={"task_id": payload["task_id"], "owner_id": payload["owner_id"]},
        )
        new_owner = str(payload["owner_id"])
        previous_owner = str(payload.get("previous_owner_id", ""))
        notify_new = (
            f"📩 任务移交（来自 {self._display_name(state, operator_id)}）："
            f"【{task_title}】现由你负责，请回复「收到」确认。"
        )
        notices = [DirectiveNotice(actor_id=new_owner, text=notify_new)]
        outbound = [self._notify_command(state.project_id, new_owner, notify_new, occurred_at)]
        if previous_owner and previous_owner != new_owner:
            handover_text = (
                f"📤 任务「{task_title}」已改派给 "
                f"{self._display_name(state, new_owner)}，相关进展请与其交接。"
            )
            notices.append(DirectiveNotice(actor_id=previous_owner, text=handover_text))
            outbound.append(
                self._notify_command(state.project_id, previous_owner, handover_text, occurred_at)
            )
        return TaskOutcome(
            kind="reassigned",
            bot_reply=(
                f"✅ 任务「{task_title}」已改派给 "
                f"{self._display_name(state, new_owner)}，已通知双方。"
            ),
            notices=notices,
            events=[event],
            outbound=outbound,
        )

    # -------------------------------------------------------------- internals

    def _authority_gate(self, state: OrgState, actor_id: str, operation: str) -> TaskOutcome | None:
        issuer = state.members.get(actor_id)
        if issuer is None:
            return TaskOutcome(kind="declined", bot_reply="未识别的发送者，无法发起任务操作。")
        if issuer.role not in PRIVILEGED_ROLES:
            return TaskOutcome(
                kind="declined",
                bot_reply=(
                    f"已识别为{operation}请求，但您当前角色（{issuer.role}）无权限直接执行。"
                    "请联系项目负责人 (PM) 发起；您的需求已被记录。"
                ),
            )
        return None

    def _create_approval(
        self,
        state: OrgState,
        *,
        actor_id: str,
        action_id: str,
        payload: dict,
        occurred_at: datetime,
    ) -> tuple[ApprovalRequest, ActionCommand]:
        """Registers the gated proposal and returns it with its card command.

        The proposer is the approver (PM self-confirm gate): the card exists to
        force a human review of LLM-proposed parameters before they land, not to
        cross-approve between people.
        """
        action_types = {
            "task_create": ActionType.TASK_CREATE,
            "task_reassign": ActionType.TASK_REASSIGN,
            "deadline_change": ActionType.PROPOSE_RESCHEDULE,
        }
        action_type = action_types[payload["proposal_kind"]]
        action = CoordinationAction(
            action_id=action_id,
            action_type=action_type,
            targets=(actor_id,),
            reason_refs=(),
            expected_effect=f"NL task operation proposed by {actor_id}",
            payload=dict(payload),
        )
        command = ActionCommand(
            command_id=f"cmd:{action_id}:{int(occurred_at.timestamp())}",
            action_id=action_id,
            action_type=action.action_type,
            targets=action.targets,
            payload={**payload, "approval_id": "", "case_id": f"case:{action_id}"},
            created_at=occurred_at,
            idempotency_key=f"idem:{action_id}",
        )
        approval = self.approval_manager.create_request(
            f"case:{action_id}",
            action,
            command,
            actor_id,
            occurred_at,
            expires_at=occurred_at + timedelta(days=2),
        )
        command = command.model_copy(
            update={
                "payload": {
                    **payload,
                    "approval_id": approval.approval_id,
                    "case_id": f"case:{action_id}",
                }
            }
        )
        request = self.approval_manager.get_request(approval.approval_id)
        if request is not None:
            request.proposed_command = command
        return request or approval, command

    def _resolve_owner(
        self, state: OrgState, proposal, result: ExtractionResult, message: str
    ) -> tuple[str | None, str]:
        """Resolves the proposed owner name to a directory member id."""
        candidates: list[str] = []
        if (
            proposal is not None
            and proposal.owner_name
            and self._is_verbatim_slot(proposal.owner_name, message)
        ):
            candidates.append(proposal.owner_name.strip())
        if result.hints and result.hints.mentioned_member_ids:
            candidates.extend(result.hints.mentioned_member_ids)
        for candidate in candidates:
            lowered = candidate.lower()
            for member_id, member in state.members.items():
                if lowered in (member_id.lower(), member.display_name.lower()):
                    return member_id, ""
            suffix = lowered.split("_")[-1]
            if len(suffix) >= 3 and suffix.isascii() and suffix.isalpha():
                for member_id in state.members:
                    if member_id.lower().endswith(suffix):
                        return member_id, ""
        return None, "、".join(candidates) or "未提供负责人"

    def _resolve_task_ref(
        self, state: OrgState, proposal, result: ExtractionResult, message: str
    ) -> str | None:
        """Resolves the referenced existing task from the proposal or hints."""
        refs: list[str] = []
        if (
            proposal is not None
            and proposal.task_ref
            and self._is_verbatim_slot(proposal.task_ref, message)
        ):
            refs.append(proposal.task_ref)
        if result.hints and result.hints.mentioned_task_ids:
            refs.extend(result.hints.mentioned_task_ids)
        for ref in refs:
            if ref in state.tasks:
                return ref
            compact = ref.lower().replace(" ", "")
            for task_id, task in state.tasks.items():
                if task_id.lower() in compact or task.title.lower().replace(" ", "") in compact:
                    return task_id
        return None

    @staticmethod
    def _is_verbatim_slot(value: str, message: str) -> bool:
        """Checks the prompt's grounding red line with case-insensitive matching."""
        return value.strip().casefold() in message.casefold()

    @staticmethod
    def _task_id_for_title(state: OrgState, title: str) -> str | None:
        compact = title.lower().replace(" ", "")
        for task_id, task in state.tasks.items():
            if task.title.lower().replace(" ", "") == compact:
                return task_id
        return None

    @staticmethod
    def _new_task_id(project_id: str, title: str) -> str:
        identity = f"{project_id}|{title.lower().replace(' ', '')}"
        return f"task-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]}"

    def _resolve_deadline(self, time_expr: str | None, occurred_at: datetime) -> datetime | None:
        if not time_expr:
            return None
        try:
            anchor = occurred_at.astimezone(ZoneInfo(self.reference_timezone))
        except Exception:
            anchor = occurred_at
        return TemporalResolver.resolve_relative_time(time_expr, anchor)

    @staticmethod
    def _proposal_action_id(
        operation: str,
        task_id: str,
        actor_id: str,
        payload: dict,
        occurred_at: datetime,
    ) -> str:
        """Makes retries idempotent without overwriting a later proposal for the same task."""
        fields = "|".join(f"{key}={payload[key]}" for key in sorted(payload))
        identity = f"{actor_id}|{occurred_at.isoformat()}|{fields}"
        suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        return f"action:{operation}:{task_id}:{suffix}"

    @staticmethod
    def _downstream_tasks(state: OrgState, source_task_id: str) -> tuple[str, ...]:
        """Returns every transitive task whose dependency chain includes source_task_id."""
        impacted: list[str] = []
        seen = {source_task_id}
        frontier = [source_task_id]
        while frontier:
            upstream = frontier.pop(0)
            for task_id, task in state.tasks.items():
                if task_id in seen or upstream not in task.dependencies:
                    continue
                seen.add(task_id)
                impacted.append(task_id)
                frontier.append(task_id)
        return tuple(impacted)

    def _notify_command(
        self, project_id: str, target_id: str, text: str, occurred_at: datetime
    ) -> ActionCommand:
        identity = f"{project_id}|{target_id}|{text[:24]}|{int(occurred_at.timestamp())}"
        return ActionCommand(
            command_id=f"cmd:task-notify:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}",
            action_id=f"action:task-notify:{target_id}",
            action_type=ActionType.SEND_DIRECTIVE,
            targets=[target_id],
            payload={"text": text},
            created_at=occurred_at,
            idempotency_key=f"idem:task-notify:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}",
        )

    @staticmethod
    def _display_name(state: OrgState, member_id: str) -> str:
        member = state.members.get(member_id)
        return member.display_name if member else member_id

    def _format_deadline(self, deadline: datetime | None) -> str | None:
        if deadline is None:
            return None
        try:
            local = deadline.astimezone(ZoneInfo(self.reference_timezone))
        except Exception:
            return deadline.isoformat()
        return local.strftime("%m-%d %H:%M")

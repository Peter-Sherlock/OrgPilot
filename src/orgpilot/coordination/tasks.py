"""Task operation manager: NL task creation and reassignment behind approval gates.

The intent router classifies ``task_create`` / ``task_reassign``; the extractor
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

    # proposed | declined | clarify | created | reassigned | rejected | none
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
        title = (proposal.title if proposal else None) or ""
        title = title.strip()
        if not title:
            return TaskOutcome(
                kind="clarify",
                bot_reply="请告诉我新任务的名称（例如：新增任务：网关压测，David 负责）。",
            )

        owner_id, owner_note = self._resolve_owner(state, proposal, result)
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
            action_id=f"action:task-create:{task_id}",
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
        task_id = self._resolve_task_ref(state, proposal, result)
        if task_id is None:
            return TaskOutcome(
                kind="clarify",
                bot_reply=(
                    "没认出要改派的任务，请指明任务名称或 ID（例如：把收银台前端结账转给 Alice）。"
                ),
            )

        owner_id, owner_note = self._resolve_owner(state, proposal, result)
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
            action_id=f"action:task-reassign:{task_id}",
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
        if action_type not in (ActionType.TASK_CREATE, ActionType.TASK_REASSIGN):
            return None

        payload = request.proposed_command.payload
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

        # Reassignment
        event = TaskUpdatedEvent(
            project_id=state.project_id,
            event_id=f"evt-task-reassign-{payload['task_id']}",
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
        action = CoordinationAction(
            action_id=action_id,
            action_type=(
                ActionType.TASK_CREATE
                if payload["proposal_kind"] == "task_create"
                else ActionType.TASK_REASSIGN
            ),
            targets=(str(payload["owner_id"]),),
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
        self, state: OrgState, proposal, result: ExtractionResult
    ) -> tuple[str | None, str]:
        """Resolves the proposed owner name to a directory member id."""
        candidates: list[str] = []
        if proposal is not None and proposal.owner_name:
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

    def _resolve_task_ref(self, state: OrgState, proposal, result: ExtractionResult) -> str | None:
        """Resolves the referenced existing task from the proposal or hints."""
        refs: list[str] = []
        if proposal is not None and proposal.task_ref:
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

    @staticmethod
    def _format_deadline(deadline: datetime | None) -> str | None:
        if deadline is None:
            return None
        try:
            local = deadline.astimezone(ZoneInfo("Asia/Shanghai"))
        except Exception:
            return deadline.isoformat()
        return local.strftime("%m-%d %H:%M")

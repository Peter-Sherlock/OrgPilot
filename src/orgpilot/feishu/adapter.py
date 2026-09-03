"""Feishu collaboration adapter for ActionCommands, Feishu cards, and OpenAPI calls."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.adapter.contracts import (
    DeadlineUpdate,
    PayloadContractError,
    RescheduleProposal,
    TextMessage,
    format_deadline_for_card,
)
from orgpilot.domain.enums import CommandStatus
from orgpilot.domain.models import ActionCommand, ActionResult
from orgpilot.events.models import (
    EventSource,
    OrgEvent,
    TaskUpdatedEvent,
    TaskUpdatedPayload,
)
from orgpilot.feishu.cards import (
    build_approval_card,
    build_executive_briefing_card,
    build_inquiry_card,
    build_notification_card,
    build_task_action_card,
)
from orgpilot.feishu.client import FeishuClient, MockFeishuClient


class FeishuCollaborationAdapter(CollaborationAdapter):
    """Collaboration adapter for Feishu OpenAPI, cards, messages, and task updates."""

    def __init__(
        self,
        client: FeishuClient | None = None,
        project_id: str = "feishu-project",
    ) -> None:
        self.client: FeishuClient = client or MockFeishuClient()
        self.project_id = project_id
        self.audit_log: list[tuple[ActionCommand, ActionResult]] = []
        self._generated_events: list[OrgEvent] = []

    def pop_generated_events(self) -> list[OrgEvent]:
        """Retrieves and clears newly generated events from task updates."""
        events = list(self._generated_events)
        self._generated_events.clear()
        return events

    def _run_async(self, coro: Any) -> Any:
        """Helper to run async client methods synchronously from adapter interface."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # The Agent adapter contract is synchronous. Run the coroutine on a
            # separate loop and wait so failures cannot be reported as success.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, coro).result()
        return asyncio.run(coro)

    def _result(self, command: ActionCommand, status: CommandStatus, **output: Any) -> ActionResult:
        result = ActionResult(
            command_id=command.command_id,
            action_id=command.action_id,
            status=status,
            output=output,
            executed_at=command.created_at,
        )
        self.audit_log.append((command, result))
        return result

    def send_private_message(self, command: ActionCommand) -> ActionResult:
        """Sends a private text message, or a task inquiry card when no text is present."""
        message = TextMessage.from_payload(command.payload)
        if message is None:
            return self._send_inquiry_card(command)

        for target in command.targets:
            # Feishu IM contract: msg_type="text" requires content {"text": "..."}.
            self._run_async(
                self.client.send_message(
                    receive_id=target, msg_type="text", content={"text": message.text}
                )
            )
        return self._result(
            command, CommandStatus.SUCCESS, sent_to=list(command.targets), text=message.text
        )

    def _send_inquiry_card(self, command: ActionCommand) -> ActionResult:
        task_id = command.payload.get("task_id", "unknown_task")
        title = command.payload.get("title", "")
        reason = command.payload.get("reason", "检测到排期延误或阻塞风险")

        card = build_inquiry_card(task_id=task_id, title=title, reason=reason)

        for target in command.targets:
            self._run_async(self.client.send_card(receive_id=target, card=card))
        return self._result(
            command, CommandStatus.SUCCESS, sent_to=list(command.targets), card=card
        )

    def request_approval(self, command: ActionCommand, approver_id: str) -> ActionResult:
        """Sends an interactive approval card: reschedule proposals use the risk
        card; NL task create/reassign proposals use the task-action card."""
        proposal_kind = command.payload.get("proposal_kind")
        if proposal_kind in ("task_create", "task_reassign"):
            card = build_task_action_card(
                approval_id=command.payload.get("approval_id", f"appr:{command.command_id}"),
                case_id=command.payload.get("case_id", "case:unknown"),
                proposal_kind=str(proposal_kind),
                task_title=str(command.payload.get("task_title", "")),
                owner_name=str(
                    command.payload.get("owner_name", command.payload.get("owner_id", ""))
                ),
                deadline_str=command.payload.get("deadline"),
                previous_owner_name=command.payload.get("previous_owner_name"),
                proposed_by=str(command.payload.get("proposed_by", "")),
            )
        else:
            try:
                proposal = RescheduleProposal.from_payload(command.payload)
            except PayloadContractError as exc:
                return self._failed(command, exc)

            card = build_approval_card(
                approval_id=command.payload.get("approval_id", f"appr:{command.command_id}"),
                case_id=command.payload.get("case_id", "case:unknown"),
                task_id=proposal.task_id,
                task_title=proposal.task_title,
                proposed_deadline_str=format_deadline_for_card(proposal.new_deadline),
                impacted_tasks=proposal.impacted_tasks,
                risk_level=proposal.risk_level,
            )

        self._run_async(self.client.send_card(receive_id=approver_id, card=card))
        return self._result(command, CommandStatus.SUCCESS, approver_id=approver_id, card=card)

    def update_task(self, command: ActionCommand) -> ActionResult:
        """Calls Feishu Task OpenAPI to update task deadline and generates a TaskUpdatedEvent.

        Fail-closed: a payload without a parsable ``new_deadline`` is rejected
        outright instead of writing the current time as the task deadline.
        """
        try:
            update = DeadlineUpdate.from_payload(command.payload)
        except PayloadContractError as exc:
            return self._failed(command, exc)

        self._run_async(
            self.client.update_task_deadline(task_guid=update.task_id, deadline=update.new_deadline)
        )

        # Generate local projection event
        now = datetime.now(UTC)
        evt = TaskUpdatedEvent(
            project_id=self.project_id,
            event_id=f"evt-feishu-task-upd-{command.command_id}",
            event_type="task.updated",
            source=EventSource.TASK,
            source_ref=f"command:{command.command_id}",
            occurred_at=now,
            received_at=now,
            payload=TaskUpdatedPayload(
                task_id=update.task_id,
                deadline=update.new_deadline,
            ),
        )
        self._generated_events.append(evt)

        return self._result(
            command,
            CommandStatus.SUCCESS,
            task_id=update.task_id,
            deadline=update.new_deadline.isoformat(),
        )

    def notify_group(self, command: ActionCommand) -> ActionResult:
        """Sends a broadcast notification card or executive briefing to target members."""
        if command.payload.get("is_executive_briefing"):
            card = build_executive_briefing_card(
                briefing=command.payload.get("briefing", {}),
                project_id=self.project_id,
            )
        else:
            task_id = command.payload.get("task_id", "task")
            task_title = command.payload.get("task_title", task_id)
            new_deadline_str = str(command.payload.get("new_deadline", "待定"))
            impacted_tasks = command.payload.get("impacted_tasks", [])
            approver_name = command.payload.get("approved_by", "PM")

            card = build_notification_card(
                task_id=task_id,
                task_title=task_title,
                new_deadline_str=new_deadline_str,
                impacted_tasks=impacted_tasks,
                approver_name=approver_name,
            )

        for target in command.targets:
            self._run_async(self.client.send_card(receive_id=target, card=card))
        return self._result(
            command, CommandStatus.SUCCESS, sent_to=list(command.targets), card=card
        )

    def _failed(self, command: ActionCommand, exc: PayloadContractError) -> ActionResult:
        result = ActionResult(
            command_id=command.command_id,
            action_id=command.action_id,
            status=CommandStatus.FAILED,
            error=str(exc),
            executed_at=command.created_at,
        )
        self.audit_log.append((command, result))
        return result

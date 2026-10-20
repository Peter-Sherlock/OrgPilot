"""Feishu collaboration adapter for ActionCommands, Feishu cards, and OpenAPI calls."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.domain.clock import Clock, SystemClock
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
    format_deadline,
)
from orgpilot.feishu.client import FeishuClient, MockFeishuClient

logger = logging.getLogger(__name__)


class FeishuCollaborationAdapter(CollaborationAdapter):
    """Collaboration adapter for Feishu OpenAPI, cards, messages, and task updates."""

    def __init__(
        self,
        client: FeishuClient | None = None,
        project_id: str = "feishu-project",
        clock: Clock | None = None,
        task_writeback: bool = False,
    ) -> None:
        self.client: FeishuClient = client or MockFeishuClient()
        self.project_id = project_id
        self.clock: Clock = clock or SystemClock()
        # Tasks imported from team.yaml live only in OrgPilot, so their ids are not
        # Feishu task guids; patching Feishu Task with them fails. Write-back is opt-in
        # for projects whose task ids really are Feishu task guids.
        self.task_writeback = task_writeback
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

    def send_private_message(self, command: ActionCommand) -> ActionResult:
        """Sends a private inquiry card or direct clarification message to team members."""
        clarification_text = command.payload.get("clarification_text")
        inquiry_text = command.payload.get("inquiry_text")

        if clarification_text or inquiry_text:
            msg_text = clarification_text or inquiry_text
            try:
                for target in command.targets:
                    self._run_async(
                        self.client.send_message(
                            receive_id=target, msg_type="text", content={"text": msg_text}
                        )
                    )
            except Exception as exc:
                # A probe or clarification that never reached Feishu must surface as a
                # failed command, not as a silently "successful" send.
                logger.warning(
                    "Feishu send_message to %s failed: %s", target, str(exc) or repr(exc)
                )
                result = ActionResult(
                    command_id=command.command_id,
                    action_id=command.action_id,
                    status=CommandStatus.FAILED,
                    output={"sent_to": command.targets, "text": msg_text},
                    error=f"send_message to {target} failed: {exc}",
                    executed_at=command.created_at,
                )
                self.audit_log.append((command, result))
                return result
            result = ActionResult(
                command_id=command.command_id,
                action_id=command.action_id,
                status=CommandStatus.SUCCESS,
                output={"sent_to": command.targets, "text": msg_text},
                executed_at=command.created_at,
            )
            self.audit_log.append((command, result))
            return result

        task_id = command.payload.get("task_id", "unknown_task")
        title = command.payload.get("title", "")
        reason = command.payload.get("reason", "检测到排期延误或阻塞风险")

        card = build_inquiry_card(task_id=task_id, title=title, reason=reason)

        for target in command.targets:
            self._run_async(self.client.send_card(receive_id=target, card=card))

        result = ActionResult(
            command_id=command.command_id,
            action_id=command.action_id,
            status=CommandStatus.SUCCESS,
            output={"sent_to": command.targets, "card": card},
            executed_at=command.created_at,
        )
        self.audit_log.append((command, result))
        return result

    def request_approval(self, command: ActionCommand, approver_id: str) -> ActionResult:
        """Sends an interactive approval card with [Approve] and [Reject] action buttons."""
        approval_id = command.payload.get("approval_id", f"appr:{command.command_id}")
        case_id = command.payload.get("case_id", "case:unknown")
        task_id = command.payload.get("task_id", "unknown_task")
        task_title = command.payload.get("task_title", task_id)
        proposed_deadline = format_deadline(command.payload.get("new_deadline"))
        impacted_tasks = command.payload.get("impacted_tasks", [])
        risk_level = command.payload.get("risk_level", "HIGH")

        card = build_approval_card(
            approval_id=approval_id,
            case_id=case_id,
            task_id=task_id,
            task_title=task_title,
            proposed_deadline_str=str(proposed_deadline),
            impacted_tasks=impacted_tasks,
            risk_level=risk_level,
        )

        self._run_async(self.client.send_card(receive_id=approver_id, card=card))

        result = ActionResult(
            command_id=command.command_id,
            action_id=command.action_id,
            status=CommandStatus.SUCCESS,
            output={"approver_id": approver_id, "card": card},
            executed_at=command.created_at,
        )
        self.audit_log.append((command, result))
        return result

    def update_task(self, command: ActionCommand) -> ActionResult:
        """Updates the task deadline in OrgPilot, and in Feishu Task when write-back is on.

        A failed Feishu call is reported as a FAILED result and leaves OrgPilot's
        deadline unchanged, so the two never silently disagree.
        """
        task_id = command.payload.get("task_id", "task")
        deadline_str = command.payload.get("new_deadline")
        if not deadline_str:
            # Never invent a deadline: falling back to "now" used to reschedule the
            # task to the moment of approval.
            result = ActionResult(
                command_id=command.command_id,
                action_id=command.action_id,
                status=CommandStatus.FAILED,
                output={"task_id": task_id},
                error="update_task payload has no new_deadline",
                executed_at=command.created_at,
            )
            self.audit_log.append((command, result))
            return result
        deadline_dt = datetime.fromisoformat(str(deadline_str))

        if self.task_writeback:
            try:
                self._run_async(
                    self.client.update_task_deadline(task_guid=task_id, deadline=deadline_dt)
                )
            except Exception as exc:
                logger.warning(
                    "Feishu update_task_deadline for %s failed: %s", task_id, str(exc) or repr(exc)
                )
                result = ActionResult(
                    command_id=command.command_id,
                    action_id=command.action_id,
                    status=CommandStatus.FAILED,
                    output={"task_id": task_id, "deadline": deadline_dt.isoformat()},
                    error=f"update_task_deadline for {task_id} failed: {exc}",
                    executed_at=command.created_at,
                )
                self.audit_log.append((command, result))
                return result

        # Generate local projection event
        now = self.clock.now()
        evt = TaskUpdatedEvent(
            project_id=self.project_id,
            event_id=f"evt-feishu-task-upd-{command.command_id}",
            event_type="task.updated",
            source=EventSource.TASK,
            source_ref=f"command:{command.command_id}",
            occurred_at=now,
            received_at=now,
            payload=TaskUpdatedPayload(
                task_id=task_id,
                deadline=deadline_dt,
            ),
        )
        self._generated_events.append(evt)

        result = ActionResult(
            command_id=command.command_id,
            action_id=command.action_id,
            status=CommandStatus.SUCCESS,
            output={
                "task_id": task_id,
                "deadline": deadline_dt.isoformat(),
                "feishu_task_synced": self.task_writeback,
            },
            executed_at=command.created_at,
        )
        self.audit_log.append((command, result))
        return result

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

        result = ActionResult(
            command_id=command.command_id,
            action_id=command.action_id,
            status=CommandStatus.SUCCESS,
            output={"sent_to": command.targets, "card": card},
            executed_at=command.created_at,
        )
        self.audit_log.append((command, result))
        return result

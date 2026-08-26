"""Feishu collaboration adapter for ActionCommands, Feishu cards, and OpenAPI calls."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from orgpilot.adapter.base import CollaborationAdapter
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
    build_inquiry_card,
    build_notification_card,
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
        """Helper to run async client calls inside sync adapter interface."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # In an active loop, schedule task in background
            return asyncio.ensure_future(coro)
        else:
            return asyncio.run(coro)

    def send_private_message(self, command: ActionCommand) -> ActionResult:
        """Sends a private inquiry card to the target team members."""
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
        proposed_deadline = command.payload.get("proposed_deadline", "未指定")
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
        """Calls Feishu Task OpenAPI to update task deadline and generates a TaskUpdatedEvent."""
        task_id = command.payload.get("task_id", "task")
        deadline_str = command.payload.get("deadline")
        deadline_dt = (
            datetime.fromisoformat(deadline_str)
            if isinstance(deadline_str, str)
            else (deadline_str or datetime.now(UTC))
        )

        self._run_async(self.client.update_task_deadline(task_guid=task_id, deadline=deadline_dt))

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
                task_id=task_id,
                deadline=deadline_dt,
            ),
        )
        self._generated_events.append(evt)

        result = ActionResult(
            command_id=command.command_id,
            action_id=command.action_id,
            status=CommandStatus.SUCCESS,
            output={"task_id": task_id, "deadline": deadline_dt.isoformat()},
            executed_at=command.created_at,
        )
        self.audit_log.append((command, result))
        return result

    def notify_group(self, command: ActionCommand) -> ActionResult:
        """Sends a broadcast notification card to the target group or members."""
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

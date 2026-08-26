"""Abstract collaboration adapter interface."""

from abc import ABC, abstractmethod

from orgpilot.domain.enums import ActionType
from orgpilot.domain.models import ActionCommand, ActionResult


class CollaborationAdapter(ABC):
    """Abstract interface for collaboration platforms (Feishu, Slack, Mock, etc.)."""

    @abstractmethod
    def send_private_message(self, command: ActionCommand) -> ActionResult:
        """Sends a private inquiry to target members."""

    @abstractmethod
    def request_approval(self, command: ActionCommand, approver_id: str) -> ActionResult:
        """Requests human approval for a high-impact action or proposal."""

    @abstractmethod
    def update_task(self, command: ActionCommand) -> ActionResult:
        """Updates task properties (requires prior approval)."""

    @abstractmethod
    def notify_group(self, command: ActionCommand) -> ActionResult:
        """Sends a notification to a group/channel."""

    def execute(self, command: ActionCommand) -> ActionResult:
        """Dispatches an action command to the appropriate adapter method."""
        match command.action_type:
            case ActionType.ASK_RECOVERY_ESTIMATE | ActionType.ASK_CLARIFICATION:
                return self.send_private_message(command)
            case ActionType.PROPOSE_RESCHEDULE:
                approver = command.targets[0] if command.targets else "approver"
                return self.request_approval(command, approver)
            case ActionType.UPDATE_TASK:
                return self.update_task(command)
            case ActionType.NOTIFY_GROUP:
                return self.notify_group(command)
            case _:
                raise ValueError(f"Unsupported action type: {command.action_type}")

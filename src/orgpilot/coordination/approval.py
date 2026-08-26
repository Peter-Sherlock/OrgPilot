"""Human approval state machine and token management."""

from datetime import datetime

from orgpilot.domain.enums import ApprovalStatus
from orgpilot.domain.models import ActionCommand, ApprovalRequest, CoordinationAction


class ApprovalManager:
    """Manages approval lifecycles, expiration, rejection, and single-use consumption."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    def create_request(
        self,
        case_id: str,
        action: CoordinationAction,
        proposed_command: ActionCommand,
        approver_id: str,
        current_time: datetime,
        expires_at: datetime | None = None,
    ) -> ApprovalRequest:
        approval_id = f"approval:{case_id}:{action.action_id}"
        request = ApprovalRequest(
            approval_id=approval_id,
            case_id=case_id,
            action_id=action.action_id,
            action_type=action.action_type,
            approver_id=approver_id,
            proposed_command=proposed_command,
            status=ApprovalStatus.PENDING,
            expires_at=expires_at,
        )
        self._requests[approval_id] = request
        return request

    def approve(
        self,
        approval_id: str,
        approver_id: str,
        current_time: datetime,
    ) -> ApprovalRequest:
        request = self._requests.get(approval_id)
        if request is None:
            raise KeyError(f"Approval request {approval_id!r} not found")

        if request.expires_at is not None and current_time > request.expires_at:
            request.status = ApprovalStatus.EXPIRED
            raise ValueError(f"Approval request {approval_id!r} has expired")

        if request.status is ApprovalStatus.REJECTED:
            raise ValueError(f"Cannot approve already rejected request {approval_id!r}")

        request.status = ApprovalStatus.APPROVED
        request.approver_id = approver_id
        request.approved_at = current_time
        return request

    def reject(
        self,
        approval_id: str,
        approver_id: str,
        reason: str,
        current_time: datetime,
    ) -> ApprovalRequest:
        request = self._requests.get(approval_id)
        if request is None:
            raise KeyError(f"Approval request {approval_id!r} not found")

        request.status = ApprovalStatus.REJECTED
        request.approver_id = approver_id
        request.rejection_reason = reason
        return request

    def consume(self, approval_id: str, current_time: datetime) -> ActionCommand:
        request = self._requests.get(approval_id)
        if request is None:
            raise KeyError(f"Approval request {approval_id!r} not found")

        if request.status is not ApprovalStatus.APPROVED:
            raise ValueError(
                f"Approval {approval_id!r} is not approved (status: {request.status.value})"
            )

        if request.consumed:
            raise ValueError(f"Approval {approval_id!r} has already been consumed")

        if request.expires_at is not None and current_time > request.expires_at:
            request.status = ApprovalStatus.EXPIRED
            raise ValueError(f"Approval {approval_id!r} has expired")

        request.consumed = True
        request.consumed_at = current_time

        command = request.proposed_command.model_copy(update={"approved_by": request.approver_id})
        return command

    def get_request(self, approval_id: str) -> ApprovalRequest | None:
        return self._requests.get(approval_id)

    def get_pending_requests(self) -> tuple[ApprovalRequest, ...]:
        return tuple(req for req in self._requests.values() if req.status is ApprovalStatus.PENDING)

    def get_requests_for_case(self, case_id: str) -> tuple[ApprovalRequest, ...]:
        return tuple(req for req in self._requests.values() if req.case_id == case_id)

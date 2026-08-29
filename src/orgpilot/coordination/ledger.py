"""Persistent Coordination Case Ledger and reconciliation lifecycle engine."""

from collections import defaultdict
from datetime import datetime

from orgpilot.domain.enums import (
    ClaimStatus,
    CoordinationCaseStatus,
    HealthStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import (
    ActionCommand,
    CoordinationAction,
    CoordinationCase,
    DependencyImpact,
    OrgState,
    TaskHealthClaim,
)

_HEALTH_SEVERITY = {
    HealthStatus.UNKNOWN: 0,
    HealthStatus.ON_TRACK: 1,
    HealthStatus.AT_RISK: 2,
    HealthStatus.DELAYED: 3,
}

_ACTIVE_STATUSES = {
    CoordinationCaseStatus.OPEN,
    CoordinationCaseStatus.WAITING_FOR_RESPONSE,
    CoordinationCaseStatus.WAITING_FOR_APPROVAL,
}


class CaseLedger:
    """Maintains case lifecycles, action history, timeout detection, and state reconciliation."""

    def __init__(self) -> None:
        self._cases: dict[str, CoordinationCase] = {}

    def get_case(self, case_id: str) -> CoordinationCase | None:
        return self._cases.get(case_id)

    def get_all_cases(self) -> tuple[CoordinationCase, ...]:
        return tuple(sorted(self._cases.values(), key=lambda c: c.case_id))

    def get_active_cases(self) -> tuple[CoordinationCase, ...]:
        return tuple(
            sorted(
                (c for c in self._cases.values() if c.status in _ACTIVE_STATUSES),
                key=lambda c: c.case_id,
            )
        )

    def is_action_duplicate(self, case_id: str, action: CoordinationAction) -> bool:
        case = self._cases.get(case_id)
        if case is None:
            return False
        for cmd in case.executed_commands:
            if cmd.action_type == action.action_type and cmd.targets == action.targets:
                return True
        return False

    def record_command(self, case_id: str, command: ActionCommand, current_time: datetime) -> None:
        case = self._cases.get(case_id)
        if case is None:
            raise KeyError(f"Case {case_id!r} not found")
        case.executed_commands = (*case.executed_commands, command)
        case.updated_at = current_time

    def transition(
        self,
        case_id: str,
        new_status: CoordinationCaseStatus,
        current_time: datetime,
        waiting_for: str | None = None,
        waiting_until: datetime | None = None,
        terminal_reason: str | None = None,
        missing_information: tuple[str, ...] | None = None,
        candidate_actions: tuple[CoordinationAction, ...] | None = None,
    ) -> CoordinationCase:
        case = self._cases.get(case_id)
        if case is None:
            raise KeyError(f"Case {case_id!r} not found")

        case.status = new_status
        case.updated_at = current_time
        if waiting_for is not None or new_status not in {
            CoordinationCaseStatus.WAITING_FOR_RESPONSE,
            CoordinationCaseStatus.WAITING_FOR_APPROVAL,
        }:
            case.waiting_for = waiting_for
        if waiting_until is not None or new_status not in {
            CoordinationCaseStatus.WAITING_FOR_RESPONSE,
            CoordinationCaseStatus.WAITING_FOR_APPROVAL,
        }:
            case.waiting_until = waiting_until
        if terminal_reason is not None:
            case.terminal_reason = terminal_reason
        if missing_information is not None:
            case.missing_information = missing_information
        if candidate_actions is not None:
            case.candidate_actions = candidate_actions

        return case

    def reconcile(
        self,
        state: OrgState,
        impacts: tuple[DependencyImpact, ...],
        current_time: datetime,
        max_response_rounds: int = 1,
    ) -> tuple[CoordinationCase, ...]:
        """Reconciles existing cases against current state and creates new cases as needed."""
        impacted_by_source: dict[str, list[str]] = defaultdict(list)
        for impact in impacts:
            impacted_by_source[impact.source_task_id].append(impact.impacted_task_id)

        for case in list(self._cases.values()):
            if case.status not in _ACTIVE_STATUSES:
                continue

            task = state.tasks.get(case.source_task_id)
            if task is None:
                case.status = CoordinationCaseStatus.CANCELLED
                case.terminal_reason = "task deleted"
                case.updated_at = current_time
                continue

            # Check if task is healthy or done
            is_healthy = task.workflow_status is WorkflowStatus.DONE or (
                task.workflow_status is not WorkflowStatus.BLOCKED
                and task.health_status in {HealthStatus.ON_TRACK, HealthStatus.UNKNOWN}
                and not task.health_conflict
                and not self._active_risk_claims(state, task.task_id)
            )

            if is_healthy:
                if case.status in {
                    CoordinationCaseStatus.WAITING_FOR_RESPONSE,
                    CoordinationCaseStatus.WAITING_FOR_APPROVAL,
                }:
                    case.status = CoordinationCaseStatus.CANCELLED
                    case.terminal_reason = "task recovered while waiting"
                else:
                    case.status = CoordinationCaseStatus.RESOLVED
                    case.terminal_reason = "task healthy or completed"
                case.waiting_for = None
                case.waiting_until = None
                case.candidate_actions = ()
                case.updated_at = current_time
                continue

            # Task is still at risk, update downstream impacts
            case.impacted_task_ids = tuple(sorted(impacted_by_source[task.task_id]))

            active_claims = self._active_risk_claims(state, task.task_id)
            case.evidence_claim_ids = tuple(claim.claim_id for claim in active_claims)

            # Check if missing information was supplied
            has_estimate = any(claim.expected_completion is not None for claim in active_claims)
            if has_estimate and "expected_completion" in case.missing_information:
                case.missing_information = ()
                if case.status is CoordinationCaseStatus.WAITING_FOR_RESPONSE:
                    case.status = CoordinationCaseStatus.RESOLVED
                    case.terminal_reason = "recovery estimate received"
                    case.waiting_for = None
                    case.waiting_until = None
                    case.candidate_actions = ()
                    case.updated_at = current_time
                    continue

            # Check for timeout when waiting for response or approval
            if (
                case.status is CoordinationCaseStatus.WAITING_FOR_RESPONSE
                and case.waiting_until is not None
                and current_time > case.waiting_until
            ):
                case.round_count += 1
                if case.round_count >= max_response_rounds:
                    case.status = CoordinationCaseStatus.ESCALATED
                    case.terminal_reason = "member response timeout exceeded retry threshold"
                    case.waiting_for = None
                    case.waiting_until = None
                    case.candidate_actions = ()
                    case.updated_at = current_time

        # Identify newly risky tasks
        for task in sorted(state.tasks.values(), key=lambda item: item.task_id):
            if not self._is_risky(task.workflow_status, task.health_status):
                continue

            case_id = f"case:task-health:{task.task_id}"
            existing = self._cases.get(case_id)
            if existing is None:
                active_claims = self._active_risk_claims(state, task.task_id)
                evidence_ids = tuple(claim.claim_id for claim in active_claims)
                missing_info: tuple[str, ...] = ()
                if active_claims and all(
                    claim.expected_completion is None for claim in active_claims
                ):
                    missing_info = ("expected_completion",)

                new_case = CoordinationCase(
                    case_id=case_id,
                    source_task_id=task.task_id,
                    status=CoordinationCaseStatus.OPEN,
                    evidence_claim_ids=evidence_ids,
                    impacted_task_ids=tuple(sorted(impacted_by_source[task.task_id])),
                    missing_information=missing_info,
                    candidate_actions=(),
                    created_at=current_time,
                    updated_at=current_time,
                )
                self._cases[case_id] = new_case

        return self.get_all_cases()

    @staticmethod
    def _is_risky(workflow_status: WorkflowStatus, health_status: HealthStatus) -> bool:
        return workflow_status is WorkflowStatus.BLOCKED or health_status in {
            HealthStatus.AT_RISK,
            HealthStatus.DELAYED,
        }

    @staticmethod
    def _active_risk_claims(state: OrgState, task_id: str) -> list[TaskHealthClaim]:
        return sorted(
            (
                claim
                for claim in state.health_claims.values()
                if claim.task_id == task_id
                and claim.status is ClaimStatus.ACTIVE
                and claim.health_status in {HealthStatus.AT_RISK, HealthStatus.DELAYED}
            ),
            key=lambda claim: (claim.occurred_at, claim.claim_id),
        )

"""Build source-backed coordination cases from current state and graph impacts."""

from collections import defaultdict

from orgpilot.domain.enums import (
    ActionType,
    ClaimStatus,
    CoordinationCaseStatus,
    HealthStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import (
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


class CoordinationService:
    """Creates explainable open cases; it does not execute external actions."""

    def build_cases(
        self, state: OrgState, impacts: tuple[DependencyImpact, ...]
    ) -> tuple[CoordinationCase, ...]:
        impacted_by_source: dict[str, list[str]] = defaultdict(list)
        for impact in impacts:
            impacted_by_source[impact.source_task_id].append(impact.impacted_task_id)

        cases: list[CoordinationCase] = []
        for task in sorted(state.tasks.values(), key=lambda item: item.task_id):
            if not self._is_risky(task.workflow_status, task.health_status):
                continue

            active_claims = self._active_risk_claims(state, task.task_id)
            evidence_ids = tuple(claim.claim_id for claim in active_claims)
            missing_information: tuple[str, ...] = ()
            actions: tuple[CoordinationAction, ...] = ()

            if active_claims and all(claim.expected_completion is None for claim in active_claims):
                missing_information = ("expected_completion",)
                primary_claim = max(
                    active_claims,
                    key=lambda claim: (
                        _HEALTH_SEVERITY[claim.health_status],
                        claim.occurred_at,
                        claim.claim_id,
                    ),
                )
                target = primary_claim.stated_by or task.owner_id
                actions = (
                    CoordinationAction(
                        action_id=f"action:{task.task_id}:ask_recovery_estimate",
                        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
                        targets=(target,),
                        reason_refs=(primary_claim.source_event_id,),
                        expected_effect="obtain a source-backed recovery estimate",
                    ),
                )

            cases.append(
                CoordinationCase(
                    case_id=f"case:task-health:{task.task_id}",
                    source_task_id=task.task_id,
                    status=CoordinationCaseStatus.OPEN,
                    evidence_claim_ids=evidence_ids,
                    impacted_task_ids=tuple(sorted(impacted_by_source[task.task_id])),
                    missing_information=missing_information,
                    candidate_actions=actions,
                )
            )

        return tuple(cases)

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

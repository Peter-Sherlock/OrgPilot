"""Build source-backed coordination cases and candidate actions from current state."""

from collections import defaultdict
from collections.abc import Iterable

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
    TaskState,
)

_HEALTH_SEVERITY = {
    HealthStatus.UNKNOWN: 0,
    HealthStatus.ON_TRACK: 1,
    HealthStatus.AT_RISK: 2,
    HealthStatus.DELAYED: 3,
}


class CoordinationService:
    """Creates explainable open cases and plans candidate actions based on evidence."""

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
            elif active_claims:
                # Active claims have expected completion; check for schedule conflicts
                primary_claim = max(
                    active_claims,
                    key=lambda claim: (
                        _HEALTH_SEVERITY[claim.health_status],
                        claim.occurred_at,
                        claim.claim_id,
                    ),
                )
                if (
                    task.deadline is not None
                    and primary_claim.expected_completion is not None
                    and primary_claim.expected_completion > task.deadline
                ):
                    pm_targets = self._find_pm_targets(state) or (task.owner_id,)
                    exp_iso = primary_claim.expected_completion.isoformat()
                    actions = (
                        CoordinationAction(
                            action_id=f"action:{task.task_id}:propose_reschedule",
                            action_type=ActionType.PROPOSE_RESCHEDULE,
                            targets=pm_targets,
                            reason_refs=(primary_claim.source_event_id,),
                            expected_effect=f"propose new deadline {exp_iso}",
                            payload=_reschedule_payload(
                                task, exp_iso, impacted_by_source[task.task_id], state
                            ),
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

    def plan_actions_for_case(
        self, case: CoordinationCase, state: OrgState
    ) -> tuple[CoordinationAction, ...]:
        task = state.tasks.get(case.source_task_id)
        if task is None or not self._is_risky(task.workflow_status, task.health_status):
            return ()

        active_claims = self._active_risk_claims(state, task.task_id)
        if not active_claims:
            return ()

        primary_claim = max(
            active_claims,
            key=lambda claim: (
                _HEALTH_SEVERITY[claim.health_status],
                claim.occurred_at,
                claim.claim_id,
            ),
        )

        if case.missing_information and "expected_completion" in case.missing_information:
            target = primary_claim.stated_by or task.owner_id
            return (
                CoordinationAction(
                    action_id=f"action:{task.task_id}:ask_recovery_estimate",
                    action_type=ActionType.ASK_RECOVERY_ESTIMATE,
                    targets=(target,),
                    reason_refs=(primary_claim.source_event_id,),
                    expected_effect="obtain a source-backed recovery estimate",
                    payload={"task_id": task.task_id, "question": "When can this task recover?"},
                ),
            )

        if (
            task.deadline is not None
            and primary_claim.expected_completion is not None
            and primary_claim.expected_completion > task.deadline
        ):
            pm_targets = self._find_pm_targets(state) or (task.owner_id,)
            exp_iso = primary_claim.expected_completion.isoformat()
            return (
                CoordinationAction(
                    action_id=f"action:{task.task_id}:propose_reschedule",
                    action_type=ActionType.PROPOSE_RESCHEDULE,
                    targets=pm_targets,
                    reason_refs=(primary_claim.source_event_id,),
                    expected_effect=f"propose new deadline {exp_iso}",
                    payload=_reschedule_payload(task, exp_iso, case.impacted_task_ids, state),
                ),
            )

        return ()

    @staticmethod
    def _find_pm_targets(state: OrgState) -> tuple[str, ...]:
        # Only "pm" is privileged (ADR-0009 §7): the same role that may start a
        # team-wide sync receives reschedule approvals. Other roles are labels.
        return tuple(
            member.member_id
            for member in sorted(state.members.values(), key=lambda m: m.member_id)
            if member.role == "pm"
        )

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


def _reschedule_payload(
    task: TaskState, new_deadline: str, impacted_task_ids: Iterable[str], state: OrgState
) -> dict[str, object]:
    """The reschedule proposal every adapter reads: ``new_deadline`` is the canonical key.

    Title and impacted-task titles ride along so an approval card can be read
    without a second lookup.
    """
    return {
        "task_id": task.task_id,
        "task_title": task.title,
        "new_deadline": new_deadline,
        "impacted_tasks": [
            state.tasks[task_id].title if task_id in state.tasks else task_id
            for task_id in sorted(impacted_task_ids)
        ],
    }

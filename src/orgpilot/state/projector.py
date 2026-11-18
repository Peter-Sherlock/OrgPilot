"""Deterministic projection from organization events to current state."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from orgpilot.domain.enums import (
    ClaimStatus,
    CommitmentStatus,
    DependencyWaitReleaseReason,
    DependencyWaitStatus,
    HealthStatus,
    WorkflowStatus,
)
from orgpilot.domain.errors import DomainInvariantError
from orgpilot.domain.models import (
    Commitment,
    DependencyWait,
    MemberState,
    OrgState,
    TaskHealthClaim,
    TaskState,
)
from orgpilot.events.models import (
    CommitmentMadeEvent,
    CommitmentSupersededEvent,
    DependencyDeliveryEstimatedEvent,
    DependencyWaitCancelledEvent,
    DependencyWaitReportedEvent,
    MemberRegisteredEvent,
    OrgEvent,
    TaskCreatedEvent,
    TaskHealthReportedEvent,
    TaskUpdatedEvent,
    TaskWorkflowChangedEvent,
)

_HEALTH_SEVERITY = {
    HealthStatus.UNKNOWN: 0,
    HealthStatus.ON_TRACK: 1,
    HealthStatus.AT_RISK: 2,
    HealthStatus.DELAYED: 3,
}

# Invalidate derived caches when event projection semantics change.
PROJECTION_VERSION = 3


class OrgProjector:
    """Applies validated events without erasing provenance or conflicting claims."""

    def __init__(self, project_id: str) -> None:
        self.state = OrgState(project_id=project_id)

    def apply(self, event: OrgEvent) -> bool:
        if event.project_id != self.state.project_id:
            raise DomainInvariantError(
                f"event project {event.project_id!r} does not match {self.state.project_id!r}"
            )
        if event.event_id in self.state.processed_event_ids:
            return False

        match event:
            case MemberRegisteredEvent():
                self._register_member(event)
            case TaskCreatedEvent():
                self._create_task(event)
            case TaskWorkflowChangedEvent():
                self._change_workflow(event)
            case TaskUpdatedEvent():
                self._update_task(event)
            case TaskHealthReportedEvent():
                self._report_health(event)
            case CommitmentMadeEvent():
                self._make_commitment(event)
            case CommitmentSupersededEvent():
                self._supersede_commitment(event)
            case DependencyWaitReportedEvent():
                self._report_dependency_wait(event)
            case DependencyDeliveryEstimatedEvent():
                self._estimate_dependency_delivery(event)
            case DependencyWaitCancelledEvent():
                self._cancel_dependency_wait(event)

        self.state.processed_event_ids.add(event.event_id)
        self.state.last_event_id = event.event_id
        return True

    def _register_member(self, event: MemberRegisteredEvent) -> None:
        member_id = event.payload.member_id
        if member_id in self.state.members:
            raise DomainInvariantError(f"member {member_id!r} already exists")
        self.state.members[member_id] = MemberState(
            member_id=member_id,
            display_name=event.payload.display_name,
            role=event.payload.role,
            source_event_id=event.event_id,
            last_update_at=event.occurred_at,
        )

    def _create_task(self, event: TaskCreatedEvent) -> None:
        payload = event.payload
        if payload.task_id in self.state.tasks:
            raise DomainInvariantError(f"task {payload.task_id!r} already exists")
        if payload.owner_id not in self.state.members:
            raise DomainInvariantError(f"unknown task owner {payload.owner_id!r}")
        if payload.task_id in payload.dependencies:
            raise DomainInvariantError(f"task {payload.task_id!r} cannot depend on itself")
        missing = sorted(set(payload.dependencies) - self.state.tasks.keys())
        if missing:
            raise DomainInvariantError(f"unknown task dependencies: {', '.join(missing)}")

        self.state.tasks[payload.task_id] = TaskState(
            task_id=payload.task_id,
            title=payload.title,
            owner_id=payload.owner_id,
            workflow_status=payload.workflow_status,
            deadline=payload.deadline,
            dependencies=payload.dependencies,
            source_event_ids=(event.event_id,),
            last_update_at=event.occurred_at,
        )

    def _change_workflow(self, event: TaskWorkflowChangedEvent) -> None:
        payload = event.payload
        task = self._require_task(payload.task_id)
        if payload.from_status is not None and task.workflow_status != payload.from_status:
            raise DomainInvariantError(
                f"task {payload.task_id!r} expected {payload.from_status}, "
                f"found {task.workflow_status}"
            )

        health_updates: dict[str, Any] = {}
        if payload.to_status is WorkflowStatus.DONE:
            self._retire_risk_claims(payload.task_id, event.event_id)
            statuses = {claim.health_status for claim in self._active_claims(payload.task_id)}
            health_updates = {
                "health_status": max(
                    statuses, key=_HEALTH_SEVERITY.get, default=HealthStatus.UNKNOWN
                ),
                "health_conflict": len(statuses) > 1,
            }
        self.state.tasks[payload.task_id] = task.model_copy(
            update={
                **health_updates,
                "workflow_status": payload.to_status,
                "source_event_ids": (*task.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )
        self._settle_matching_commitments(
            payload.task_id, "workflow_status", payload.to_status, event.occurred_at
        )
        if payload.to_status is WorkflowStatus.DONE:
            self._release_waits_on_completion(payload.task_id, event)

    def _report_dependency_wait(self, event: DependencyWaitReportedEvent) -> None:
        """Opens a wait, or appends evidence when the same active wait is reported again."""
        payload = event.payload
        waiting = self._require_task(payload.waiting_task_id)
        self._require_task(payload.upstream_task_id)
        if event.actor_id is None or event.actor_id != waiting.owner_id:
            raise DomainInvariantError(
                f"only the owner of {waiting.task_id!r} can report that it is waiting"
            )
        if payload.upstream_task_id not in waiting.dependencies:
            raise DomainInvariantError(
                f"{payload.upstream_task_id!r} is not a formal dependency of {waiting.task_id!r}"
            )

        existing = self.state.dependency_waits.get(payload.wait_id)
        if existing is not None:
            if (
                existing.status is DependencyWaitStatus.RELEASED
                or existing.waiting_task_id != payload.waiting_task_id
                or existing.upstream_task_id != payload.upstream_task_id
            ):
                raise DomainInvariantError(f"wait {payload.wait_id!r} cannot take this report")
            self.state.dependency_waits[payload.wait_id] = existing.model_copy(
                update={
                    "evidence_event_ids": (*existing.evidence_event_ids, event.event_id),
                    "updated_at": event.occurred_at,
                }
            )
            return

        upstream = self._require_task(payload.upstream_task_id)
        if WorkflowStatus.DONE in {waiting.workflow_status, upstream.workflow_status}:
            raise DomainInvariantError("a finished task cannot open a dependency wait")
        for wait in self.state.dependency_waits.values():
            if (
                wait.status is not DependencyWaitStatus.RELEASED
                and wait.waiting_task_id == payload.waiting_task_id
                and wait.upstream_task_id == payload.upstream_task_id
            ):
                raise DomainInvariantError(f"wait {wait.wait_id!r} is already active for this pair")
        self.state.dependency_waits[payload.wait_id] = DependencyWait(
            wait_id=payload.wait_id,
            waiting_task_id=payload.waiting_task_id,
            upstream_task_id=payload.upstream_task_id,
            reported_by=event.actor_id,
            quote=payload.quote,
            evidence_event_ids=(event.event_id,),
            opened_at=event.occurred_at,
            updated_at=event.occurred_at,
        )

    def _estimate_dependency_delivery(self, event: DependencyDeliveryEstimatedEvent) -> None:
        """Records the upstream owner's estimate; the wait stays open until delivery."""
        wait = self._require_active_wait(event.payload.wait_id)
        upstream = self._require_task(wait.upstream_task_id)
        if event.actor_id is None or event.actor_id != upstream.owner_id:
            raise DomainInvariantError(
                f"only the owner of {upstream.task_id!r} can estimate its delivery"
            )
        self.state.dependency_waits[wait.wait_id] = wait.model_copy(
            update={
                "status": DependencyWaitStatus.ESTIMATED,
                "estimated_at": event.payload.estimated_at,
                "estimated_by": event.actor_id,
                "estimate_quote": event.payload.quote,
                "evidence_event_ids": (*wait.evidence_event_ids, event.event_id),
                "updated_at": event.occurred_at,
            }
        )

    def _cancel_dependency_wait(self, event: DependencyWaitCancelledEvent) -> None:
        wait = self._require_active_wait(event.payload.wait_id)
        if event.actor_id != wait.reported_by:
            raise DomainInvariantError(f"only {wait.reported_by!r} can cancel {wait.wait_id!r}")
        self._release_wait(wait, DependencyWaitReleaseReason.CANCELLED_BY_WAITER, event)

    def _release_waits_on_completion(
        self, task_id: str, event: TaskWorkflowChangedEvent
    ) -> None:
        for wait in tuple(self.state.dependency_waits.values()):
            if wait.status is DependencyWaitStatus.RELEASED:
                continue
            if wait.upstream_task_id == task_id:
                self._release_wait(wait, DependencyWaitReleaseReason.UPSTREAM_DONE, event)
            elif wait.waiting_task_id == task_id:
                self._release_wait(wait, DependencyWaitReleaseReason.WAITER_DONE, event)

    def _release_wait(
        self,
        wait: DependencyWait,
        reason: DependencyWaitReleaseReason,
        event: OrgEvent,
    ) -> None:
        self.state.dependency_waits[wait.wait_id] = wait.model_copy(
            update={
                "status": DependencyWaitStatus.RELEASED,
                "release_reason": reason,
                "released_by_event_id": event.event_id,
                "evidence_event_ids": (*wait.evidence_event_ids, event.event_id),
                "updated_at": event.occurred_at,
            }
        )

    def _require_active_wait(self, wait_id: str) -> DependencyWait:
        wait = self.state.dependency_waits.get(wait_id)
        if wait is None:
            raise DomainInvariantError(f"unknown dependency wait {wait_id!r}")
        if wait.status is DependencyWaitStatus.RELEASED:
            raise DomainInvariantError(f"dependency wait {wait_id!r} is already released")
        return wait

    def _update_task(self, event: TaskUpdatedEvent) -> None:
        payload = event.payload
        task = self._require_task(payload.task_id)
        updates: dict[str, Any] = {
            "source_event_ids": (*task.source_event_ids, event.event_id),
            "last_update_at": event.occurred_at,
        }
        if payload.deadline is not None:
            updates["deadline"] = payload.deadline
        if payload.title is not None:
            updates["title"] = payload.title
        if payload.owner_id is not None:
            if payload.owner_id not in self.state.members:
                raise DomainInvariantError(f"unknown task owner {payload.owner_id!r}")
            updates["owner_id"] = payload.owner_id

        self.state.tasks[payload.task_id] = task.model_copy(update=updates)

    def _report_health(self, event: TaskHealthReportedEvent) -> None:
        task = self._require_task(event.payload.task_id)
        if event.actor_id is not None and event.actor_id not in self.state.members:
            raise DomainInvariantError(f"unknown event actor {event.actor_id!r}")

        claim_id = f"claim:{event.event_id}:task_health"
        superseded_claims: dict[str, TaskHealthClaim] = {}
        for existing in self._active_claims(event.payload.task_id):
            if existing.stated_by == event.actor_id:
                superseded_claims[existing.claim_id] = existing.model_copy(
                    update={"status": ClaimStatus.SUPERSEDED, "superseded_by": claim_id}
                )

        claim = TaskHealthClaim(
            claim_id=claim_id,
            task_id=event.payload.task_id,
            stated_by=event.actor_id,
            health_status=event.payload.health_status,
            expected_completion=event.payload.expected_completion,
            blocker=event.payload.blocker,
            confidence=event.payload.confidence,
            source_event_id=event.event_id,
            source_ref=event.source_ref,
            occurred_at=event.occurred_at,
        )

        self.state.health_claims.update(superseded_claims)
        self.state.health_claims[claim_id] = claim
        if task.workflow_status is WorkflowStatus.DONE:
            self._retire_risk_claims(task.task_id, event.event_id)
        active_claims = self._active_claims(event.payload.task_id)
        statuses = {item.health_status for item in active_claims}
        resolved_health = max(statuses, key=_HEALTH_SEVERITY.get, default=HealthStatus.UNKNOWN)
        self.state.tasks[task.task_id] = task.model_copy(
            update={
                "health_status": resolved_health,
                "health_conflict": len(statuses) > 1,
                "health_claim_ids": (*task.health_claim_ids, claim_id),
                "source_event_ids": (*task.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )
        self._settle_matching_commitments(
            task.task_id, "health_status", resolved_health, event.occurred_at
        )

    def _retire_risk_claims(self, task_id: str, trigger_event_id: str) -> None:
        """Keep risk history, but a completed task cannot carry active risk.

        ``superseded_by`` references the event that retires the claim: completion,
        or a later report received while the official workflow is still DONE.
        """
        for claim in self._active_claims(task_id):
            if claim.health_status in {HealthStatus.AT_RISK, HealthStatus.DELAYED}:
                self.state.health_claims[claim.claim_id] = claim.model_copy(
                    update={"status": ClaimStatus.SUPERSEDED, "superseded_by": trigger_event_id}
                )

    def _make_commitment(self, event: CommitmentMadeEvent) -> None:
        if event.actor_id is None:
            raise DomainInvariantError("commitment.made requires actor_id")
        if event.actor_id not in self.state.members:
            raise DomainInvariantError(f"unknown commitment actor {event.actor_id!r}")
        if event.payload.target_id not in self.state.tasks:
            raise DomainInvariantError(f"unknown commitment target {event.payload.target_id!r}")
        if event.payload.commitment_id in self.state.commitments:
            raise DomainInvariantError(f"commitment {event.payload.commitment_id!r} already exists")

        self.state.commitments[event.payload.commitment_id] = Commitment(
            commitment_id=event.payload.commitment_id,
            actor_id=event.actor_id,
            target_type=event.payload.target_type,
            target_id=event.payload.target_id,
            predicate=event.payload.predicate,
            expected_value=event.payload.expected_value,
            due_at=event.payload.due_at,
            source_event_id=event.event_id,
            last_update_at=event.occurred_at,
        )

    def _supersede_commitment(self, event: CommitmentSupersededEvent) -> None:
        commitment = self.state.commitments.get(event.payload.commitment_id)
        if commitment is None:
            raise DomainInvariantError(f"unknown commitment {event.payload.commitment_id!r}")
        if commitment.status is not CommitmentStatus.ACTIVE:
            raise DomainInvariantError(
                f"only active commitments can be superseded: {commitment.commitment_id!r}"
            )
        self.state.commitments[commitment.commitment_id] = commitment.model_copy(
            update={
                "status": CommitmentStatus.SUPERSEDED,
                "superseded_by": event.payload.replacement_commitment_id,
                "last_update_at": event.occurred_at,
            }
        )

    def _settle_matching_commitments(
        self, target_id: str, predicate: str, value: object, occurred_at: datetime
    ) -> None:
        """Close the ACTIVE commitments whose predicate this event made true.

        The outcome compares the event's business time with ``due_at``, never the
        wall clock, so replaying the log gives the same statuses. Reaching the
        value at ``due_at`` exactly is on time; after it the promise is BROKEN even
        though the work got done (the task's own state records that). A commitment
        without ``due_at`` cannot be late. Commitments that expire with no matching
        event stay ACTIVE here: noticing that needs a time tick (ADR-0014).
        """
        normalized_value = str(value)
        for commitment_id, commitment in tuple(self.state.commitments.items()):
            if (
                commitment.status is CommitmentStatus.ACTIVE
                and commitment.target_id == target_id
                and commitment.predicate == predicate
                and commitment.expected_value == normalized_value
            ):
                late = commitment.due_at is not None and occurred_at > commitment.due_at
                self.state.commitments[commitment_id] = commitment.model_copy(
                    update={
                        "status": CommitmentStatus.BROKEN if late else CommitmentStatus.FULFILLED,
                        "last_update_at": occurred_at,
                    }
                )

    def _require_task(self, task_id: str) -> TaskState:
        task = self.state.tasks.get(task_id)
        if task is None:
            raise DomainInvariantError(f"unknown task {task_id!r}")
        return task

    def _active_claims(self, task_id: str) -> list[TaskHealthClaim]:
        return [
            claim
            for claim in self.state.health_claims.values()
            if claim.task_id == task_id and claim.status is ClaimStatus.ACTIVE
        ]

    def replay(self, events: Iterable[OrgEvent]) -> OrgState:
        for event in events:
            self.apply(event)
        return self.state

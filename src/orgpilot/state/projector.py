"""Deterministic projection from organization events to current state."""

from collections.abc import Iterable
from typing import Any

from orgpilot.domain.enums import ClaimStatus, CommitmentStatus, DirectiveStatus, HealthStatus
from orgpilot.domain.errors import DomainInvariantError
from orgpilot.domain.models import (
    Commitment,
    DirectiveState,
    MemberState,
    OrgState,
    PendingDirectiveClarification,
    TaskHealthClaim,
    TaskState,
)
from orgpilot.events.models import (
    CommitmentMadeEvent,
    CommitmentSupersededEvent,
    DirectiveAcknowledgedEvent,
    DirectiveClarificationRequestedEvent,
    DirectiveClarificationResolvedEvent,
    DirectiveCompletedEvent,
    DirectiveDeliveredEvent,
    DirectiveDeliveryFailedEvent,
    DirectiveEscalatedEvent,
    DirectiveIssuedEvent,
    DirectiveRemindedEvent,
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
            case DirectiveIssuedEvent():
                self._issue_directive(event)
            case DirectiveAcknowledgedEvent():
                self._acknowledge_directive(event)
            case DirectiveCompletedEvent():
                self._complete_directive(event)
            case DirectiveRemindedEvent():
                self._remind_directive(event)
            case DirectiveEscalatedEvent():
                self._escalate_directive(event)
            case DirectiveDeliveredEvent():
                self._deliver_directive(event)
            case DirectiveDeliveryFailedEvent():
                self._fail_directive_delivery(event)
            case DirectiveClarificationRequestedEvent():
                self._request_directive_clarification(event)
            case DirectiveClarificationResolvedEvent():
                self._resolve_directive_clarification(event)

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

        self.state.tasks[payload.task_id] = task.model_copy(
            update={
                "workflow_status": payload.to_status,
                "source_event_ids": (*task.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )
        self._fulfill_matching_commitments(payload.task_id, "workflow_status", payload.to_status)

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
        active_claims = self._active_claims(event.payload.task_id)
        statuses = {item.health_status for item in active_claims}
        resolved_health = max(statuses, key=_HEALTH_SEVERITY.get)
        self.state.tasks[task.task_id] = task.model_copy(
            update={
                "health_status": resolved_health,
                "health_conflict": len(statuses) > 1,
                "health_claim_ids": (*task.health_claim_ids, claim_id),
                "source_event_ids": (*task.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )
        self._fulfill_matching_commitments(task.task_id, "health_status", resolved_health)

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

    def _fulfill_matching_commitments(self, target_id: str, predicate: str, value: object) -> None:
        normalized_value = str(value)
        for commitment_id, commitment in tuple(self.state.commitments.items()):
            if (
                commitment.status is CommitmentStatus.ACTIVE
                and commitment.target_id == target_id
                and commitment.predicate == predicate
                and commitment.expected_value == normalized_value
            ):
                self.state.commitments[commitment_id] = commitment.model_copy(
                    update={"status": CommitmentStatus.FULFILLED}
                )

    def _require_task(self, task_id: str) -> TaskState:
        task = self.state.tasks.get(task_id)
        if task is None:
            raise DomainInvariantError(f"unknown task {task_id!r}")
        return task

    def _require_directive(self, directive_id: str) -> DirectiveState:
        directive = self.state.directives.get(directive_id)
        if directive is None:
            raise DomainInvariantError(f"unknown directive {directive_id!r}")
        return directive

    def _issue_directive(self, event: DirectiveIssuedEvent) -> None:
        payload = event.payload
        if payload.directive_id in self.state.directives:
            raise DomainInvariantError(f"directive {payload.directive_id!r} already exists")
        if payload.issuer_id not in self.state.members:
            raise DomainInvariantError(f"unknown directive issuer {payload.issuer_id!r}")
        if payload.target_id not in self.state.members:
            raise DomainInvariantError(f"unknown directive target {payload.target_id!r}")
        if payload.task_id is not None:
            self._require_task(payload.task_id)

        self.state.directives[payload.directive_id] = DirectiveState(
            directive_id=payload.directive_id,
            text=payload.text,
            issuer_id=payload.issuer_id,
            target_id=payload.target_id,
            task_id=payload.task_id,
            deadline=payload.deadline,
            status=DirectiveStatus.ISSUED,
            issued_at=event.occurred_at,
            source_event_ids=(event.event_id,),
            last_update_at=event.occurred_at,
        )

    def _acknowledge_directive(self, event: DirectiveAcknowledgedEvent) -> None:
        payload = event.payload
        directive = self._require_directive(payload.directive_id)
        if payload.ack_by not in self.state.members:
            raise DomainInvariantError(f"unknown directive acknowledger {payload.ack_by!r}")
        if directive.status is not DirectiveStatus.ISSUED:
            # Defensive idempotency: a replayed ack after completion must not brick replay.
            return

        self.state.directives[payload.directive_id] = directive.model_copy(
            update={
                "status": DirectiveStatus.ACKNOWLEDGED,
                "acknowledged_at": event.occurred_at,
                "source_event_ids": (*directive.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )

    def _complete_directive(self, event: DirectiveCompletedEvent) -> None:
        payload = event.payload
        directive = self._require_directive(payload.directive_id)
        if payload.completed_by not in self.state.members:
            raise DomainInvariantError(f"unknown directive completer {payload.completed_by!r}")
        if directive.status is DirectiveStatus.COMPLETED:
            return

        self.state.directives[payload.directive_id] = directive.model_copy(
            update={
                "status": DirectiveStatus.COMPLETED,
                "completed_at": event.occurred_at,
                "source_event_ids": (*directive.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )

    def _remind_directive(self, event: DirectiveRemindedEvent) -> None:
        payload = event.payload
        directive = self._require_directive(payload.directive_id)
        if directive.status is not DirectiveStatus.ISSUED:
            return

        self.state.directives[payload.directive_id] = directive.model_copy(
            update={
                "reminder_count": max(directive.reminder_count + 1, payload.reminder_index),
                "source_event_ids": (*directive.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )

    def _escalate_directive(self, event: DirectiveEscalatedEvent) -> None:
        payload = event.payload
        directive = self._require_directive(payload.directive_id)
        if directive.status is not DirectiveStatus.ISSUED or directive.escalated:
            return

        self.state.directives[payload.directive_id] = directive.model_copy(
            update={
                "escalated": True,
                "source_event_ids": (*directive.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )

    def _deliver_directive(self, event: DirectiveDeliveredEvent) -> None:
        directive = self._require_directive(event.payload.directive_id)
        self._validate_directive_delivery_target(directive, event.payload.target_id)
        self.state.directives[event.payload.directive_id] = directive.model_copy(
            update={
                "delivery_status": "delivered",
                "source_event_ids": (*directive.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )

    def _fail_directive_delivery(self, event: DirectiveDeliveryFailedEvent) -> None:
        directive = self._require_directive(event.payload.directive_id)
        self._validate_directive_delivery_target(directive, event.payload.target_id)
        self.state.directives[event.payload.directive_id] = directive.model_copy(
            update={
                "delivery_status": "failed",
                "source_event_ids": (*directive.source_event_ids, event.event_id),
                "last_update_at": event.occurred_at,
            }
        )

    @staticmethod
    def _validate_directive_delivery_target(
        directive: DirectiveState, event_target_id: str
    ) -> None:
        if event_target_id != directive.target_id:
            raise DomainInvariantError(
                f"directive delivery target {event_target_id!r} does not match "
                f"directive target {directive.target_id!r}"
            )

    def _request_directive_clarification(self, event: DirectiveClarificationRequestedEvent) -> None:
        payload = event.payload
        if payload.clarification_id in self.state.pending_directive_clarifications:
            raise DomainInvariantError(
                f"directive clarification {payload.clarification_id!r} already exists"
            )
        if payload.issuer_id not in self.state.members:
            raise DomainInvariantError(f"unknown clarification issuer {payload.issuer_id!r}")
        if payload.task_id is not None:
            self._require_task(payload.task_id)
        self.state.pending_directive_clarifications[payload.clarification_id] = (
            PendingDirectiveClarification(
                clarification_id=payload.clarification_id,
                issuer_id=payload.issuer_id,
                draft_text=payload.draft_text,
                missing_slots=payload.missing_slots,
                targets=payload.targets,
                task_id=payload.task_id,
                time_expr=payload.time_expr,
                source_event_ids=(event.event_id,),
                last_update_at=event.occurred_at,
            )
        )

    def _resolve_directive_clarification(self, event: DirectiveClarificationResolvedEvent) -> None:
        payload = event.payload
        pending = self.state.pending_directive_clarifications.pop(payload.clarification_id, None)
        if pending is None:
            raise DomainInvariantError(
                f"unknown directive clarification {payload.clarification_id!r}"
            )

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

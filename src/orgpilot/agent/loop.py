"""Bounded Coordination Agent Loop with deterministic turn execution and state machine."""

from datetime import datetime, timedelta

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.coordination.approval import ApprovalManager
from orgpilot.coordination.ledger import CaseLedger
from orgpilot.coordination.service import CoordinationService
from orgpilot.dependencies.analyzer import DependencyAnalyzer
from orgpilot.domain.enums import (
    ActionType,
    AgentTerminationReason,
    ApprovalStatus,
    CoordinationCaseStatus,
)
from orgpilot.domain.models import (
    ActionCommand,
    AgentTurnTrace,
    OrgState,
)
from orgpilot.events.log import AppendResult, InMemoryEventLog
from orgpilot.events.models import OrgEvent
from orgpilot.policy.engine import PolicyEngine
from orgpilot.state.projector import OrgProjector


class CoordinationAgent:
    """Orchestrates event ingestion, state projection, policy checking, and adapter execution."""

    def __init__(
        self,
        project_id: str,
        adapter: CollaborationAdapter | None = None,
        max_response_timeout_hours: int = 2,
    ) -> None:
        self.project_id = project_id
        self.event_log = InMemoryEventLog()
        self.projector = OrgProjector(project_id=project_id)
        self.dependency_analyzer = DependencyAnalyzer()
        self.coordination_service = CoordinationService()
        self.policy_engine = PolicyEngine()
        self.approval_manager = ApprovalManager()
        self.case_ledger = CaseLedger()
        self.adapter = adapter or MockCollaborationAdapter(project_id=project_id)
        self.max_response_timeout = timedelta(hours=max_response_timeout_hours)
        self._turn_count = 0

    @property
    def state(self) -> OrgState:
        return self.projector.state

    def run_turn(
        self,
        events: list[OrgEvent],
        current_time: datetime,
    ) -> tuple[AgentTurnTrace, list[OrgEvent]]:
        """Executes a single bounded turn in the agent loop."""
        self._turn_count += 1
        ingested_ids: list[str] = []

        for event in events:
            if self.event_log.append(event) is not AppendResult.DUPLICATE:
                self.projector.apply(event)
                ingested_ids.append(event.event_id)

        impacts = self.dependency_analyzer.impacts(self.projector.state.tasks)
        self.case_ledger.reconcile(
            self.projector.state, impacts, current_time, max_response_rounds=1
        )

        active_cases = self.case_ledger.get_active_cases()
        candidate_action_ids: list[str] = []
        policy_decision_ids: list[str] = []
        executed_cmd_ids: list[str] = []

        for case in active_cases:
            if case.status is CoordinationCaseStatus.OPEN:
                actions = self.coordination_service.plan_actions_for_case(
                    case, self.projector.state
                )
                self.case_ledger.transition(
                    case.case_id,
                    case.status,
                    current_time,
                    candidate_actions=actions,
                )

                for action in actions:
                    candidate_action_ids.append(action.action_id)
                    if self.case_ledger.is_action_duplicate(case.case_id, action):
                        continue

                    decision = self.policy_engine.evaluate(action)
                    policy_decision_ids.append(f"decision:{action.action_id}")

                    if action.action_type in {
                        ActionType.ASK_RECOVERY_ESTIMATE,
                        ActionType.ASK_CLARIFICATION,
                    }:
                        command = ActionCommand(
                            command_id=f"cmd:{action.action_id}:r{self._turn_count}",
                            action_id=action.action_id,
                            action_type=action.action_type,
                            targets=action.targets,
                            payload=action.payload,
                            reason_refs=action.reason_refs,
                            created_at=current_time,
                            idempotency_key=f"idem:{action.action_id}:r{self._turn_count}",
                        )
                        self.adapter.execute(command)
                        self.case_ledger.record_command(case.case_id, command, current_time)
                        executed_cmd_ids.append(command.command_id)

                        target_member = action.targets[0] if action.targets else None
                        self.case_ledger.transition(
                            case.case_id,
                            CoordinationCaseStatus.WAITING_FOR_RESPONSE,
                            current_time,
                            waiting_for=target_member,
                            waiting_until=current_time + self.max_response_timeout,
                        )

                    elif (
                        action.action_type
                        in {
                            ActionType.PROPOSE_RESCHEDULE,
                            ActionType.UPDATE_TASK,
                            ActionType.NOTIFY_GROUP,
                        }
                        or decision.requires_approval
                    ):
                        proposed_command = ActionCommand(
                            command_id=f"cmd:{action.action_id}:r{self._turn_count}",
                            action_id=action.action_id,
                            action_type=action.action_type,
                            targets=action.targets,
                            payload=action.payload,
                            reason_refs=action.reason_refs,
                            created_at=current_time,
                            idempotency_key=f"idem:{action.action_id}:r{self._turn_count}",
                        )
                        approver = action.targets[0] if action.targets else "approver"
                        approval_request = self.approval_manager.create_request(
                            case.case_id,
                            action,
                            proposed_command,
                            approver,
                            current_time,
                            expires_at=current_time + timedelta(days=2),
                        )
                        outbound_command = proposed_command.model_copy(
                            update={
                                "payload": {
                                    **proposed_command.payload,
                                    "approval_id": approval_request.approval_id,
                                    "case_id": case.case_id,
                                }
                            }
                        )
                        self.adapter.request_approval(outbound_command, approver)
                        executed_cmd_ids.append(proposed_command.command_id)

                        self.case_ledger.transition(
                            case.case_id,
                            CoordinationCaseStatus.WAITING_FOR_APPROVAL,
                            current_time,
                            waiting_for=approver,
                            waiting_until=current_time + timedelta(days=2),
                        )

            elif case.status is CoordinationCaseStatus.WAITING_FOR_APPROVAL:
                requests = self.approval_manager.get_requests_for_case(case.case_id)
                for req in requests:
                    if req.status is ApprovalStatus.APPROVED and not req.consumed:
                        command = self.approval_manager.consume(req.approval_id, current_time)
                        if req.action_type is ActionType.PROPOSE_RESCHEDULE:
                            update_cmd = ActionCommand(
                                command_id=f"cmd:{case.source_task_id}:update_task:r{self._turn_count}",
                                action_id=f"action:{case.source_task_id}:update_task",
                                action_type=ActionType.UPDATE_TASK,
                                targets=(case.source_task_id,),
                                payload=req.proposed_command.payload,
                                approved_by=req.approver_id,
                                created_at=current_time,
                                idempotency_key=f"idem:update:{case.source_task_id}:r{self._turn_count}",
                            )
                            self.adapter.execute(update_cmd)
                            self.case_ledger.record_command(case.case_id, update_cmd, current_time)
                            executed_cmd_ids.append(update_cmd.command_id)
                            self.case_ledger.transition(
                                case.case_id,
                                CoordinationCaseStatus.RESOLVED,
                                current_time,
                                terminal_reason="reschedule approved and task updated",
                                candidate_actions=(),
                            )
                        elif req.action_type is ActionType.UPDATE_TASK:
                            self.adapter.execute(command)
                            self.case_ledger.record_command(case.case_id, command, current_time)
                            executed_cmd_ids.append(command.command_id)
                            self.case_ledger.transition(
                                case.case_id,
                                CoordinationCaseStatus.RESOLVED,
                                current_time,
                                terminal_reason="task update executed",
                                candidate_actions=(),
                            )
                    elif req.status is ApprovalStatus.REJECTED:
                        self.case_ledger.transition(
                            case.case_id,
                            CoordinationCaseStatus.ESCALATED,
                            current_time,
                            terminal_reason=(
                                f"approval rejected: {req.rejection_reason or 'declined'}"
                            ),
                            candidate_actions=(),
                        )

        # Collect side effects / feedback events from adapter
        generated_events = self.adapter.pop_generated_events()

        # Apply any adapter-generated events (e.g., TaskUpdatedEvent) to internal state immediately
        for gen_evt in generated_events:
            self.event_log.append(gen_evt)
            self.projector.apply(gen_evt)

        termination_reason = self._determine_termination_reason()

        trace = AgentTurnTrace(
            round_number=self._turn_count,
            occurred_at=current_time,
            ingested_event_ids=tuple(ingested_ids),
            active_case_ids=tuple(c.case_id for c in self.case_ledger.get_active_cases()),
            candidate_action_ids=tuple(candidate_action_ids),
            policy_decision_ids=tuple(policy_decision_ids),
            executed_command_ids=tuple(executed_cmd_ids),
            generated_event_ids=tuple(e.event_id for e in generated_events),
            termination_reason=termination_reason,
        )

        return trace, generated_events

    def _determine_termination_reason(self) -> AgentTerminationReason:
        all_cases = self.case_ledger.get_all_cases()
        if not all_cases:
            return AgentTerminationReason.NO_ACTION

        active_cases = self.case_ledger.get_active_cases()
        if not active_cases:
            if any(c.status is CoordinationCaseStatus.ESCALATED for c in all_cases):
                return AgentTerminationReason.ESCALATED
            if all(
                c.status in {CoordinationCaseStatus.RESOLVED, CoordinationCaseStatus.CANCELLED}
                for c in all_cases
            ):
                return AgentTerminationReason.ALL_RESOLVED
            return AgentTerminationReason.ALL_RESOLVED

        for c in active_cases:
            if c.status is CoordinationCaseStatus.WAITING_FOR_RESPONSE:
                return AgentTerminationReason.WAITING_RESPONSE
            if c.status is CoordinationCaseStatus.WAITING_FOR_APPROVAL:
                return AgentTerminationReason.WAITING_APPROVAL

        return AgentTerminationReason.NO_ACTION

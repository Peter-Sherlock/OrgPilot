"""Single public replay path used by the CLI and automated tests."""

from orgpilot.agent.loop import CoordinationAgent
from orgpilot.coordination.service import CoordinationService
from orgpilot.dependencies.analyzer import DependencyAnalyzer
from orgpilot.domain.enums import ApprovalStatus
from orgpilot.domain.models import AgentExecutionTrace, AgentTurnTrace
from orgpilot.events.log import AppendResult, InMemoryEventLog
from orgpilot.policy.engine import PolicyEngine
from orgpilot.scenarios.models import ReplayResult, ScenarioDefinition
from orgpilot.state.projector import OrgProjector


class ScenarioRunner:
    def __init__(self) -> None:
        self._dependency_analyzer = DependencyAnalyzer()
        self._coordination_service = CoordinationService()
        self._policy_engine = PolicyEngine()

    def run(self, scenario: ScenarioDefinition) -> ReplayResult:
        if scenario.rounds:
            return self._run_interactive(scenario)
        return self._run_static(scenario)

    def _run_static(self, scenario: ScenarioDefinition) -> ReplayResult:
        event_log = InMemoryEventLog()
        projector = OrgProjector(project_id=scenario.project_id)
        duplicate_count = 0

        for event in scenario.events:
            append_result = event_log.append(event)
            if append_result is AppendResult.DUPLICATE:
                duplicate_count += 1
                continue
            projector.apply(event)

        impacts = self._dependency_analyzer.impacts(projector.state.tasks)
        cases = self._coordination_service.build_cases(projector.state, impacts)
        decisions = tuple(
            self._policy_engine.evaluate(action)
            for case in cases
            for action in case.candidate_actions
        )

        return ReplayResult(
            scenario_id=scenario.scenario_id,
            state=projector.state,
            impacts=impacts,
            cases=cases,
            policy_decisions=decisions,
            event_count=len(event_log),
            duplicate_event_count=duplicate_count,
        )

    def _run_interactive(self, scenario: ScenarioDefinition) -> ReplayResult:
        agent = CoordinationAgent(project_id=scenario.project_id)
        turn_traces: list[AgentTurnTrace] = []

        for round_item in scenario.rounds:
            # Apply configured approval actions before running the turn
            for appr in round_item.approvals:
                pending_requests = agent.approval_manager.get_pending_requests()
                for req in pending_requests:
                    if appr.approval_id and req.approval_id != appr.approval_id:
                        continue
                    if appr.decision is ApprovalStatus.APPROVED:
                        agent.approval_manager.approve(
                            req.approval_id, appr.approver_id, round_item.current_time
                        )
                    elif appr.decision is ApprovalStatus.REJECTED:
                        agent.approval_manager.reject(
                            req.approval_id,
                            appr.approver_id,
                            appr.reason or "declined",
                            round_item.current_time,
                        )

            turn_trace, generated_events = agent.run_turn(
                list(round_item.events), round_item.current_time
            )
            turn_traces.append(turn_trace)

            # If the adapter generated events during this turn (e.g. from task updates),
            # apply them into the agent immediately.
            if generated_events:
                agent.run_turn(generated_events, round_item.current_time)

        final_termination = (
            turn_traces[-1].termination_reason
            if turn_traces and turn_traces[-1].termination_reason
            else agent._determine_termination_reason()
        )
        agent_trace = AgentExecutionTrace(
            scenario_id=scenario.scenario_id,
            turns=tuple(turn_traces),
            final_termination_reason=final_termination,
            final_cases=agent.case_ledger.get_all_cases(),
        )

        impacts = self._dependency_analyzer.impacts(agent.projector.state.tasks)
        all_cases = agent.case_ledger.get_all_cases()
        all_decisions = tuple(
            self._policy_engine.evaluate(action)
            for case in all_cases
            for action in case.candidate_actions
        )

        return ReplayResult(
            scenario_id=scenario.scenario_id,
            state=agent.projector.state,
            impacts=impacts,
            cases=all_cases,
            policy_decisions=all_decisions,
            event_count=len(agent.event_log),
            duplicate_event_count=0,
            agent_trace=agent_trace,
        )

"""Single public replay path used by the CLI and automated tests."""

from orgpilot.coordination import CoordinationService
from orgpilot.dependencies import DependencyAnalyzer
from orgpilot.events import AppendResult, InMemoryEventLog
from orgpilot.policy import PolicyEngine
from orgpilot.scenarios.models import ReplayResult, ScenarioDefinition
from orgpilot.state import OrgProjector


class ScenarioRunner:
    def __init__(self) -> None:
        self._dependency_analyzer = DependencyAnalyzer()
        self._coordination_service = CoordinationService()
        self._policy_engine = PolicyEngine()

    def run(self, scenario: ScenarioDefinition) -> ReplayResult:
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

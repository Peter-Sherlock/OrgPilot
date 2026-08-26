"""Programmatic comparison between replay results and declared ground truth."""

from typing import Any

from orgpilot.domain.errors import GroundTruthMismatch
from orgpilot.scenarios.models import (
    AssertionResult,
    ExpectedAction,
    ExpectedCase,
    ExpectedClaim,
    ExpectedCommitment,
    ExpectedImpact,
    ExpectedTaskState,
    GroundTruth,
    GroundTruthReport,
    InteractiveGroundTruth,
    ReplayResult,
    ScenarioDefinition,
)


def evaluate_ground_truth(result: ReplayResult, truth: GroundTruth) -> GroundTruthReport:
    assertions: list[AssertionResult] = []

    def check(name: str, expected: Any, actual: Any) -> None:
        assertions.append(
            AssertionResult(
                name=name,
                passed=expected == actual,
                expected=expected,
                actual=actual,
            )
        )

    check("event_count", truth.event_count, result.event_count)
    check("member_count", truth.member_count, len(result.state.members))
    check("task_count", truth.task_count, len(result.state.tasks))

    actual_tasks = {
        task_id: ExpectedTaskState(
            workflow_status=task.workflow_status,
            health_status=task.health_status,
            health_conflict=task.health_conflict,
        )
        for task_id, task in result.state.tasks.items()
        if task_id in truth.tasks
    }
    check("tasks", truth.tasks, actual_tasks)

    actual_impacts = tuple(
        ExpectedImpact(
            source_task_id=item.source_task_id,
            impacted_task_id=item.impacted_task_id,
            path=item.path,
        )
        for item in result.impacts
    )
    check("impacts", truth.impacts, actual_impacts)

    actual_cases = tuple(
        ExpectedCase(
            source_task_id=case.source_task_id,
            status=case.status,
            impacted_task_ids=case.impacted_task_ids,
            missing_information=case.missing_information,
            action_types=tuple(action.action_type for action in case.candidate_actions),
        )
        for case in result.cases
    )
    check("open_cases", truth.open_cases, actual_cases)

    decision_by_action_id = {decision.action_id: decision for decision in result.policy_decisions}
    actual_actions = tuple(
        ExpectedAction(
            action_type=action.action_type,
            targets=action.targets,
            disposition=decision_by_action_id[action.action_id].disposition,
            requires_approval=decision_by_action_id[action.action_id].requires_approval,
        )
        for case in result.cases
        for action in case.candidate_actions
        if action.action_id in decision_by_action_id
    )
    check("expected_actions", truth.expected_actions, actual_actions)

    actual_claims = {
        claim_id: ExpectedClaim(status=claim.status, health_status=claim.health_status)
        for claim_id, claim in result.state.health_claims.items()
        if claim_id in truth.claims
    }
    check("claims", truth.claims, actual_claims)

    actual_commitments = {
        commitment_id: ExpectedCommitment(status=commitment.status)
        for commitment_id, commitment in result.state.commitments.items()
        if commitment_id in truth.commitments
    }
    check("commitments", truth.commitments, actual_commitments)

    return GroundTruthReport(
        scenario_id=result.scenario_id,
        passed=all(item.passed for item in assertions),
        assertions=tuple(assertions),
    )


def evaluate_interactive_ground_truth(
    result: ReplayResult, truth: InteractiveGroundTruth
) -> GroundTruthReport:
    assertions: list[AssertionResult] = []

    def check(name: str, expected: Any, actual: Any) -> None:
        assertions.append(
            AssertionResult(
                name=name,
                passed=expected == actual,
                expected=expected,
                actual=actual,
            )
        )

    if result.agent_trace is not None:
        check("total_rounds", truth.total_rounds, len(result.agent_trace.turns))
        check(
            "final_termination_reason",
            truth.final_termination_reason,
            result.agent_trace.final_termination_reason,
        )

    actual_cases = {case.case_id: case.status for case in result.cases}
    for case_id, expected_status in truth.final_cases.items():
        check(f"case_status:{case_id}", expected_status, actual_cases.get(case_id))

    actual_reasons = {case.case_id: (case.terminal_reason or "") for case in result.cases}
    for case_id, expected_reason_keyword in truth.case_terminal_reasons.items():
        actual_reason = actual_reasons.get(case_id, "")
        check(
            f"case_terminal_reason:{case_id}",
            True,
            expected_reason_keyword.lower() in actual_reason.lower(),
        )

    if truth.tasks:
        actual_tasks = {}
        for task_id, exp_task in truth.tasks.items():
            task = result.state.tasks.get(task_id)
            if task is not None:
                actual_tasks[task_id] = ExpectedTaskState(
                    workflow_status=task.workflow_status,
                    health_status=task.health_status,
                    health_conflict=task.health_conflict,
                    deadline=task.deadline if exp_task.deadline is not None else None,
                )
            check(f"task:{task_id}", exp_task, actual_tasks.get(task_id))

    return GroundTruthReport(
        scenario_id=result.scenario_id,
        passed=all(item.passed for item in assertions),
        assertions=tuple(assertions),
    )


def evaluate_scenario(scenario: ScenarioDefinition, result: ReplayResult) -> GroundTruthReport:
    if scenario.interactive_ground_truth is not None:
        return evaluate_interactive_ground_truth(result, scenario.interactive_ground_truth)
    if scenario.ground_truth is not None:
        return evaluate_ground_truth(result, scenario.ground_truth)
    raise ValueError(f"Scenario {scenario.scenario_id!r} has no declared ground truth")


def assert_ground_truth(result: ReplayResult, truth: GroundTruth) -> None:
    report = evaluate_ground_truth(result, truth)
    if report.passed:
        return
    failures = "\n".join(
        f"- {item.name}: expected={item.expected!r}, actual={item.actual!r}"
        for item in report.assertions
        if not item.passed
    )
    raise GroundTruthMismatch(f"scenario {report.scenario_id!r} failed:\n{failures}")


def assert_scenario(scenario: ScenarioDefinition, result: ReplayResult) -> None:
    report = evaluate_scenario(scenario, result)
    if report.passed:
        return
    failures = "\n".join(
        f"- {item.name}: expected={item.expected!r}, actual={item.actual!r}"
        for item in report.assertions
        if not item.passed
    )
    raise GroundTruthMismatch(f"scenario {report.scenario_id!r} failed:\n{failures}")

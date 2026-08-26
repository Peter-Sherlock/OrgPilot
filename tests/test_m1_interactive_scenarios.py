"""Deterministic evaluation of all M1 multi-round interactive coordination scenarios."""

from pathlib import Path

import pytest

from orgpilot.scenarios.evaluator import assert_scenario, evaluate_scenario
from orgpilot.scenarios.loader import load_scenario
from orgpilot.scenarios.runner import ScenarioRunner

M1_SCENARIOS = [
    "evals/scenarios/m1_01_delay_inquiry_and_recovery.yaml",
    "evals/scenarios/m1_02_delay_reschedule_approved.yaml",
    "evals/scenarios/m1_03_pm_rejects_reschedule.yaml",
    "evals/scenarios/m1_04_recovery_cancels_pending_inquiry.yaml",
    "evals/scenarios/m1_05_unresponsive_member_escalates.yaml",
]


@pytest.mark.parametrize("scenario_path", M1_SCENARIOS)
def test_m1_interactive_scenario_passes_ground_truth(scenario_path: str) -> None:
    scenario = load_scenario(Path(scenario_path))
    runner = ScenarioRunner()
    result = runner.run(scenario)
    report = evaluate_scenario(scenario, result)
    assert report.passed, f"Scenario {scenario.scenario_id} failed assertions: {report.assertions}"
    assert_scenario(scenario, result)


@pytest.mark.parametrize("scenario_path", M1_SCENARIOS)
def test_m1_scenario_replay_is_100_percent_deterministic(scenario_path: str) -> None:
    scenario = load_scenario(Path(scenario_path))
    runner = ScenarioRunner()

    result_run1 = runner.run(scenario)
    result_run2 = runner.run(scenario)

    assert result_run1.event_count == result_run2.event_count
    assert len(result_run1.cases) == len(result_run2.cases)
    assert result_run1.agent_trace == result_run2.agent_trace
    assert result_run1.state.model_dump() == result_run2.state.model_dump()

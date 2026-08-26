"""The four YAML scenarios are executable specifications, not prose examples."""

from pathlib import Path

import pytest

from orgpilot.scenarios.evaluator import assert_ground_truth, evaluate_ground_truth
from orgpilot.scenarios.loader import discover_scenarios, load_scenario
from orgpilot.scenarios.runner import ScenarioRunner

SCENARIO_PATHS = discover_scenarios(Path("evals/scenarios"))


@pytest.mark.parametrize("scenario_path", SCENARIO_PATHS, ids=lambda path: path.stem)
def test_ground_truth_scenario(scenario_path: Path) -> None:
    scenario = load_scenario(scenario_path)
    result = ScenarioRunner().run(scenario)

    assert_ground_truth(result, scenario.ground_truth)
    assert evaluate_ground_truth(result, scenario.ground_truth).passed


@pytest.mark.parametrize("scenario_path", SCENARIO_PATHS, ids=lambda path: path.stem)
def test_replay_is_deterministic(scenario_path: Path) -> None:
    scenario = load_scenario(scenario_path)

    first = ScenarioRunner().run(scenario)
    second = ScenarioRunner().run(scenario)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_exactly_four_p0_scenarios_are_discoverable() -> None:
    assert len(SCENARIO_PATHS) == 4

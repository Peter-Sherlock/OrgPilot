"""Ground-truth scenario loading, replay, and evaluation."""

from orgpilot.scenarios.evaluator import (
    assert_ground_truth,
    assert_scenario,
    evaluate_ground_truth,
    evaluate_interactive_ground_truth,
    evaluate_scenario,
)
from orgpilot.scenarios.loader import discover_scenarios, load_scenario
from orgpilot.scenarios.runner import ScenarioRunner

__all__ = [
    "ScenarioRunner",
    "assert_ground_truth",
    "assert_scenario",
    "discover_scenarios",
    "evaluate_ground_truth",
    "evaluate_interactive_ground_truth",
    "evaluate_scenario",
    "load_scenario",
]

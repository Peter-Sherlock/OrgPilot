"""Command-line replay interface for local development and evaluation."""

import argparse
from pathlib import Path

from orgpilot.scenarios.evaluator import evaluate_ground_truth
from orgpilot.scenarios.loader import discover_scenarios, load_scenario
from orgpilot.scenarios.runner import ScenarioRunner


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orgpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    replay = subparsers.add_parser("replay", help="replay ground-truth scenarios")
    replay.add_argument("path", nargs="?", type=Path, help="one scenario YAML file")
    replay.add_argument("--all", action="store_true", help="run evals/scenarios/*.yaml")
    return parser


def _replay(paths: tuple[Path, ...]) -> int:
    runner = ScenarioRunner()
    failed = False
    for path in paths:
        scenario = load_scenario(path)
        result = runner.run(scenario)
        report = evaluate_ground_truth(result, scenario.ground_truth)
        status = "PASS" if report.passed else "FAIL"
        print(
            f"[{status}] {scenario.scenario_id}: "
            f"events={result.event_count} impacts={len(result.impacts)} "
            f"cases={len(result.cases)} actions={len(result.policy_decisions)}"
        )
        for assertion in report.assertions:
            if not assertion.passed:
                failed = True
                print(
                    f"  - {assertion.name}: expected={assertion.expected!r} "
                    f"actual={assertion.actual!r}"
                )
    return 1 if failed else 0


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "replay":
        if args.all and args.path is not None:
            raise SystemExit("choose a scenario path or --all, not both")
        if args.all:
            paths = discover_scenarios(Path("evals/scenarios"))
        elif args.path is not None:
            paths = (args.path,)
        else:
            raise SystemExit("provide a scenario path or --all")
        if not paths:
            raise SystemExit("no scenario files found")
        return _replay(paths)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

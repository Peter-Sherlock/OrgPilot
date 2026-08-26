"""Command-line replay and evaluation interface for local development."""

import argparse
from pathlib import Path

from orgpilot.extraction.evaluator import evaluate_extractor, load_gold_dataset
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.scenarios.evaluator import evaluate_scenario
from orgpilot.scenarios.loader import discover_scenarios, load_scenario
from orgpilot.scenarios.runner import ScenarioRunner


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orgpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay", help="replay ground-truth scenarios")
    replay.add_argument("path", nargs="?", type=Path, help="one scenario YAML file")
    replay.add_argument("--all", action="store_true", help="run evals/scenarios/*.yaml")

    extract_eval = subparsers.add_parser(
        "eval-extraction", help="evaluate natural language claim extraction metrics"
    )
    extract_eval.add_argument(
        "--dataset",
        type=Path,
        default=Path("evals/extraction/gold_dataset.yaml"),
        help="path to gold dataset YAML",
    )

    return parser


def _replay(paths: tuple[Path, ...]) -> int:
    runner = ScenarioRunner()
    failed = False
    for path in paths:
        scenario = load_scenario(path)
        result = runner.run(scenario)
        report = evaluate_scenario(scenario, result)
        status = "PASS" if report.passed else "FAIL"
        extra = ""
        if result.agent_trace:
            extra = (
                f" rounds={len(result.agent_trace.turns)} "
                f"termination={result.agent_trace.final_termination_reason.value}"
            )
        print(
            f"[{status}] {scenario.scenario_id}: "
            f"events={result.event_count} impacts={len(result.impacts)} "
            f"cases={len(result.cases)} actions={len(result.policy_decisions)}{extra}"
        )
        for assertion in report.assertions:
            if not assertion.passed:
                failed = True
                print(
                    f"  - {assertion.name}: expected={assertion.expected!r} "
                    f"actual={assertion.actual!r}"
                )
    return 1 if failed else 0


def _eval_extraction(dataset_path: Path) -> int:
    if not dataset_path.exists():
        raise SystemExit(f"dataset file not found: {dataset_path}")
    samples = load_gold_dataset(dataset_path)
    extractor = ClaimExtractor()
    metrics = evaluate_extractor(extractor, samples)
    status = "PASS" if metrics.passed else "FAIL"
    print(f"[{status}] Natural Language Extraction Benchmark ({metrics.total_samples} samples):")
    print(
        f"  - Health Status Precision: {metrics.health_status_precision:.2%}, "
        f"Recall: {metrics.health_status_recall:.2%}, F1: {metrics.health_status_f1:.2%}"
    )
    print(f"  - Task ID Accuracy: {metrics.task_id_accuracy:.2%}")
    print(f"  - Slot DateTime Accuracy: {metrics.slot_datetime_accuracy:.2%}")
    print(f"  - False Alarm Rate: {metrics.false_alarm_rate:.2%}")
    print(f"  - Grounding Valid Rate: {metrics.grounding_valid_rate:.2%}")
    return 0 if metrics.passed else 1


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
    if args.command == "eval-extraction":
        return _eval_extraction(args.dataset)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

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
    extract_eval.add_argument(
        "--provider",
        choices=["mock", "aihubmix"],
        default="mock",
        help="mock = offline deterministic regression; aihubmix = live model benchmark",
    )

    ws_parser = subparsers.add_parser(
        "start-feishu-ws", help="start Feishu WebSocket long connection listener"
    )
    ws_parser.add_argument(
        "--project-id",
        type=str,
        default=None,
        help="OrgPilot project ID (defaults to env ORGPILOT_FEISHU_PROJECT_ID)",
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


def _eval_extraction(dataset_path: Path, provider: str = "mock") -> int:
    if not dataset_path.exists():
        raise SystemExit(f"dataset file not found: {dataset_path}")
    samples = load_gold_dataset(dataset_path)

    live_client = None
    if provider == "aihubmix":
        from orgpilot.config import OrgPilotSettings
        from orgpilot.extraction.client import AnthropicCompatibleLLMClient

        settings = OrgPilotSettings.from_env()
        if not settings.aihubmix_api_key:
            raise SystemExit("AIHUBMIX_API_KEY is required for --provider aihubmix")
        live_client = AnthropicCompatibleLLMClient(
            api_key=settings.aihubmix_api_key,
            base_url=settings.aihubmix_base_url,
            model=settings.aihubmix_model,
            reasoning_effort=settings.llm_reasoning_effort,
        )
        print(
            f"[*] Live benchmark against {settings.aihubmix_model} "
            "via configured Anthropic-compatible endpoint..."
        )
    extractor = ClaimExtractor(llm_client=live_client)
    try:
        metrics = evaluate_extractor(extractor, samples)
    finally:
        if live_client is not None:
            live_client.close()
    label = f"live ({settings.aihubmix_model})" if provider == "aihubmix" else provider
    status = "PASS" if metrics.passed else "FAIL"
    print(
        f"[{status}] Natural Language Extraction Benchmark "
        f"({label}, {metrics.total_samples} samples):"
    )
    print(
        f"  - Health Status Precision: {metrics.health_status_precision:.2%}, "
        f"Recall: {metrics.health_status_recall:.2%}, F1: {metrics.health_status_f1:.2%}"
    )
    print(f"  - Task ID Accuracy: {metrics.task_id_accuracy:.2%}")
    print(f"  - Slot DateTime Accuracy: {metrics.slot_datetime_accuracy:.2%}")
    print(f"  - False Alarm Rate: {metrics.false_alarm_rate:.2%}")
    print(f"  - Grounding Valid Rate: {metrics.grounding_valid_rate:.2%}")
    print(f"  - Intent Accuracy: {metrics.intent_accuracy:.2%}")
    return 0 if metrics.passed else 1


def _start_feishu_ws(project_id: str | None = None) -> int:
    import asyncio
    import time

    from orgpilot.config import OrgPilotSettings
    from orgpilot.feishu.ws import FeishuWebSocketListener
    from orgpilot.gateway.service import GatewayService
    from orgpilot.storage.database import Database

    settings = OrgPilotSettings.from_env()
    app_id = settings.feishu_app_id
    app_secret = settings.feishu_app_secret
    if not app_id or not app_secret:
        raise SystemExit(
            "Error: FEISHU_APP_ID and FEISHU_APP_SECRET environment variables are required."
        )

    target_project = project_id or settings.feishu_project_id
    db = Database(settings.database_url)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db.init_db())

    service = GatewayService(db)
    listener = FeishuWebSocketListener(
        app_id=app_id,
        app_secret=app_secret,
        gateway_service=service,
        project_id=target_project,
        loop=loop,
        demo_bootstrap=settings.demo_bootstrap,
    )
    print(f"[*] Starting Feishu WebSocket Listener for project '{target_project}'...")
    print(f"[*] App ID: {app_id}")
    print("[*] WebSocket long-connection active. Listening for Feishu messages and card actions...")
    print("[*] Press Ctrl+C to stop.")

    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Stopping Feishu WebSocket Listener...")
        return 0


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
        return _eval_extraction(args.dataset, args.provider)
    if args.command == "start-feishu-ws":
        return _start_feishu_ws(args.project_id)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

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

    preflight = subparsers.add_parser(
        "feishu-preflight",
        help="validate live Feishu configuration without sending messages or updating tasks",
    )
    preflight.add_argument(
        "--online-auth",
        action="store_true",
        help="also request a tenant token; this authenticates but performs no Feishu write",
    )

    l2_smoke = subparsers.add_parser(
        "feishu-l2-smoke",
        help="run one challenge-bound inbound message and at most one text reply",
    )
    l2_smoke.add_argument("--challenge", help="exact challenge; defaults to a fresh random token")
    l2_smoke.add_argument("--project-id", required=True, help="fresh isolated project ID")
    l2_smoke.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="bounded wait for the exact challenge (30-300 seconds)",
    )
    l2_smoke.add_argument(
        "--confirm-single-reply",
        action="store_true",
        help="required acknowledgement that one real text reply is authorized",
    )

    l3_approval = subparsers.add_parser(
        "feishu-l3-approval", help="one isolated test approval card and one bound decision"
    )
    l3_approval.add_argument("--run-id", help="fresh feishu-l3-... acceptance directory")
    l3_approval.add_argument("--timeout-seconds", type=int, default=300)
    l3_approval.add_argument("--confirm-single-card", action="store_true")
    l3_approval.add_argument(
        "--preview", action="store_true", help="show exact card, no network/files"
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
    from orgpilot.extraction.client import AnthropicCompatibleLLMClient
    from orgpilot.extraction.extractor import ClaimExtractor
    from orgpilot.feishu.runtime import build_feishu_adapter_factory
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
    if not settings.feishu_allow_writes:
        raise SystemExit(
            "Error: Feishu write gate is closed. Run 'orgpilot feishu-preflight' first, "
            "then set ORGPILOT_FEISHU_ALLOW_WRITES=true only for an authorized live test."
        )

    target_project = project_id or settings.feishu_project_id
    db = Database(settings.database_url)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(db.init_db())

    llm_client = None
    if settings.llm_provider == "aihubmix":
        llm_client = AnthropicCompatibleLLMClient(
            api_key=settings.aihubmix_api_key or "",
            base_url=settings.aihubmix_base_url,
            model=settings.aihubmix_model,
            reasoning_effort=settings.llm_reasoning_effort,
        )
    service = GatewayService(
        db,
        extractor=ClaimExtractor(llm_client=llm_client),
        adapter_factory=build_feishu_adapter_factory(settings),
        reference_timezone=settings.reference_timezone,
        directive_reminder_minutes=settings.directive_reminder_minutes,
        directive_escalation_minutes=settings.directive_escalation_minutes,
        outbox_max_attempts=settings.outbox_max_attempts,
        outbox_retry_seconds=settings.outbox_retry_seconds,
    )
    listener = FeishuWebSocketListener(
        app_id=app_id,
        app_secret=app_secret,
        gateway_service=service,
        project_id=target_project,
        loop=loop,
        demo_bootstrap=settings.demo_bootstrap,
    )
    print(f"[*] Starting Feishu WebSocket Listener for project '{target_project}'...")
    print("[*] App credentials: configured (values hidden)")
    print("[*] WebSocket long-connection active. Listening for Feishu messages and card actions...")
    print("[*] Press Ctrl+C to stop.")

    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Stopping Feishu WebSocket Listener...")
        return 0
    finally:
        listener.stop()
        loop.run_until_complete(db.close())
        if llm_client is not None:
            llm_client.close()
        loop.close()


def _feishu_preflight(online_auth: bool = False) -> int:
    import asyncio

    from orgpilot.config import OrgPilotSettings
    from orgpilot.feishu.client import AsyncFeishuClient

    try:
        settings = OrgPilotSettings.from_env()
    except ValueError as exc:
        print(f"[FAIL] configuration: {exc}")
        return 1

    checks = [
        (
            "adapter",
            settings.collaboration_adapter == "feishu",
            "feishu selected" if settings.collaboration_adapter == "feishu" else "not feishu",
        ),
        (
            "credentials",
            bool(settings.feishu_app_id and settings.feishu_app_secret),
            (
                "present (values hidden)"
                if settings.feishu_app_id and settings.feishu_app_secret
                else "missing"
            ),
        ),
        (
            "transport",
            settings.feishu_use_ws or bool(settings.feishu_verification_token),
            (
                "WebSocket"
                if settings.feishu_use_ws
                else (
                    "HTTP Webhook"
                    if settings.feishu_verification_token
                    else "HTTP Webhook verification token missing"
                )
            ),
        ),
        (
            "demo bootstrap",
            not settings.demo_bootstrap,
            "disabled" if not settings.demo_bootstrap else "enabled; disable for live acceptance",
        ),
        (
            "write gate",
            not settings.feishu_allow_writes,
            "closed" if not settings.feishu_allow_writes else "OPEN; close before preflight",
        ),
    ]
    passed = True
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        passed = passed and ok
    if not passed:
        print("[FAIL] Feishu preflight stopped before network authentication.")
        return 1

    if not online_auth:
        print("[SKIP] online auth: use --online-auth for a read-only credential check")
        print("[PASS] Feishu preflight completed with the write gate closed.")
        return 0

    client = AsyncFeishuClient(
        app_id=settings.feishu_app_id or "",
        app_secret=settings.feishu_app_secret or "",
        allow_writes=False,
    )
    try:
        asyncio.run(client.get_tenant_access_token())
    except Exception as exc:
        print(f"[FAIL] online auth: {type(exc).__name__} (details hidden)")
        return 1
    print("[PASS] online auth: tenant token issued (value hidden; no write performed)")
    print("[PASS] Feishu preflight completed with the write gate closed.")
    return 0


def _feishu_l2_smoke(
    challenge: str | None,
    project_id: str,
    timeout_seconds: int,
    confirm_single_reply: bool,
) -> int:
    import json
    import re
    import secrets

    from orgpilot.config import OrgPilotSettings
    from orgpilot.feishu.acceptance import L2Acceptance
    from orgpilot.feishu.client import AsyncFeishuClient
    from orgpilot.feishu.ws import FeishuWebSocketListener

    if not confirm_single_reply:
        raise SystemExit("--confirm-single-reply is required for the real L2 smoke test")
    challenge = challenge or f"ORGPILOT-L2:{secrets.token_urlsafe(18)}"
    if sum(not char.isspace() for char in challenge) < 12:
        raise SystemExit("--challenge must contain at least 12 non-whitespace characters")
    if len(challenge) > 200 or any(ord(char) < 32 for char in challenge):
        raise SystemExit("--challenge must be a single line of at most 200 characters")
    if not 30 <= timeout_seconds <= 300:
        raise SystemExit("--timeout-seconds must be between 30 and 300")
    if not re.fullmatch(r"feishu-l2-[a-zA-Z0-9_-]{1,64}", project_id):
        raise SystemExit("--project-id must start with 'feishu-l2-' and use a safe unique suffix")

    settings = OrgPilotSettings.from_env()
    if settings.collaboration_adapter != "feishu":
        raise SystemExit("ORGPILOT_COLLABORATION_ADAPTER=feishu is required")
    if not settings.feishu_use_ws:
        raise SystemExit("ORGPILOT_FEISHU_USE_WS=true is required for L2")
    if settings.demo_bootstrap:
        raise SystemExit("ORGPILOT_DEMO_BOOTSTRAP must remain false")
    if settings.feishu_allow_writes:
        raise SystemExit(
            "Ambient Feishu write gate must remain closed; L2 opens only its bounded client"
        )

    report_path = Path("var/feishu-acceptance") / f"{project_id}.json"
    client = AsyncFeishuClient(
        app_id=settings.feishu_app_id or "",
        app_secret=settings.feishu_app_secret or "",
        allow_writes=True,
    )
    try:
        acceptance = L2Acceptance(challenge, client, report_path)
    except FileExistsError:
        raise SystemExit("L2 report already exists; choose a fresh project ID") from None
    listener = FeishuWebSocketListener(
        app_id=settings.feishu_app_id or "",
        app_secret=settings.feishu_app_secret or "",
        project_id=project_id,
        acceptance=acceptance,
    )
    print("[*] L2 isolated listener starting (no business DB, LLM or outbox)...", flush=True)
    print("[*] At most one fixed text reply to one private user; no cards/tasks.", flush=True)
    stopped = False
    try:
        listener.start()
        if not listener.wait_until_connected(30.0):
            error_type = listener.connection_error_type or "none"
            print(f"[FAIL] WebSocket not ready (error={error_type}; details hidden).", flush=True)
        else:
            acceptance.arm(timeout_seconds)
            print("[READY] WebSocket connected; send this exact text in private chat:", flush=True)
            print(challenge, flush=True)
            acceptance.wait(timeout_seconds)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"[FAIL] L2 interrupted: {type(exc).__name__} (details hidden).", flush=True)
    finally:
        acceptance.close()
        try:
            stopped = listener.stop()
        except Exception as exc:
            print(f"[FAIL] L2 shutdown: {type(exc).__name__} (details hidden).", flush=True)
        acceptance.record_shutdown(stopped)
    result = acceptance.snapshot()
    print("[AUDIT] " + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    print(f"[*] Local redacted report: {report_path}", flush=True)
    if not stopped or result.get("audit_error"):
        print("[FAIL] Shutdown or audit persistence was not confirmed; do not retry automatically.")
        return 1
    if result["reply_status"] == "api_accepted":
        print(
            "[PASS] One reply accepted by Feishu API; "
            "user-side visibility still needs confirmation."
        )
        return 0
    if result["send_attempts"]:
        print("[FAIL] Reply outcome unknown; reconcile in Feishu before any new attempt.")
    else:
        print("[FAIL] No reply attempted; inspect received_messages and filtered reasons.")
    return 1


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
    if args.command == "feishu-preflight":
        return _feishu_preflight(args.online_auth)
    if args.command == "feishu-l2-smoke":
        return _feishu_l2_smoke(
            args.challenge,
            args.project_id,
            args.timeout_seconds,
            args.confirm_single_reply,
        )
    if args.command == "feishu-l3-approval":
        from orgpilot.feishu.approval_cli import run_approval_probe

        return run_approval_probe(
            args.run_id, args.timeout_seconds, args.confirm_single_card, args.preview
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

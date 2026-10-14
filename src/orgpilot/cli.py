"""Command-line replay and evaluation interface for local development."""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

    intent_eval = subparsers.add_parser(
        "eval-intent", help="run the intent behavior evaluation suite (ADR-0008 §8)"
    )
    intent_eval.add_argument("--split", choices=["dev", "test"], default="dev")
    intent_eval.add_argument("--mode", choices=["mock", "replay", "live"], default="mock")
    intent_eval.add_argument(
        "--system", choices=["baseline", "layered"], default="baseline"
    )
    intent_eval.add_argument("--intent", choices=["heuristic", "oracle"], default="heuristic")
    intent_eval.add_argument(
        "--client", choices=["heuristic", "llm"], default="heuristic",
        help="heuristic keeps the mock pipeline; llm uses the DeepSeek/AIHubMix client (3b)",
    )
    intent_eval.add_argument(
        "--live", action="store_true",
        help="--client llm: send real requests (requires --confirm-spend and the provider key)",
    )
    intent_eval.add_argument(
        "--replay", action="store_true",
        help="--client llm: run offline from recordings (missing recording fails)",
    )
    intent_eval.add_argument(
        "--confirm-spend", action="store_true",
        help="acknowledge that live calls cost money",
    )
    intent_eval.add_argument(
        "--limit", type=int, default=None,
        help="evaluate only the first N samples/conversations",
    )
    intent_eval.add_argument(
        "--max-spend-usd", type=float, default=None,
        help="abort live runs once the estimated cost at official peak prices exceeds this",
    )
    intent_eval.add_argument(
        "--conversations",
        action="store_true",
        help="run the multi-turn conversation scripts (evals/intent/conversations.yaml)",
    )
    intent_eval.add_argument(
        "--dataset-dir", type=Path, default=Path("evals/intent"), help="dataset directory"
    )
    intent_eval.add_argument(
        "--report-dir", type=Path, default=Path("evals/intent/reports"), help="report output dir"
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

    team = subparsers.add_parser("team", help="import or inspect the team roster (ADR-0009)")
    team_commands = team.add_subparsers(dest="team_command", required=True)
    team_import = team_commands.add_parser("import", help="import members and tasks from team.yaml")
    team_import.add_argument("path", type=Path, help="team.yaml file")
    team_import.add_argument(
        "--project", default=None, help="defaults to env ORGPILOT_FEISHU_PROJECT_ID"
    )
    team_import.add_argument(
        "--dry-run", action="store_true", help="print the plan without writing events"
    )
    team_show = team_commands.add_parser("show", help="list the project's members and tasks")
    team_show.add_argument(
        "--project", default=None, help="defaults to env ORGPILOT_FEISHU_PROJECT_ID"
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


def _prepare_llm_client(gate_args: Any) -> Any:
    """Gates and builds the LLM intent client for --client llm runs."""
    from orgpilot.config import OrgPilotSettings
    from orgpilot.intent.llm_client import LLMIntentClient

    if gate_args.live and gate_args.replay:
        raise SystemExit("--live 与 --replay 只能二选一")
    if not gate_args.live and not gate_args.replay:
        raise SystemExit(
            "--client llm 需要显式选择 --live --confirm-spend（真实调用）或 --replay（离线回放）"
        )
    try:
        settings = OrgPilotSettings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if settings.llm_provider == "mock":
        raise SystemExit(
            "--client llm 需要 ORGPILOT_LLM_PROVIDER=aihubmix 或 deepseek（含对应 key）"
        )
    try:
        settings.validate()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if gate_args.live and not gate_args.confirm_spend:
        raise SystemExit("在线运行必须显式传入 --confirm-spend（会产生费用）")
    try:
        return LLMIntentClient.from_settings(
            settings,
            mode="live" if gate_args.live else "replay",
            # Only evaluation runs record; live runs capture responses for
            # free --replay reruns of the same prompt version.
            record=gate_args.live,
            max_spend_usd=gate_args.max_spend_usd,
        )
    except ValueError as exc:
        # includes the unknown-model-price rejection for --max-spend-usd
        raise SystemExit(str(exc)) from exc


def _print_llm_stats(stats: dict[str, Any]) -> None:
    if not stats.get("call_count"):
        return
    print("  - LLM stats:")
    print(
        f"      schema first-pass {stats['first_pass_rate']:.2%}, "
        f"retry {stats['retry_rate']:.2%}, degraded {stats['degraded_rate']:.2%}"
    )
    print(
        f"      latency p50 {stats['latency_p50_ms']}ms / p95 {stats['latency_p95_ms']}ms, "
        f"cache hit {stats['cache_hit_ratio']:.2%}"
    )
    print(
        f"      tokens/message in {stats['avg_input_tokens']} "
        f"(cache {stats['avg_cache_read_tokens']}) out {stats['avg_output_tokens']}"
    )
    if stats.get("total_cost_usd_estimate") is None:
        print("      cost 价格未知（模型不在计价表，费用未估算）")
    else:
        print(
            f"      cost ~${stats['total_cost_usd_estimate']:.4f} "
            f"(${stats['cost_per_message_usd_estimate']:.6f}/msg)"
        )
    if stats["validation_error_types"]:
        print(f"      validation errors: {stats['validation_error_types']}")
    if stats["normalization_totals"]:
        print(f"      normalization: {stats['normalization_totals']}")


def _eval_conversations(
    mode: str,
    dataset_dir: Path,
    report_dir: Path,
    intent: str = "heuristic",
    client: str = "heuristic",
    live: bool = False,
    replay: bool = False,
    confirm_spend: bool = False,
    limit: int | None = None,
    max_spend_usd: float | None = None,
) -> int:
    import asyncio

    from orgpilot.intent.client import HeuristicIntentClient
    from orgpilot.intent.conversation_eval import (
        HEURISTIC_GATE_CONVERSATIONS,
        ConversationSystemUnderTest,
        run_conversation_eval,
    )
    from orgpilot.intent.evaluator import ORACLE_TITLE_SUFFIX, load_intent_project
    from orgpilot.intent.llm_client import SpendLimitExceeded, aggregate_llm_metrics
    from orgpilot.intent.oracle import OracleIntentClient

    if mode != "mock":
        raise SystemExit("eval-intent --conversations 目前仅支持 --mode mock")
    conversations_path = dataset_dir / "conversations.yaml"
    if not conversations_path.exists():
        raise SystemExit(f"conversation file not found: {conversations_path}")

    project = load_intent_project(dataset_dir / "project.yaml")
    title_suffix = None
    llm_stats_provider = None
    llm_client = None
    if intent == "oracle":
        title_suffix = ORACLE_TITLE_SUFFIX
        intent_client_impl = lambda script: OracleIntentClient(  # noqa: E731
            script.turns, allow_duplicate_keys=True
        )
        eval_system = ConversationSystemUnderTest(project, intent_client_impl)
    elif client == "llm":
        gate_args = SimpleNamespace(
            live=live, replay=replay, confirm_spend=confirm_spend, max_spend_usd=max_spend_usd
        )
        llm_client = _prepare_llm_client(gate_args)
        run_mode = "live" if live else "replay"
        title_suffix = f"LLMIntentClient {llm_client.model}（{run_mode}）"
        eval_system = ConversationSystemUnderTest(project, llm_client)
        llm_stats_provider = lambda: aggregate_llm_metrics(llm_client.metrics)  # noqa: E731
    else:
        eval_system = ConversationSystemUnderTest(project, HeuristicIntentClient())

    try:
        passed, total, results = asyncio.run(
            run_conversation_eval(
                conversations_path,
                report_dir,
                project=project,
                intent_client=eval_system._intent_client,  # noqa: SLF001 - CLI wiring
                title_suffix=title_suffix,
                limit=limit,
                llm_stats_provider=llm_stats_provider,
            )
        )
    except SpendLimitExceeded as exc:
        print(f"超出预算中止：{exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"回放失败：{exc}")
        return 1
    print(
        f"Conversation Evaluation (mode={mode}, intent={intent}, client={client}, "
        "oracle gate: all conversations must pass):"
    )
    if title_suffix:
        print(f"  - {title_suffix}")
    print(f"  - Conversations: {passed}/{total} passed")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"  - [{status}] {result.script_id} ({len(result.turn_results)} turns)")
        for issue in result.issues:
            print(f"      · {issue}")
    if client == "llm" and llm_client is not None:
        _print_llm_stats(aggregate_llm_metrics(llm_client.metrics))
    if intent == "oracle" and passed != total:
        print("  - oracle gate violated: all conversations must pass")
        return 1
    if intent == "heuristic" and client == "heuristic" and HEURISTIC_GATE_CONVERSATIONS:
        # 3a.2: heuristic quality is a non-goal except for the one demo
        # conversation — conv_01 must converge with the right completion time.
        gate = [r for r in results if r.script_id in HEURISTIC_GATE_CONVERSATIONS]
        failed = [r.script_id for r in gate if not r.passed]
        print(
            "  - heuristic gate: "
            f"{', '.join(HEURISTIC_GATE_CONVERSATIONS)} must converge "
            "(other conversations are recorded without a gate)"
        )
        if failed:
            print(f"  - heuristic gate violated: {', '.join(failed)} failed")
            return 1
    return 0


def _eval_intent(
    split: str,
    mode: str,
    dataset_dir: Path,
    report_dir: Path,
    system: str = "baseline",
    intent: str = "heuristic",
    conversations: bool = False,
    client: str = "heuristic",
    live: bool = False,
    replay: bool = False,
    confirm_spend: bool = False,
    limit: int | None = None,
    max_spend_usd: float | None = None,
) -> int:
    if conversations:
        return _eval_conversations(
            mode=mode,
            dataset_dir=dataset_dir,
            report_dir=report_dir,
            intent=intent,
            client=client,
            live=live,
            replay=replay,
            confirm_spend=confirm_spend,
            limit=limit,
            max_spend_usd=max_spend_usd,
        )
    import asyncio

    from orgpilot.intent.client import HeuristicIntentClient
    from orgpilot.intent.evaluator import (
        ORACLE_TITLE_SUFFIX,
        LayeredSystemUnderTest,
        load_intent_project,
        load_intent_samples,
        run_intent_eval,
    )
    from orgpilot.intent.llm_client import SpendLimitExceeded, aggregate_llm_metrics
    from orgpilot.intent.metrics import RecordingIntentClient
    from orgpilot.intent.oracle import OracleIntentClient

    if mode != "mock":
        raise SystemExit(
            f"eval-intent --mode {mode} needs the intent layer or an online model; "
            "this step only supports --mode mock (ADR-0008 implementation step 1)"
        )
    if intent == "oracle" and system != "layered":
        raise SystemExit("eval-intent --intent oracle 只能与 --system layered 组合使用")
    if intent == "oracle" and client == "llm":
        raise SystemExit("--client llm 与 --intent oracle 不能组合（oracle 只测流水线）")
    if not (dataset_dir / f"{split}.yaml").exists():
        raise SystemExit(f"dataset file not found: {dataset_dir / f'{split}.yaml'}")

    eval_system = None
    title_suffix = None
    run_mode = mode
    llm_stats_provider = None
    llm_client = None
    intent_recorder = None
    if client == "llm":
        gate_args = SimpleNamespace(
            live=live, replay=replay, confirm_spend=confirm_spend, max_spend_usd=max_spend_usd
        )
        llm_client = _prepare_llm_client(gate_args)
        run_mode = f"llm-{'live' if live else 'replay'}"
        title_suffix = (
            f"LLMIntentClient {llm_client.model}"
            f"（{'live 在线' if live else 'replay 离线回放'}）"
        )
        # §8: wrap (never replace) the client so the classifier output of every
        # sample is recorded for the intent-layer metrics.
        intent_recorder = RecordingIntentClient(llm_client)
        eval_system = LayeredSystemUnderTest(
            load_intent_project(dataset_dir / "project.yaml"), intent_recorder
        )
        llm_stats_provider = lambda: aggregate_llm_metrics(llm_client.metrics)  # noqa: E731
    elif system == "layered":
        if intent == "oracle":
            project = load_intent_project(dataset_dir / "project.yaml")
            samples = load_intent_samples(dataset_dir / f"{split}.yaml")
            intent_recorder = RecordingIntentClient(OracleIntentClient(samples))
            eval_system = LayeredSystemUnderTest(project, intent_recorder)
            title_suffix = ORACLE_TITLE_SUFFIX
        else:
            project = load_intent_project(dataset_dir / "project.yaml")
            eval_system = LayeredSystemUnderTest(project, HeuristicIntentClient())

    run_kwargs: dict[str, object] = {"limit": limit, "llm_stats_provider": llm_stats_provider}
    if eval_system is not None:
        run_kwargs["system"] = eval_system
        run_kwargs["title_suffix"] = title_suffix
    if intent_recorder is not None:
        run_kwargs["intent_recorder"] = intent_recorder

    try:
        metrics, failures = asyncio.run(
            run_intent_eval(
                split=split,
                mode=run_mode,
                dataset_dir=dataset_dir,
                report_dir=report_dir,
                **run_kwargs,
            )
        )
    except SpendLimitExceeded as exc:
        print(f"超出预算中止：{exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"回放失败：{exc}")
        return 1
    print(
        f"Intent Behavior Evaluation (split={split}, mode={run_mode}, system={system}, "
        f"intent={intent}, client={client}, no pass gate this step)"
    )
    if title_suffix:
        print(f"  - {title_suffix}")
    if client == "llm" and llm_client is not None:
        _print_llm_stats(aggregate_llm_metrics(llm_client.metrics))
    print(f"  - Samples: {metrics.total_samples}")
    print(
        f"  - Claim precision: {metrics.claim_precision:.2%}, "
        f"recall: {metrics.claim_recall:.2%}"
    )
    print(
        f"  - Task id accuracy: {metrics.task_id_accuracy:.2%}, "
        f"health accuracy: {metrics.health_status_accuracy:.2%}, "
        f"time accuracy: {metrics.time_accuracy:.2%}"
    )
    print(
        f"  - False claim rate: {metrics.false_claim_rate:.2%}, "
        f"sync false trigger rate: {metrics.sync_false_trigger_rate:.2%}"
    )
    print(
        f"  - Unconfirmed mass sync sends: {metrics.unconfirmed_sync_count} "
        f"/ {metrics.unconfirmed_sync_expected} samples requiring a card (target 0)"
    )
    print(
        f"  - Reply category accuracy: {metrics.reply_category_accuracy:.2%}, "
        f"multi-turn success: {metrics.multi_turn_success_rate:.2%}"
    )
    if metrics.intent_metrics is not None:
        im = metrics.intent_metrics
        macro = "—" if im.macro_f1 is None else f"{im.macro_f1:.2%}"
        answers = "—" if im.answers_pending_accuracy is None else (
            f"{im.answers_pending_accuracy:.2%}"
        )
        print(
            f"  - Intent set exact match: {im.intent_set_exact_match_rate:.2%} "
            f"({im.intent_set_exact_matches}/{im.samples_with_intents}), "
            f"macro-F1: {macro}, answers_pending acc: {answers} "
            f"(report carries the per-intent table and confusion matrix)"
        )
    print(f"  - Failures: {len(failures)} (see report for expected-vs-observed details)")
    return 0


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
        demo_mode=settings.demo_mode,
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


def _team_directory(settings: Any) -> Any:
    """The Feishu contact lookup used to resolve member emails (ADR-0009 §3)."""
    from orgpilot.feishu.client import AsyncFeishuClient

    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise SystemExit(
            "Error: FEISHU_APP_ID and FEISHU_APP_SECRET are required to resolve member emails."
        )
    return AsyncFeishuClient(app_id=settings.feishu_app_id, app_secret=settings.feishu_app_secret)


def _print_team_errors(errors: list[str]) -> None:
    print("导入失败，未写入任何事件：", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)


def _team_import(path: Path, project: str | None, dry_run: bool) -> int:
    import asyncio
    from datetime import UTC, datetime

    from orgpilot.config import OrgPilotSettings
    from orgpilot.gateway.service import GatewayService
    from orgpilot.storage.database import Database
    from orgpilot.team import ChangeKind, TeamImportError, import_team, load_team_spec

    settings = OrgPilotSettings.from_env()
    try:
        spec, digest = load_team_spec(path, settings.business_timezone)
    except TeamImportError as exc:
        _print_team_errors(exc.errors)
        return 1
    target = project or settings.feishu_project_id
    if spec.project != target:
        _print_team_errors(
            [f"team.yaml is for project {spec.project!r}, but the target is {target!r}"]
        )
        return 1
    directory = _team_directory(settings)

    async def run() -> int:
        db = Database(settings.database_url)
        await db.init_db()
        try:
            service = GatewayService(db, business_timezone=settings.business_timezone)
            now = datetime.now(UTC).astimezone(settings.business_timezone)
            result = await import_team(
                service, spec, directory, digest=digest, now=now, dry_run=dry_run
            )
        except TeamImportError as exc:
            _print_team_errors(exc.errors)
            return 1
        finally:
            await db.close()

        plan = result.plan
        print(f"项目 {spec.project} · team-import@{digest}")
        for change in plan.changes:
            print(change.render())
        for warning in plan.warnings:
            print(f"警告：{warning}")
        skipped = sum(1 for c in plan.changes if c.kind is ChangeKind.SKIP)
        if not plan.events:
            print(f"没有需要写入的变更，跳过 {skipped} 项")
        elif result.applied:
            print(f"已写入 {len(plan.events)} 个事件，跳过 {skipped} 项")
        else:
            print(f"将写入 {len(plan.events)} 个事件，跳过 {skipped} 项（dry-run，未写入）")
        return 0

    return asyncio.run(run())


def _team_show(project: str | None) -> int:
    import asyncio

    from orgpilot.config import OrgPilotSettings
    from orgpilot.gateway.service import GatewayService
    from orgpilot.storage.database import Database

    settings = OrgPilotSettings.from_env()
    target = project or settings.feishu_project_id

    async def run() -> int:
        db = Database(settings.database_url)
        await db.init_db()
        try:
            agent = await GatewayService(db).get_or_replay_agent(target)
        finally:
            await db.close()
        state = agent.projector.state
        names = {m.member_id: m.display_name for m in state.members.values()}
        print(f"项目 {target}")
        print(f"成员（{len(state.members)}）")
        for member in sorted(state.members.values(), key=lambda m: (m.role != "pm", m.member_id)):
            print(f"  {member.member_id[:12]}…  {member.display_name}  {member.role}")
        print(f"任务（{len(state.tasks)}）")
        for task in sorted(state.tasks.values(), key=lambda t: t.task_id):
            deadline = (
                task.deadline.astimezone(settings.business_timezone).strftime("%m-%d %H:%M")
                if task.deadline
                else "无"
            )
            print(
                f"  {task.task_id}  {task.title}  {task.workflow_status.value}  "
                f"截止 {deadline}  负责人 {names.get(task.owner_id, task.owner_id)}  "
                f"依赖 {', '.join(task.dependencies) or '-'}"
            )
        return 0

    return asyncio.run(run())


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
    if args.command == "eval-intent":
        return _eval_intent(
            args.split,
            args.mode,
            args.dataset_dir,
            args.report_dir,
            system=args.system,
            intent=args.intent,
            conversations=args.conversations,
            client=args.client,
            live=args.live,
            replay=args.replay,
            confirm_spend=args.confirm_spend,
            limit=args.limit,
            max_spend_usd=args.max_spend_usd,
        )
    if args.command == "start-feishu-ws":
        return _start_feishu_ws(args.project_id)
    if args.command == "team":
        if args.team_command == "import":
            return _team_import(args.path, args.project, args.dry_run)
        return _team_show(args.project)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

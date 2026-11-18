"""R1 dependency wait evaluation (ADR-0016), versioned apart from ``evals/intent``.

Every case runs through the real ``MessageRouter`` with the feature switched on, in its
own in-memory database, and is scored on what the system actually did: which waits exist
afterwards and who was contacted.

Metrics follow section 5 of ``docs/m2-message-driven-plan.md``: upstream localization
(correct / all scenarios), wrong contacts, and duplicate actions.

    # Offline, no model and no credentials
    uv run python -m evals.dependency_wait.runner

    # Offline, from the committed recordings
    uv run python -m evals.dependency_wait.runner --client llm --replay --check

    # Real model calls; needs provider settings in the environment and costs money
    uv run python -m evals.dependency_wait.runner --client llm --live --confirm-spend \
        --max-spend-usd 0.10 --write-results
"""

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.domain.enums import WorkflowStatus
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    TaskCreatedEvent,
    TaskCreatedPayload,
)
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.gateway.service import GatewayService
from orgpilot.intent.client import HeuristicIntentClient, IntentClient, IntentContext
from orgpilot.intent.llm_client import (
    PROMPT_VERSION,
    IntentInfrastructureError,
    LLMIntentClient,
    aggregate_llm_metrics,
)
from orgpilot.intent.models import (
    Certainty,
    IntentItem,
    IntentResult,
    IntentType,
    Polarity,
    SpeechAct,
)
from orgpilot.intent.replies import ReplyKind
from orgpilot.intent.router import MessageRouter
from orgpilot.storage.database import Database

EVAL_DIR = Path(__file__).parent
CASES_PATH = EVAL_DIR / "cases.yaml"
PROJECT_PATH = EVAL_DIR / "project.yaml"
FROZEN_PATH = EVAL_DIR / "FROZEN.json"
RECORDINGS_DIR = EVAL_DIR / "recordings"
RESULTS_DIR = EVAL_DIR / "results"
DEFAULT_MODEL = "deepseek-flash"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
SETUP_LEAD = timedelta(minutes=30)
TURN_GAP = timedelta(minutes=1)


# ------------------------------------------------------------------ dataset


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_project(path: Path = PROJECT_PATH) -> dict[str, Any]:
    return load_yaml(path)


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    return load_yaml(path)["cases"]


def file_sha256(path: Path) -> str:
    """Hash of the text with normalized line endings, so checkouts on any OS agree."""
    text = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(text).hexdigest()


def dataset_fingerprint() -> dict[str, str]:
    return {
        "cases_sha256": file_sha256(CASES_PATH),
        "project_sha256": file_sha256(PROJECT_PATH),
        "prompt_version": PROMPT_VERSION,
    }


def frozen_mismatches() -> list[str]:
    """What differs from the frozen dataset; a missing manifest counts as a difference."""
    if not FROZEN_PATH.exists():
        return [f"缺少冻结清单 {FROZEN_PATH.name}"]
    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    return [
        f"{key}: 冻结值 {frozen.get(key)}，当前值 {value}"
        for key, value in dataset_fingerprint().items()
        if frozen.get(key) != value
    ]


# ---------------------------------------------------------------- observation


@dataclass(frozen=True, order=True)
class Contact:
    """One outbound message to a member; ``subject`` tells repeated actions apart."""

    action: str
    target: str
    subject: str = ""


@dataclass(frozen=True)
class ObservedWait:
    waiting: str
    upstream: str
    status: str
    estimated_at: str | None = None


@dataclass
class Observation:
    waits: list[ObservedWait] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    reply_categories: list[str] = field(default_factory=list)
    reply_texts: list[str] = field(default_factory=list)
    model_items: list[Any] = field(default_factory=list)


class _Adapter(MockCollaborationAdapter):
    """Mock adapter that also exposes a Feishu client, so replies can be observed."""

    def __init__(self, project_id: str) -> None:
        super().__init__(project_id=project_id)
        self.client = MockFeishuClient()


class SetupAwareClient:
    """Answers setup turns from a script, so only measured turns reach the model."""

    def __init__(self, inner: IntentClient) -> None:
        self.inner = inner
        self.script: IntentResult | None = None

    async def classify(self, message: str, context: IntentContext) -> IntentResult:
        if self.script is not None:
            return self.script
        return await self.inner.classify(message, context)


def _seed_events(project: dict[str, Any]) -> list[dict[str, Any]]:
    project_id = project["project_id"]
    anchor = datetime.fromisoformat(project["anchor"]) - timedelta(days=1)

    def common(event_id: str, source: EventSource) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "event_id": event_id,
            "source": source,
            "source_ref": f"setup:{event_id}",
            "occurred_at": anchor,
            "received_at": anchor,
        }

    events: list[Any] = [
        MemberRegisteredEvent(
            event_type="member.registered",
            payload=MemberRegisteredPayload(
                member_id=member["member_id"],
                display_name=member["display_name"],
                role=member["role"],
            ),
            **common(f"evt-member-{member['member_id']}", EventSource.HUMAN),
        )
        for member in project["members"]
    ]
    events += [
        TaskCreatedEvent(
            event_type="task.created",
            payload=TaskCreatedPayload(
                task_id=task["task_id"],
                title=task["title"],
                owner_id=task["owner_id"],
                workflow_status=WorkflowStatus(task["workflow_status"]),
                deadline=datetime.fromisoformat(task["deadline"]),
                dependencies=tuple(task.get("dependencies", ())),
            ),
            **common(f"evt-task-{task['task_id']}", EventSource.TASK),
        )
        for task in project["tasks"]
    ]
    return [event.model_dump(mode="json") for event in events]


def _setup_result(turn: dict[str, Any]) -> IntentResult:
    return IntentResult(
        items=[
            IntentItem(
                intent=IntentType.DEPENDENCY_WAIT,
                task_id=turn["task_id"],
                speech_act=SpeechAct.STATEMENT,
                polarity=Polarity.AFFIRMATIVE,
                certainty=Certainty.CERTAIN,
                source_quote=turn["text"],
                confidence=1.0,
            )
        ]
    )


def _contacts_since(adapter: _Adapter, audit_start: int, state: Any) -> list[Contact]:
    contacts: list[Contact] = []
    for command, _result in adapter.audit_log[audit_start:]:
        wait = state.dependency_waits.get(str(command.payload.get("wait_id", "")))
        subject = (
            f"{wait.waiting_task_id}->{wait.upstream_task_id}"
            if wait is not None
            else str(command.payload.get("task_id", ""))
        )
        contacts += [
            Contact(command.action_type.value, target, subject) for target in command.targets
        ]
    return contacts


async def run_case(
    case: dict[str, Any], project: dict[str, Any], intent_client: IntentClient
) -> Observation:
    """Plays one case against a fresh system and reports what it did."""
    project_id = project["project_id"]
    anchor = datetime.fromisoformat(project["anchor"])
    client = SetupAwareClient(intent_client)
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    adapter = _Adapter(project_id)
    service = GatewayService(db, adapter_factory=lambda _project: adapter)
    await service.ingest_raw_events(project_id, _seed_events(project))
    router = MessageRouter(
        service,
        intent_client=client,
        settings=OrgPilotSettings(dependency_wait_enabled=True),
    )

    for index, turn in enumerate(case.get("setup", [])):
        client.script = _setup_result(turn)
        await router.handle_message(
            project_id=project_id,
            sender_id=turn["sender"],
            message_id=f"om-{case['id']}-setup-{index}",
            text=turn["text"],
            occurred_at=anchor - SETUP_LEAD + TURN_GAP * index,
        )
    client.script = None

    observation = Observation()
    audit_start = len(adapter.audit_log)
    for index, turn in enumerate(case["turns"]):
        sent_start = len(adapter.client.sent_messages)
        cards_start = len(adapter.client.sent_cards)
        outcome = await router.handle_message(
            project_id=project_id,
            sender_id=turn["sender"],
            message_id=turn.get("message_id", f"om-{case['id']}-{index}"),
            text=turn["text"],
            occurred_at=anchor + TURN_GAP * index,
        )
        observation.reply_categories.append(outcome.reply_category.value)
        observation.reply_texts.append(outcome.reply_text or "")
        observation.model_items.append(outcome.data.get("intent_summary", {}))
        # Anything the bot sends to someone other than the sender is a contact too.
        direct = [
            *adapter.client.sent_messages[sent_start:],
            *adapter.client.sent_cards[cards_start:],
        ]
        observation.contacts += [
            Contact("direct_message", record["receive_id"])
            for record in direct
            if record["receive_id"] != turn["sender"]
        ]

    state = (await service.get_or_replay_agent(project_id)).projector.state
    observation.contacts += _contacts_since(adapter, audit_start, state)
    observation.waits = sorted(
        (
            ObservedWait(
                waiting=wait.waiting_task_id,
                upstream=wait.upstream_task_id,
                status=wait.status.value,
                estimated_at=wait.estimated_at.isoformat() if wait.estimated_at else None,
            )
            for wait in state.dependency_waits.values()
        ),
        key=lambda wait: (wait.waiting, wait.upstream, wait.status),
    )
    await db.close()
    return observation


# -------------------------------------------------------------------- scoring


def case_group(case: dict[str, Any]) -> str:
    if case.get("setup"):
        return "existing_wait"
    return "should_open" if case["expected"]["waits"] else "should_not_open"


def score_case(case: dict[str, Any], observation: Observation) -> dict[str, Any]:
    """Compares one observation with the frozen expectation. Pure, so it can be tested."""
    expected = case["expected"]
    wanted = Counter((w["waiting"], w["upstream"], w["status"]) for w in expected["waits"])
    actual = Counter((w.waiting, w.upstream, w.status) for w in observation.waits)
    localized = wanted == actual

    estimate_correct: bool | None = None
    for wait in expected["waits"]:
        if "estimated_at" not in wait:
            continue
        moment = datetime.fromisoformat(wait["estimated_at"])
        estimate_correct = any(
            seen.waiting == wait["waiting"]
            and seen.upstream == wait["upstream"]
            and seen.estimated_at is not None
            and datetime.fromisoformat(seen.estimated_at) == moment
            for seen in observation.waits
        )

    # The same action to the same person about the same wait is a duplicate; whatever
    # remains is compared with the expected contacts as a multiset.
    occurrences = Counter(observation.contacts)
    duplicates = sorted(
        contact for contact, count in occurrences.items() for _ in range(count - 1)
    )
    unique = Counter((contact.action, contact.target) for contact in occurrences)
    expected_contacts = Counter((c["action"], c["target"]) for c in expected["contacts"])
    wrong = sorted((unique - expected_contacts).elements())
    missing = sorted((expected_contacts - unique).elements())
    senders = {turn["sender"] for turn in case["turns"]}

    clarified: bool | None = None
    if expected.get("clarify"):
        clarified = ReplyKind.CLARIFY.value in observation.reply_categories
    fallback = ReplyKind.FALLBACK.value in observation.reply_categories

    reasons: list[str] = []
    if not localized:
        reasons.append(f"等待状态不符：期望 {sorted(wanted)}，实际 {sorted(actual)}")
    if estimate_correct is False:
        reasons.append("预计时间不符")
    reasons += [f"错误联系：{action} -> {target}" for action, target in wrong]
    reasons += [f"漏联系：{action} -> {target}" for action, target in missing]
    reasons += [f"重复动作：{item.action} -> {item.target}" for item in duplicates]
    if clarified is False:
        reasons.append("应回问发送者，但没有回问")
    if fallback:
        reasons.append("模型调用失败，走了兜底回复")

    return {
        "id": case["id"],
        "category": case["category"],
        "group": case_group(case),
        "hard": bool(case.get("hard")),
        "passed": not reasons,
        "localized": localized,
        "estimate_correct": estimate_correct,
        "clarified": clarified,
        "fallback": fallback,
        "wrong_contacts": [list(item) for item in wrong],
        "wrong_contacts_to_others": [list(item) for item in wrong if item[1] not in senders],
        "missing_contacts": [list(item) for item in missing],
        "duplicate_actions": [asdict(item) for item in duplicates],
        "reasons": reasons,
        "observed": {
            "waits": [asdict(wait) for wait in observation.waits],
            "contacts": [asdict(item) for item in sorted(observation.contacts)],
            "reply_categories": observation.reply_categories,
            "reply_texts": observation.reply_texts,
            "model_items": observation.model_items,
        },
    }


def _ratio(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return {"correct": sum(1 for row in rows if row[key]), "total": len(rows)}


def summarize(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """The reported numbers. Wrong contacts stay a count of messages, never a rate."""
    groups = {
        group: _ratio([row for row in scores if row["group"] == group], "localized")
        for group in ("should_open", "should_not_open", "existing_wait")
    }
    categories = {
        category: _ratio([row for row in scores if row["category"] == category], "passed")
        for category in sorted({row["category"] for row in scores})
    }
    estimates = [row for row in scores if row["estimate_correct"] is not None]
    clarifications = [row for row in scores if row["clarified"] is not None]
    return {
        "scenarios": len(scores),
        "upstream_localization": _ratio(scores, "localized"),
        "localization_by_group": groups,
        "wrong_contacts": sum(len(row["wrong_contacts"]) for row in scores),
        "wrong_contacts_to_others": sum(len(row["wrong_contacts_to_others"]) for row in scores),
        "cases_with_wrong_contact": [row["id"] for row in scores if row["wrong_contacts"]],
        "duplicate_actions": sum(len(row["duplicate_actions"]) for row in scores),
        "missing_contacts": sum(len(row["missing_contacts"]) for row in scores),
        "estimate_time": _ratio(estimates, "estimate_correct"),
        "clarification": _ratio(clarifications, "clarified"),
        "cases_passed": _ratio(scores, "passed"),
        "passed_by_category": categories,
        "hard_cases_passed": _ratio([row for row in scores if row["hard"]], "passed"),
        "fallbacks": sum(1 for row in scores if row["fallback"]),
    }


async def evaluate(
    intent_client: IntentClient,
    cases: list[dict[str, Any]] | None = None,
    project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases = load_cases() if cases is None else cases
    project = load_project() if project is None else project
    scores: list[dict[str, Any]] = []
    for case in cases:
        scores.append(score_case(case, await run_case(case, project, intent_client)))
    return {"summary": summarize(scores), "cases": scores}


# ------------------------------------------------------------------------ CLI


def behaviour_view(report: dict[str, Any]) -> dict[str, Any]:
    """A report without the model's raw output, for comparing a replay with the live run.

    Identical requests can come back with a different reported confidence, and a recording
    keeps only the last response, so raw output is not reproducible even when behaviour is.
    """
    plain = json.loads(json.dumps({key: report[key] for key in ("summary", "cases")}))
    for case in plain["cases"]:
        del case["observed"]["model_items"]
    return plain


def results_path(model: str) -> Path:
    return RESULTS_DIR / f"{PROMPT_VERSION}_{model}.json"


def recordings_dir(model: str) -> Path:
    return RECORDINGS_DIR / f"{PROMPT_VERSION}_{model}"


def _print_report(report: dict[str, Any], label: str) -> None:
    summary = report["summary"]

    def pair(entry: dict[str, int]) -> str:
        return f"{entry['correct']}/{entry['total']}"

    groups = summary["localization_by_group"]
    print(f"R1 依赖等待评测（{label}）")
    print(f"  上游定位准确：{pair(summary['upstream_localization'])}")
    print(f"    应建立等待：{pair(groups['should_open'])}")
    print(f"    不应建立等待：{pair(groups['should_not_open'])}")
    print(f"    已有等待（取消或回复）：{pair(groups['existing_wait'])}")
    print(f"  错误联系数：{summary['wrong_contacts']}")
    print(f"    其中联系了发送者以外的成员：{summary['wrong_contacts_to_others']}")
    print(f"  重复动作数：{summary['duplicate_actions']}")
    print(f"  漏联系数：{summary['missing_contacts']}")
    print(f"  预计时间正确：{pair(summary['estimate_time'])}")
    print(f"  应回问且回问：{pair(summary['clarification'])}")
    print(f"  场景全部通过：{pair(summary['cases_passed'])}")
    print(f"  模型调用失败：{summary['fallbacks']}")
    for row in report["cases"]:
        if not row["passed"]:
            print(f"  [失败] {row['id']}")
            for reason in row["reasons"]:
                print(f"      {reason}")


def _build_llm_client(args: argparse.Namespace) -> LLMIntentClient:
    if args.replay:
        return LLMIntentClient(
            api_key="replay-only",
            base_url="https://replay.invalid",
            model=args.model,
            business_tz=BUSINESS_TZ,
            mode="replay",
            recordings_dir=recordings_dir(args.model),
        )
    settings = OrgPilotSettings.from_env()
    if settings.llm_provider == "mock":
        raise SystemExit("在线评测需要设置 ORGPILOT_LLM_PROVIDER 和对应的密钥")
    client = LLMIntentClient.from_settings(
        settings,
        mode="live",
        record=True,
        recordings_dir=recordings_dir(args.model),
        max_spend_usd=args.max_spend_usd,
    )
    if client.model != args.model:
        raise SystemExit(f"配置的模型是 {client.model}，与 --model {args.model} 不一致")
    return client


def _validate_args(args: argparse.Namespace) -> None:
    if args.client != "llm":
        if args.live or args.replay:
            raise SystemExit("--live 和 --replay 只能与 --client llm 一起使用")
        return
    if args.live == args.replay:
        raise SystemExit("--client llm 需要选择 --live 或 --replay")
    mismatches = frozen_mismatches()
    if mismatches:
        raise SystemExit("评测集与冻结版本不一致：\n" + "\n".join(mismatches))
    if args.live:
        if not args.confirm_spend or args.max_spend_usd is None:
            raise SystemExit("在线评测必须同时传 --confirm-spend 和 --max-spend-usd")
        if results_path(args.model).exists():
            raise SystemExit(
                f"{results_path(args.model).name} 已存在：看过结果后重跑会让数字失去意义"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R1 dependency wait evaluation")
    parser.add_argument("--client", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--confirm-spend", action="store_true")
    parser.add_argument("--max-spend-usd", type=float)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--write-results", action="store_true")
    parser.add_argument("--check", action="store_true", help="compare with the committed results")
    args = parser.parse_args(argv)
    _validate_args(args)

    client: Any = HeuristicIntentClient() if args.client == "heuristic" else _build_llm_client(args)
    try:
        report = asyncio.run(evaluate(client))
    except IntentInfrastructureError as exc:
        print(f"评测中止：{exc}", file=sys.stderr)
        return 1

    mode = "live" if args.live else "replay" if args.replay else "heuristic"
    _print_report(report, f"{mode}，{args.model if args.client == 'llm' else '无模型'}")

    if args.client == "llm":
        stats = aggregate_llm_metrics(client.metrics)
        print(f"  模型调用：{stats.get('call_count', 0)} 次")
        if args.live and not stats.get("call_count"):
            # Bad credentials or an outage say nothing about the model.
            print("评测中止：没有一次模型调用成功，结果不写入", file=sys.stderr)
            return 1
        if args.live:
            print(f"  估算费用：${stats.get('total_cost_usd_estimate')}")
            if args.write_results:
                report["run"] = {
                    **dataset_fingerprint(),
                    "model": client.model,
                    "llm_stats": stats,
                }
                RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                results_path(args.model).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

    if args.check:
        committed = json.loads(results_path(args.model).read_text(encoding="utf-8"))
        same = behaviour_view(committed) == behaviour_view(report)
        print("  系统行为与已提交结果一致" if same else "  系统行为与已提交结果不一致")
        return 0 if same else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

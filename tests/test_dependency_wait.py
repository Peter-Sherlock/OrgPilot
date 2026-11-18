"""R1 dependency waits (ADR-0016): rules, projection, and the message closed loop."""

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.coordination.dependency_wait import (
    UpstreamOutcome,
    resolve_upstream,
    wait_case_id,
    wait_clauses,
)
from orgpilot.domain.enums import (
    ActionType,
    CommandStatus,
    CoordinationCaseStatus,
    DependencyWaitReleaseReason,
    DependencyWaitStatus,
    PolicyDisposition,
    WorkflowStatus,
)
from orgpilot.domain.errors import DomainInvariantError
from orgpilot.domain.models import ActionCommand, ActionResult, CoordinationAction
from orgpilot.events.models import (
    DependencyDeliveryEstimatedEvent,
    DependencyDeliveryEstimatedPayload,
    DependencyWaitCancelledEvent,
    DependencyWaitCancelledPayload,
    DependencyWaitReportedEvent,
    DependencyWaitReportedPayload,
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    TaskCreatedEvent,
    TaskCreatedPayload,
    TaskWorkflowChangedEvent,
    TaskWorkflowChangedPayload,
)
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.gateway.service import GatewayService
from orgpilot.intent.client import HeuristicIntentClient, IntentClient, ScriptedIntentClient
from orgpilot.intent.dialogue import CLARIFICATION_SOURCE_PROBE
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
from orgpilot.policy.engine import PolicyEngine
from orgpilot.state.projector import OrgProjector
from orgpilot.storage.database import Database

BJ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=BJ)
PROJECT = "proj-wait"
WAIT_ID = "wait:page:api:one"
ON = OrgPilotSettings(dependency_wait_enabled=True)


# ------------------------------------------------------------------- fixtures


def _common(event_id: str, source: EventSource = EventSource.HUMAN) -> dict:
    return {
        "project_id": PROJECT,
        "event_id": event_id,
        "source": source,
        "source_ref": f"setup:{event_id}",
        "occurred_at": NOW,
        "received_at": NOW,
    }


def _member(member_id: str, name: str, role: str) -> MemberRegisteredEvent:
    return MemberRegisteredEvent(
        event_type="member.registered",
        payload=MemberRegisteredPayload(member_id=member_id, display_name=name, role=role),
        **_common(f"evt-member-{member_id}"),
    )


def _task(
    task_id: str,
    title: str,
    owner: str,
    dependencies: tuple[str, ...] = (),
    deadline: datetime | None = NOW + timedelta(days=5),
) -> TaskCreatedEvent:
    return TaskCreatedEvent(
        event_type="task.created",
        payload=TaskCreatedPayload(
            task_id=task_id,
            title=title,
            owner_id=owner,
            workflow_status=WorkflowStatus.DOING,
            deadline=deadline,
            dependencies=dependencies,
        ),
        **_common(f"evt-task-{task_id}", EventSource.TASK),
    )


def _base_events() -> list:
    return [
        _member("alice", "Alice", "frontend"),
        _member("bob", "Bob", "backend"),
        _member("dana", "Dana", "frontend"),
        _member("pm", "Pat", "pm"),
        _task("api", "支付接口", "bob"),
        _task("order", "订单服务", "bob"),
        _task("page", "结算页面", "alice", ("api",)),
        _task("cart", "购物车页面", "dana", ("api",)),
    ]


def _reported(
    event_id: str = "evt-report",
    wait_id: str = WAIT_ID,
    actor: str = "alice",
    waiting: str = "page",
    upstream: str = "api",
) -> DependencyWaitReportedEvent:
    return DependencyWaitReportedEvent(
        event_type="dependency.wait_reported",
        actor_id=actor,
        payload=DependencyWaitReportedPayload(
            wait_id=wait_id,
            waiting_task_id=waiting,
            upstream_task_id=upstream,
            quote="在等支付接口",
        ),
        **_common(event_id, EventSource.MESSAGE),
    )


def _estimated(actor: str = "bob") -> DependencyDeliveryEstimatedEvent:
    return DependencyDeliveryEstimatedEvent(
        event_type="dependency.delivery_estimated",
        actor_id=actor,
        payload=DependencyDeliveryEstimatedPayload(
            wait_id=WAIT_ID, estimated_at=NOW + timedelta(days=1), quote="明天可联调"
        ),
        **_common("evt-estimate", EventSource.MESSAGE),
    )


def _workflow_done(task_id: str) -> TaskWorkflowChangedEvent:
    return TaskWorkflowChangedEvent(
        event_type="task.workflow_changed",
        payload=TaskWorkflowChangedPayload(task_id=task_id, to_status=WorkflowStatus.DONE),
        **_common(f"evt-done-{task_id}", EventSource.TASK),
    )


def _projector(*events: object) -> OrgProjector:
    projector = OrgProjector(project_id=PROJECT)
    projector.replay([*_base_events(), *events])
    return projector


def _item(quote: str, **overrides: object) -> IntentItem:
    values: dict[str, object] = {
        "intent": IntentType.REPORT_STATUS,
        "speech_act": SpeechAct.STATEMENT,
        "polarity": Polarity.AFFIRMATIVE,
        "certainty": Certainty.CERTAIN,
        "source_quote": quote,
        "confidence": 0.95,
    }
    values.update(overrides)
    return IntentItem(**values)  # type: ignore[arg-type]


def _result(*items: IntentItem, answers_pending: bool = False) -> IntentResult:
    return IntentResult(items=list(items), answers_pending=answers_pending)


class RecordingAdapter(MockCollaborationAdapter):
    """Exposes a Feishu client so the router's replies are observable too."""

    def __init__(self, project_id: str) -> None:
        super().__init__(project_id=project_id)
        self.client = MockFeishuClient()
        self.fail_action_types: set[ActionType] = set()

    def send_private_message(self, command: ActionCommand) -> ActionResult:
        if command.action_type in self.fail_action_types:
            result = ActionResult(
                command_id=command.command_id,
                action_id=command.action_id,
                status=CommandStatus.FAILED,
                error="network down",
                executed_at=command.created_at,
            )
            self.audit_log.append((command, result))
            return result
        return super().send_private_message(command)

    def sent(self, action_type: ActionType) -> list[ActionCommand]:
        return [cmd for cmd, _ in self.audit_log if cmd.action_type is action_type]


async def _make_router(
    intent_client: IntentClient | None = None,
    settings: OrgPilotSettings = ON,
    database_url: str = "sqlite+aiosqlite:///:memory:",
    seed: bool = True,
) -> tuple[MessageRouter, GatewayService, RecordingAdapter, Database]:
    db = Database(database_url)
    await db.init_db()
    adapter = RecordingAdapter(PROJECT)
    service = GatewayService(db, adapter_factory=lambda _project: adapter)
    if seed:
        await service.ingest_raw_events(
            PROJECT, [event.model_dump(mode="json") for event in _base_events()]
        )
    router = MessageRouter(
        service, intent_client=intent_client or HeuristicIntentClient(), settings=settings
    )
    return router, service, adapter, db


async def _send(
    router: MessageRouter,
    text: str,
    sender: str = "alice",
    at: datetime = NOW,
    message_id: str | None = None,
):
    return await router.handle_message(
        project_id=PROJECT,
        sender_id=sender,
        message_id=message_id or f"om-{uuid.uuid4().hex[:8]}",
        text=text,
        occurred_at=at,
    )


async def _state(service: GatewayService):
    return (await service.get_or_replay_agent(PROJECT)).projector.state


async def _only_wait(service: GatewayService):
    waits = list((await _state(service)).dependency_waits.values())
    assert len(waits) == 1
    return waits[0]


# ---------------------------------------------------------------- projection


def test_report_opens_a_wait_and_a_repeat_only_adds_evidence() -> None:
    projector = _projector(_reported(), _reported(event_id="evt-report-2"))

    wait = projector.state.dependency_waits[WAIT_ID]
    assert wait.status is DependencyWaitStatus.AWAITING_ESTIMATE
    assert wait.evidence_event_ids == ("evt-report", "evt-report-2")


def test_a_second_active_wait_for_the_same_pair_is_rejected() -> None:
    with pytest.raises(DomainInvariantError, match="already active"):
        _projector(_reported(), _reported(event_id="evt-other", wait_id="wait:other"))


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (_reported(actor="dana"), "only the owner"),
        (_reported(upstream="order"), "not a formal dependency"),
    ],
)
def test_report_requires_the_owner_and_a_formal_edge(event: object, message: str) -> None:
    with pytest.raises(DomainInvariantError, match=message):
        _projector(event)


def test_estimate_does_not_release_or_touch_official_task_state() -> None:
    before = _projector(_reported()).state.tasks["api"]

    projector = _projector(_reported(), _estimated())

    wait = projector.state.dependency_waits[WAIT_ID]
    assert wait.status is DependencyWaitStatus.ESTIMATED
    assert wait.release_reason is None
    assert projector.state.tasks["api"] == before
    assert projector.state.health_claims == {}


def test_only_the_upstream_owner_can_estimate() -> None:
    with pytest.raises(DomainInvariantError, match="only the owner"):
        _projector(_reported(), _estimated(actor="alice"))


@pytest.mark.parametrize(
    ("task_id", "reason"),
    [
        ("api", DependencyWaitReleaseReason.UPSTREAM_DONE),
        ("page", DependencyWaitReleaseReason.WAITER_DONE),
    ],
)
def test_formal_completion_releases_the_wait(
    task_id: str, reason: DependencyWaitReleaseReason
) -> None:
    wait = _projector(_reported(), _workflow_done(task_id)).state.dependency_waits[WAIT_ID]

    assert wait.status is DependencyWaitStatus.RELEASED
    assert wait.release_reason is reason
    assert wait.released_by_event_id == f"evt-done-{task_id}"


def test_only_the_reporter_can_cancel() -> None:
    cancel = DependencyWaitCancelledEvent(
        event_type="dependency.wait_cancelled",
        actor_id="bob",
        payload=DependencyWaitCancelledPayload(wait_id=WAIT_ID, quote="不用等了"),
        **_common("evt-cancel", EventSource.MESSAGE),
    )
    with pytest.raises(DomainInvariantError, match="can cancel"):
        _projector(_reported(), cancel)


# --------------------------------------------------------------------- rules


def _resolve(text: str, dependencies: tuple[str, ...] = ("api", "order")):
    state = _projector().state
    waiting = state.tasks["page"].model_copy(update={"dependencies": dependencies})
    return resolve_upstream(state, waiting, text)


@pytest.mark.parametrize(
    ("text", "dependencies", "outcome", "task_id"),
    [
        ("结算页面在等支付接口", ("api", "order"), UpstreamOutcome.MATCHED, "api"),
        ("我在等接口", ("api",), UpstreamOutcome.MATCHED, "api"),
        ("我在等上游", ("api",), UpstreamOutcome.UNNAMED, None),
        ("我在等上游", ("api", "order"), UpstreamOutcome.AMBIGUOUS, None),
        ("结算页面在等", (), UpstreamOutcome.NO_DEPENDENCY, None),
    ],
)
def test_resolve_upstream_never_guesses(
    text: str,
    dependencies: tuple[str, ...],
    outcome: UpstreamOutcome,
    task_id: str | None,
) -> None:
    resolution = _resolve(text, dependencies)

    assert resolution.outcome is outcome
    assert resolution.task_id == task_id


def test_the_waiting_tasks_own_title_does_not_name_an_upstream() -> None:
    state = _projector().state
    waiting = state.tasks["page"].model_copy(
        update={"title": "支付页面", "dependencies": ("api", "order")}
    )

    assert resolve_upstream(state, waiting, "支付页面在等接口").outcome is UpstreamOutcome.AMBIGUOUS


def test_a_finished_upstream_is_reported_rather_than_chased() -> None:
    state = _projector(_workflow_done("api")).state

    resolution = resolve_upstream(state, state.tasks["page"], "在等支付接口")

    assert resolution.outcome is UpstreamOutcome.ALREADY_DONE
    assert resolution.candidates == ("api",)


def test_an_upstream_owned_by_the_sender_needs_no_contact() -> None:
    state = _projector().state
    state.tasks["api"] = state.tasks["api"].model_copy(update={"owner_id": "alice"})

    resolution = resolve_upstream(state, state.tasks["page"], "在等支付接口")

    assert resolution.outcome is UpstreamOutcome.SELF_OWNED


@pytest.mark.parametrize(
    "message",
    [
        "支付接口好了吗？我们还等着",
        "如果还要等支付接口就麻烦了",
        "不用等支付接口了",
        "等一下支付接口的事",
        "支付接口报错了，等我修好",
    ],
)
def test_questions_hypotheticals_and_idioms_are_not_wait_statements(message: str) -> None:
    assert not any("支付接口" in clause for clause in wait_clauses(message))


# -------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    ("targets", "disposition"),
    [(("bob",), PolicyDisposition.ALLOW), (("bob", "pm"), PolicyDisposition.DENY)],
)
def test_wait_messages_are_private_to_exactly_one_person(
    targets: tuple[str, ...], disposition: PolicyDisposition
) -> None:
    action = CoordinationAction(
        action_id="a",
        action_type=ActionType.NOTIFY_WAIT_OPENED,
        targets=targets,
        reason_refs=(),
        expected_effect="tell",
    )

    assert PolicyEngine().evaluate(action).disposition is disposition


# ---------------------------------------------------------------- closed loop


async def test_flag_off_keeps_the_recognized_but_unsupported_behaviour() -> None:
    message = "一直等着支付接口，谁来催一下"
    client = ScriptedIntentClient(
        responses={message: _result(_item(message, intent=IntentType.DEPENDENCY_WAIT))}
    )
    router, service, adapter, db = await _make_router(client, OrgPilotSettings())
    try:
        outcome = await _send(router, message)

        assert outcome.reply_category is ReplyKind.UNSUPPORTED
        assert (await _state(service)).dependency_waits == {}
        assert adapter.audit_log == []
    finally:
        await db.close()


async def test_closed_loop_from_wait_to_relayed_estimate_to_delivery() -> None:
    router, service, adapter, db = await _make_router()
    try:
        opened = await _send(router, "结算页面在等支付接口")

        assert opened.reply_category is ReplyKind.CLAIM_RECORDED
        wait = await _only_wait(service)
        assert (wait.waiting_task_id, wait.upstream_task_id) == ("page", "api")
        assert [cmd.targets for cmd in adapter.sent(ActionType.ASK_DEPENDENCY_ETA)] == [("bob",)]
        assert [cmd.targets for cmd in adapter.sent(ActionType.NOTIFY_WAIT_OPENED)] == [("pm",)]
        assert adapter.sent(ActionType.ASK_RECOVERY_ESTIMATE) == []
        agent = await service.get_or_replay_agent(PROJECT)
        case = agent.case_ledger.get_case(wait_case_id(wait.wait_id))
        assert case.status is CoordinationCaseStatus.WAITING_FOR_RESPONSE
        assert case.waiting_for == "bob"
        assert case.waiting_until is None

        answered = await _send(router, "明天下午3点可以联调", sender="bob")

        assert "不代表已经交付" in answered.reply_text
        wait = await _only_wait(service)
        assert wait.status is DependencyWaitStatus.ESTIMATED
        assert wait.estimated_at == datetime(2026, 11, 19, 15, 0, tzinfo=BJ)
        relays = adapter.sent(ActionType.RELAY_DELIVERY_ESTIMATE)
        assert sorted(cmd.targets[0] for cmd in relays) == ["alice", "pm"]
        state = await _state(service)
        assert state.health_claims == {}
        assert state.tasks["api"].workflow_status is WorkflowStatus.DOING
        agent = await service.get_or_replay_agent(PROJECT)
        assert (
            agent.case_ledger.get_case(wait_case_id(wait.wait_id)).status
            is CoordinationCaseStatus.RESOLVED
        )

        await service.ingest_raw_events(PROJECT, [_workflow_done("api").model_dump(mode="json")])

        wait = await _only_wait(service)
        assert wait.status is DependencyWaitStatus.RELEASED
        assert wait.release_reason is DependencyWaitReleaseReason.UPSTREAM_DONE
    finally:
        await db.close()


async def test_frozen_prompt_output_is_promoted_without_questioning_the_waiter() -> None:
    message = "结算页面在等支付接口"
    frozen_style = _result(
        _item(message, task_id="page", health_status="at_risk", blocker="等支付接口")
    )
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: frozen_style})
    )
    try:
        await _send(router, message)

        assert (await _only_wait(service)).upstream_task_id == "api"
        assert (await _state(service)).health_claims == {}
        assert adapter.sent(ActionType.ASK_RECOVERY_ESTIMATE) == []
    finally:
        await db.close()


async def test_a_real_status_report_survives_next_to_the_wait() -> None:
    message = "结算页面开发正常，在等支付接口"
    healthy = _result(_item(message, task_id="page", health_status="on_track"))
    router, service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: healthy})
    )
    try:
        await _send(router, message)

        state = await _state(service)
        assert len(state.dependency_waits) == 1
        assert [claim.health_status.value for claim in state.health_claims.values()] == ["on_track"]
    finally:
        await db.close()


async def test_a_stated_delay_is_kept_as_a_claim_next_to_the_estimate() -> None:
    message = "支付接口延期了，后天下午3点才能联调"
    delayed = _item(
        message,
        task_id="api",
        health_status="delayed",
        blocker="第三方证书过期",
        expected_completion=NOW + timedelta(days=2),
    )
    client = ScriptedIntentClient(
        responses={message: _result(delayed)}, default=_result(_item("结算页面在等支付接口"))
    )
    router, service, _adapter, db = await _make_router(client)
    try:
        await _send(router, "结算页面在等支付接口")

        await _send(router, message, sender="bob")

        assert (await _only_wait(service)).status is DependencyWaitStatus.ESTIMATED
        claims = (await _state(service)).health_claims.values()
        assert [claim.health_status.value for claim in claims] == ["delayed"]
    finally:
        await db.close()


async def test_ambiguous_upstream_asks_the_sender_and_contacts_nobody() -> None:
    ambiguous = "接口网关在等上游"
    item = _item(ambiguous, intent=IntentType.DEPENDENCY_WAIT, task_id="gate")
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={ambiguous: _result(item)})
    )
    try:
        await service.ingest_raw_events(
            PROJECT,
            [_task("gate", "接口网关", "alice", ("api", "order")).model_dump(mode="json")],
        )

        outcome = await _send(router, ambiguous)

        assert outcome.reply_category is ReplyKind.CLARIFY
        assert "支付接口" in outcome.reply_text and "订单服务" in outcome.reply_text
        assert (await _state(service)).dependency_waits == {}
        assert adapter.audit_log == []
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("overrides", "category"),
    [
        ({"speech_act": SpeechAct.QUESTION}, ReplyKind.NOTED),
        ({"speech_act": SpeechAct.HYPOTHETICAL}, ReplyKind.NOTED),
        ({"certainty": Certainty.HEDGED}, ReplyKind.CLARIFY),
        ({"confidence": 0.3}, ReplyKind.CLARIFY),
        ({"polarity": Polarity.NEGATIVE}, ReplyKind.NOTED),
    ],
)
async def test_non_assertions_create_no_wait_and_contact_nobody(
    overrides: dict, category: ReplyKind
) -> None:
    message = "结算页面和支付接口的事"
    item = _item(message, intent=IntentType.DEPENDENCY_WAIT, task_id="page", **overrides)
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: _result(item)})
    )
    try:
        outcome = await _send(router, message)

        assert outcome.reply_category is category
        assert (await _state(service)).dependency_waits == {}
        assert adapter.audit_log == []
    finally:
        await db.close()


async def test_someone_elses_task_cannot_open_a_wait() -> None:
    message = "购物车页面在等支付接口"
    item = _item(message, intent=IntentType.DEPENDENCY_WAIT, task_id="cart")
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: _result(item)})
    )
    try:
        await _send(router, message, sender="pm")

        assert (await _state(service)).dependency_waits == {}
        assert adapter.audit_log == []
    finally:
        await db.close()


async def test_repeated_reports_ask_the_upstream_owner_once() -> None:
    router, service, adapter, db = await _make_router()
    try:
        await _send(router, "结算页面在等支付接口", message_id="om-same")
        await _send(router, "结算页面在等支付接口", message_id="om-same")
        repeat = await _send(router, "还在等支付接口呢")

        assert "已经登记过" in repeat.reply_text
        assert len(adapter.sent(ActionType.ASK_DEPENDENCY_ETA)) == 1
        assert len(adapter.sent(ActionType.NOTIFY_WAIT_OPENED)) == 1
        assert len((await _only_wait(service)).evidence_event_ids) == 2
    finally:
        await db.close()


async def test_two_waiters_share_one_question_and_one_answer() -> None:
    router, service, adapter, db = await _make_router()
    try:
        await _send(router, "结算页面在等支付接口", sender="alice")
        await _send(router, "购物车页面在等支付接口", sender="dana", at=NOW + timedelta(minutes=5))

        assert len(adapter.sent(ActionType.ASK_DEPENDENCY_ETA)) == 1

        await _send(router, "明天下午3点可以联调", sender="bob", at=NOW + timedelta(minutes=10))

        waits = (await _state(service)).dependency_waits.values()
        assert {wait.status for wait in waits} == {DependencyWaitStatus.ESTIMATED}
        relays = adapter.sent(ActionType.RELAY_DELIVERY_ESTIMATE)
        assert {"alice", "dana"} <= {cmd.targets[0] for cmd in relays}
    finally:
        await db.close()


async def test_cancelling_the_asking_wait_hands_the_question_to_the_next_waiter() -> None:
    router, service, adapter, db = await _make_router()
    try:
        await _send(router, "结算页面在等支付接口", sender="alice")
        await _send(router, "购物车页面在等支付接口", sender="dana", at=NOW + timedelta(minutes=5))

        cancelled = await _send(router, "不用等了", sender="alice", at=NOW + timedelta(minutes=9))

        assert "已取消" in cancelled.reply_text
        asks = adapter.sent(ActionType.ASK_DEPENDENCY_ETA)
        assert len(asks) == 2
        assert "购物车页面" in asks[1].payload["inquiry_text"]
    finally:
        await db.close()


async def test_estimate_after_the_deadline_is_flagged_but_never_rescheduled() -> None:
    router, service, adapter, db = await _make_router()
    try:
        await _send(router, "结算页面在等支付接口")
        deadline = (await _state(service)).tasks["page"].deadline

        await _send(router, "10天后可以联调", sender="bob")

        relay = adapter.sent(ActionType.RELAY_DELIVERY_ESTIMATE)[0]
        assert "不会自动改期" in relay.payload["inquiry_text"]
        assert (await _state(service)).tasks["page"].deadline == deadline
        agent = await service.get_or_replay_agent(PROJECT)
        assert agent.approval_manager.get_all_requests() == ()
    finally:
        await db.close()


async def test_upstream_reply_binds_after_a_restart(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'wait.db').as_posix()}"
    router, _service, _adapter, db = await _make_router(database_url=url)
    await _send(router, "结算页面在等支付接口")
    await db.close()

    router, service, adapter, db = await _make_router(database_url=url, seed=False)
    try:
        await _send(router, "明天下午3点可以联调", sender="bob")

        assert (await _only_wait(service)).status is DependencyWaitStatus.ESTIMATED
        assert len(adapter.sent(ActionType.RELAY_DELIVERY_ESTIMATE)) == 2
        assert adapter.sent(ActionType.ASK_DEPENDENCY_ETA) == []
    finally:
        await db.close()


async def test_a_pending_probe_is_neither_overwritten_nor_answered_by_the_wait() -> None:
    router, service, _adapter, db = await _make_router()
    try:
        await _send(router, "结算页面在等支付接口")
        router.register_pending_clarification(
            PROJECT,
            "bob",
            "请汇报进展",
            task_ids=["api"],
            source=CLARIFICATION_SOURCE_PROBE,
            occurred_at=NOW,
        )

        await _send(router, "明天下午3点可以联调", sender="bob")

        pending = router.dialogue.get_pending_clarification(PROJECT, "bob", now=NOW)
        assert (await _only_wait(service)).status is DependencyWaitStatus.AWAITING_ESTIMATE
        assert pending is None or pending.source == CLARIFICATION_SOURCE_PROBE
    finally:
        await db.close()


async def test_a_reply_without_a_time_keeps_waiting_and_records_nothing() -> None:
    router, service, adapter, db = await _make_router()
    try:
        await _send(router, "结算页面在等支付接口")

        outcome = await _send(router, "我看一下", sender="bob")

        assert outcome.reply_category is ReplyKind.CLARIFY
        assert (await _only_wait(service)).status is DependencyWaitStatus.AWAITING_ESTIMATE
        assert (await _state(service)).health_claims == {}
        assert adapter.sent(ActionType.RELAY_DELIVERY_ESTIMATE) == []
    finally:
        await db.close()


async def test_a_failed_send_is_reported_and_never_retried() -> None:
    router, service, adapter, db = await _make_router()
    adapter.fail_action_types.add(ActionType.ASK_DEPENDENCY_ETA)
    try:
        outcome = await _send(router, "结算页面在等支付接口")

        assert "没能联系到 Bob" in outcome.reply_text
        wait = await _only_wait(service)
        agent = await service.get_or_replay_agent(PROJECT)
        case = agent.case_ledger.get_case(wait_case_id(wait.wait_id))
        assert case.status is CoordinationCaseStatus.ESCALATED
        assert "network down" in (case.terminal_reason or "")

        adapter.fail_action_types.clear()
        repeat = await _send(router, "还在等支付接口")

        assert len(adapter.sent(ActionType.ASK_DEPENDENCY_ETA)) == 1
        assert "没能联系到 Bob" in repeat.reply_text
    finally:
        await db.close()


async def test_an_answer_that_gives_no_time_never_reaches_the_claim_path_without_a_task() -> None:
    message = "还没法给时间"
    answer = _item(
        message,
        intent=IntentType.ANSWER_PENDING,
        polarity=Polarity.NEGATIVE,
        health_status="at_risk",
    )
    wait_text = "结算页面在等支付接口"
    client = ScriptedIntentClient(
        responses={
            message: _result(answer, answers_pending=True),
            wait_text: _result(_item(wait_text, task_id="page", health_status="at_risk")),
        }
    )
    router, service, _adapter, db = await _make_router(client)
    try:
        await _send(router, wait_text)

        outcome = await _send(router, message, sender="bob")

        assert "消息处理失败" not in outcome.reply_text
        assert (await _only_wait(service)).status is DependencyWaitStatus.AWAITING_ESTIMATE
        claims = (await _state(service)).health_claims.values()
        assert [claim.task_id for claim in claims] == ["api"]
    finally:
        await db.close()


@pytest.mark.parametrize("intent", [IntentType.ANSWER_PENDING, IntentType.REPORT_STATUS])
async def test_a_reply_that_fits_two_upstream_tasks_asks_which_one(intent: IntentType) -> None:
    router, service, adapter, db = await _make_router()
    try:
        await service.ingest_raw_events(
            PROJECT, [_task("ledger", "账务页面", "dana", ("order",)).model_dump(mode="json")]
        )
        await _send(router, "结算页面在等支付接口", sender="alice")
        await _send(router, "账务页面在等订单服务", sender="dana")
        message = "明天下午3点可以联调"
        router.intent_client = ScriptedIntentClient(
            responses={message: _result(_item(message, intent=intent))}
        )

        outcome = await _send(router, message, sender="bob")

        assert outcome.reply_category is ReplyKind.CLARIFY
        assert "支付接口" in outcome.reply_text and "订单服务" in outcome.reply_text
        statuses = {w.status for w in (await _state(service)).dependency_waits.values()}
        assert statuses == {DependencyWaitStatus.AWAITING_ESTIMATE}
        assert adapter.sent(ActionType.RELAY_DELIVERY_ESTIMATE) == []
    finally:
        await db.close()


async def test_cancelling_with_two_open_waits_asks_which_one() -> None:
    router, service, _adapter, db = await _make_router()
    try:
        await service.ingest_raw_events(
            PROJECT,
            [_task("gate", "接口网关", "alice", ("api", "order")).model_dump(mode="json")],
        )
        await _send(router, "结算页面在等支付接口")
        await _send(router, "接口网关在等订单服务")

        outcome = await _send(router, "不用等了")

        assert outcome.reply_category is ReplyKind.CLARIFY
        waits = (await _state(service)).dependency_waits.values()
        assert len(waits) == 2
        assert DependencyWaitStatus.RELEASED not in {wait.status for wait in waits}
    finally:
        await db.close()


async def test_the_named_task_decides_when_two_of_yours_wait_on_the_same_upstream() -> None:
    router, service, adapter, db = await _make_router()
    try:
        await service.ingest_raw_events(
            PROJECT, [_task("gate", "接口网关", "alice", ("api",)).model_dump(mode="json")]
        )

        named = await _send(router, "结算页面在等支付接口")

        assert named.reply_category is ReplyKind.CLAIM_RECORDED
        assert (await _only_wait(service)).waiting_task_id == "page"

        unnamed = await _send(router, "我在等支付接口")

        assert unnamed.reply_category is ReplyKind.CLARIFY
        assert len(adapter.sent(ActionType.ASK_DEPENDENCY_ETA)) == 1
    finally:
        await db.close()


async def test_a_finished_waiting_task_cannot_open_a_wait() -> None:
    message = "结算页面在等支付接口"
    item = _item(message, intent=IntentType.DEPENDENCY_WAIT, task_id="page")
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: _result(item)})
    )
    try:
        await service.ingest_raw_events(PROJECT, [_workflow_done("page").model_dump(mode="json")])

        outcome = await _send(router, message)

        assert outcome.reply_category is ReplyKind.NOTED
        assert (await _state(service)).dependency_waits == {}
        assert adapter.audit_log == []
    finally:
        await db.close()


async def test_an_unanswered_wait_stays_visible_in_a_status_query() -> None:
    message = "结算页面现在什么情况"
    query = _item(message, intent=IntentType.QUERY_STATUS, task_id="page")
    wait_text = "结算页面在等支付接口"
    client = ScriptedIntentClient(
        responses={
            message: _result(query),
            wait_text: _result(_item(wait_text, task_id="page", health_status="at_risk")),
        }
    )
    router, _service, _adapter, db = await _make_router(client)
    try:
        await _send(router, wait_text)

        outcome = await _send(router, message)

        assert "正在等【支付接口】：Bob 的交付时间待确认" in outcome.reply_text
    finally:
        await db.close()

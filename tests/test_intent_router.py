"""Integration tests for the layered MessageRouter (ADR-0008 §3-§4)."""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.domain.enums import ProbeMemberStatus
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.gateway.service import GatewayService
from orgpilot.intent.client import IntentClient, ScriptedIntentClient
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

BJ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=BJ)
PROJECT = "proj-router"


class RecordingAdapter(MockCollaborationAdapter):
    """Mock adapter exposing a Feishu client so outbound replies are observable."""

    def __init__(self, project_id: str) -> None:
        super().__init__(project_id=project_id)
        self.client = MockFeishuClient()


def _setup_events() -> list[dict]:
    return [
        {
            "schema_version": 1,
            "project_id": PROJECT,
            "event_id": "evt-r-alice",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": NOW.isoformat(),
            "received_at": NOW.isoformat(),
            "payload": {"member_id": "ou_alice", "display_name": "Alice", "role": "backend"},
        },
        {
            "schema_version": 1,
            "project_id": PROJECT,
            "event_id": "evt-r-carol",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": NOW.isoformat(),
            "received_at": NOW.isoformat(),
            "payload": {"member_id": "ou_carol", "display_name": "Carol", "role": "pm"},
        },
        {
            "schema_version": 1,
            "project_id": PROJECT,
            "event_id": "evt-r-task",
            "event_type": "task.created",
            "source": "task",
            "source_ref": "setup",
            "occurred_at": NOW.isoformat(),
            "received_at": NOW.isoformat(),
            "payload": {
                "task_id": "backend_api",
                "title": "后端接口",
                "owner_id": "ou_alice",
                "workflow_status": "doing",
                "deadline": "2026-10-30T18:00:00+08:00",
            },
        },
    ]


def _item(message: str, **overrides: object) -> IntentItem:
    values: dict[str, object] = {
        "intent": IntentType.REPORT_STATUS,
        "task_id": "backend_api",
        "speech_act": SpeechAct.STATEMENT,
        "polarity": Polarity.AFFIRMATIVE,
        "certainty": Certainty.CERTAIN,
        "health_status": "at_risk",
        "source_quote": message,
        "confidence": 0.95,
    }
    values.update(overrides)
    return IntentItem(**values)  # type: ignore[arg-type]


def _result(*items: IntentItem, answers_pending: bool = False) -> IntentResult:
    return IntentResult(items=list(items), answers_pending=answers_pending)


async def _make_router(
    intent_client: IntentClient | None = None,
) -> tuple[MessageRouter, GatewayService, RecordingAdapter, Database]:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    adapter = RecordingAdapter(PROJECT)
    service = GatewayService(db, adapter_factory=lambda _project: adapter)
    await service.ingest_raw_events(PROJECT, _setup_events())
    router = MessageRouter(service, intent_client=intent_client)
    return router, service, adapter, db


async def _send(router: MessageRouter, text: str, sender: str = "ou_alice") -> object:
    return await router.handle_message(
        project_id=PROJECT,
        sender_id=sender,
        message_id=f"om-{uuid.uuid4().hex[:8]}",
        text=text,
        occurred_at=NOW,
    )


async def test_duplicate_message_returns_cached_outcome_without_resending() -> None:
    scripted = _result(_item("支付SDK报错了，明天下午5点能修好"))
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={"支付SDK报错了，明天下午5点能修好": scripted})
    )
    try:
        first = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_dup",
            text="支付SDK报错了，明天下午5点能修好",
            occurred_at=NOW,
        )
        assert first.reply_category == ReplyKind.CLAIM_RECORDED
        replies_after_first = len(adapter.client.sent_messages)

        redelivered = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_dup",
            text="支付SDK报错了，明天下午5点能修好",
            occurred_at=NOW,
        )
        assert redelivered.msg == "duplicate_suppressed"
        assert redelivered.duplicate is True
        assert redelivered.reply_category == ReplyKind.CLAIM_RECORDED
        assert len(adapter.client.sent_messages) == replies_after_first
        events = await service.event_store.get_events(PROJECT)
        assert sum(1 for e in events if e.event_type == "task.health_reported") == 1
    finally:
        await db.close()


async def test_group_messages_are_ignored() -> None:
    router, service, adapter, db = await _make_router(ScriptedIntentClient())
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_group",
            text="有人在吗",
            occurred_at=NOW,
            chat_type="group",
        )
        assert outcome.msg == "group_not_supported"
        assert adapter.client.sent_messages == []
        assert len(adapter.client.sent_cards) == 0
        events = await service.event_store.get_events(PROJECT)
        assert len(events) == len(_setup_events())
    finally:
        await db.close()


async def test_unregistered_sender_gets_hint_and_no_events() -> None:
    router, service, adapter, db = await _make_router(ScriptedIntentClient())
    try:
        outcome = await _send(router, "支付SDK报错了", sender="ou_stranger")
        assert outcome.msg == "sender_not_registered"
        assert len(adapter.client.sent_messages) == 1
        events = await service.event_store.get_events(PROJECT)
        assert len(events) == len(_setup_events())
    finally:
        await db.close()


async def test_pm_slash_sync_starts_directly_and_non_pm_is_denied() -> None:
    router, service, adapter, db = await _make_router(ScriptedIntentClient())
    try:
        # non-PM first: no session exists yet, so the command hits the role gate
        denied = await _send(router, "/同步", sender="ou_alice")
        assert denied.reply_category == ReplyKind.NO_PERMISSION
        coordinator = await service.get_sync_coordinator(PROJECT)
        assert len(coordinator._sessions) == 0

        outcome = await _send(router, "/同步", sender="ou_carol")
        assert outcome.msg == "sync_session_started"
        assert outcome.reply_category == ReplyKind.SYNC_STARTED
        coordinator = await service.get_sync_coordinator(PROJECT)
        assert len(coordinator._sessions) == 1
    finally:
        await db.close()


async def test_help_and_unknown_commands_return_help_text() -> None:
    router, _service, _adapter, db = await _make_router(ScriptedIntentClient())
    try:
        for command in ("/帮助", "/不存在的命令"):
            outcome = await _send(router, command)
            assert outcome.reply_category == ReplyKind.HELP
            assert "/同步" in outcome.reply_text
    finally:
        await db.close()


async def test_classify_failure_falls_back_without_retry() -> None:
    """Non-transient failures get NO router retry: the LLM client already
    retried validation internally; repeating a protocol error is waste (3b.1)."""
    router, service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={"报错": RuntimeError("boom")})
    )
    try:
        outcome = await _send(router, "报错")
        assert outcome.msg == "fallback"
        assert outcome.reply_category == ReplyKind.FALLBACK
        assert outcome.reply_text == "没理解，可以换个说法吗？也可以发送 /帮助 查看用法"
        scripted = router.intent_client
        assert isinstance(scripted, ScriptedIntentClient)
        assert len(scripted.calls) == 1  # transient errors only are retried
        events = await service.event_store.get_events(PROJECT)
        assert len(events) == len(_setup_events())
    finally:
        await db.close()


async def test_transient_failure_is_retried_once() -> None:
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={"报错": TimeoutError("slow provider")})
    )
    try:
        outcome = await _send(router, "报错")
        assert outcome.msg == "fallback"
        scripted = router.intent_client
        assert isinstance(scripted, ScriptedIntentClient)
        assert len(scripted.calls) == 2  # exactly one retry for a transient error
    finally:
        await db.close()


async def test_infrastructure_error_is_neither_retried_nor_degraded() -> None:
    from orgpilot.intent.llm_client import (
        IntentInfrastructureError,
        ReplayMissError,
        SpendLimitExceeded,
    )

    assert issubclass(ReplayMissError, IntentInfrastructureError)
    assert issubclass(SpendLimitExceeded, IntentInfrastructureError)
    for infrastructure_failure in (
        ReplayMissError("missing replay recording: x"),
        SpendLimitExceeded("budget gone"),
    ):
        router, _service, _adapter, db = await _make_router(
            ScriptedIntentClient(responses={"报错": infrastructure_failure})
        )
        try:
            with pytest.raises(IntentInfrastructureError):
                await _send(router, "报错")
            scripted = router.intent_client
            assert isinstance(scripted, ScriptedIntentClient)
            assert len(scripted.calls) == 1  # loud failure: no retry, no fallback
        finally:
            await db.close()


async def test_invalid_intent_output_type_falls_back() -> None:
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={"报错": {"not": "an intent result"}})
    )
    try:
        outcome = await _send(router, "报错")
        assert outcome.msg == "fallback"
    finally:
        await db.close()


async def test_scripted_claim_is_recorded_with_summary() -> None:
    message = "支付SDK报错了，明天下午5点能修好"
    scripted = _result(
        _item(
            message,
            expected_completion=datetime(2026, 10, 14, 17, 0, tzinfo=BJ),
            blocker="第三方接口",
        )
    )
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_claim",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.CLAIM_RECORDED
        assert "已记录：后端接口" in outcome.reply_text
        agent = await service.get_or_replay_agent(PROJECT)
        assert agent.projector.state.tasks["backend_api"].health_status.value == "at_risk"
    finally:
        await db.close()


async def test_answer_pending_merges_into_pending_claim_and_clears_state() -> None:
    message = "预计明天下午5点能恢复"
    scripted = _result(
        _item(
            message,
            intent=IntentType.ANSWER_PENDING,
            health_status="on_track",
            expected_completion=datetime(2026, 10, 14, 17, 0, tzinfo=BJ),
        ),
        answers_pending=True,
    )
    router, service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    router.register_pending_clarification(
        PROJECT,
        "ou_alice",
        "大概什么时候能恢复？",
        task_ids=["backend_api"],
        occurred_at=NOW,
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_answer",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.CLAIM_RECORDED
        assert outcome.asks_clarification is False
        assert router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW) is None
        agent = await service.get_or_replay_agent(PROJECT)
        assert agent.projector.state.tasks["backend_api"].health_status.value == "on_track"
    finally:
        await db.close()


async def test_incomplete_answer_asks_for_remaining_slots() -> None:
    message = "还在卡着，具体几点说不好"
    scripted = _result(
        _item(message, intent=IntentType.ANSWER_PENDING, health_status="at_risk"),
        answers_pending=True,
    )
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    router.register_pending_clarification(
        PROJECT,
        "ou_alice",
        "大概什么时候能恢复？",
        task_ids=["backend_api"],
        occurred_at=NOW,
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_answer2",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.CLAIM_RECORDED
        assert outcome.asks_clarification is True
        pending = router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW)
        assert pending is not None
        assert pending.asked_rounds == 1
        assert pending.claims["backend_api"].health_status == "at_risk"
    finally:
        await db.close()


async def test_query_status_answers_from_projection() -> None:
    message = "后端接口现在什么情况？"
    scripted = _result(_item(message, intent=IntentType.QUERY_STATUS, task_id="backend_api"))
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_carol",
            message_id="om_query",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.QUERY_ANSWER
        assert "后端接口" in outcome.reply_text
    finally:
        await db.close()


async def test_pm_request_sync_sends_confirmation_card_without_starting() -> None:
    message = "大家进展怎么样了"
    scripted = _result(
        _item(
            message,
            intent=IntentType.REQUEST_SYNC,
            task_id=None,
            speech_act=SpeechAct.IMPERATIVE,
        )
    )
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_carol",
            message_id="om_sync",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.SYNC_CONFIRM
        assert outcome.msg == "success"
        coordinator = await service.get_sync_coordinator(PROJECT)
        assert len(coordinator._sessions) == 0  # card first, NO probes yet
        assert len(adapter.client.sent_cards) == 1
        assert adapter.client.sent_cards[0]["card"]["kind"] == "sync_confirmation"
        confirmation = router.dialogue.get_pending_sync_confirmation(
            PROJECT, "ou_carol", now=NOW
        )
        assert confirmation is not None and confirmation.member_ids == ["ou_alice"]
    finally:
        await db.close()


async def test_negative_request_sync_is_noted() -> None:
    message = "不用同步进度了"
    scripted = _result(
        _item(message, intent=IntentType.REQUEST_SYNC, task_id=None, polarity=Polarity.NEGATIVE)
    )
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_carol",
            message_id="om_nosync",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.NOTED
        assert adapter.client.sent_cards == []
        # the noted reply goes out as plain text, no probes anywhere
        assert [m["msg_type"] for m in adapter.client.sent_messages] == ["text"]
    finally:
        await db.close()


async def test_acknowledge_confirms_pending_card_and_starts_sync() -> None:
    message = "好的，发吧"
    scripted = _result(
        _item(message, intent=IntentType.ACKNOWLEDGE, task_id=None, speech_act=SpeechAct.STATEMENT)
    )
    router, service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    router.register_pending_sync_confirmation(
        PROJECT, "ou_carol", initiated_by="ou_carol", member_ids=["ou_alice"], occurred_at=NOW
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_carol",
            message_id="om_ack",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.SYNC_STARTED
        assert outcome.msg == "sync_session_started"
        coordinator = await service.get_sync_coordinator(PROJECT)
        assert len(coordinator._sessions) == 1
        assert (
            router.dialogue.get_pending_sync_confirmation(PROJECT, "ou_carol", now=NOW) is None
        )
    finally:
        await db.close()


async def test_acknowledge_without_pending_item_is_smalltalk() -> None:
    message = "收到"
    scripted = _result(_item(message, intent=IntentType.ACKNOWLEDGE, task_id=None))
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_ack2",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.SMALLTALK
    finally:
        await db.close()


async def test_topic_switch_reasks_without_counting_rounds() -> None:
    message = "哈哈哈你们看热搜了吗"
    scripted = _result(
        _item(message, intent=IntentType.CHITCHAT, task_id=None), answers_pending=False
    )
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    router.register_pending_clarification(
        PROJECT,
        "ou_alice",
        "大概什么时候能恢复？",
        task_ids=["backend_api"],
        occurred_at=NOW,
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_topic",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.SMALLTALK
        assert outcome.asks_clarification is True
        assert "大概什么时候能恢复" in outcome.reply_text
        pending = router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW)
        assert pending is not None and pending.asked_rounds == 0  # R9: not incremented
        assert pending.reask_count == 1  # 3a.2: topic switches count separately
    finally:
        await db.close()


async def test_topic_switch_reask_stops_after_budget() -> None:
    message = "哈哈哈你们看热搜了吗"
    scripted = _result(
        _item(message, intent=IntentType.CHITCHAT, task_id=None), answers_pending=False
    )
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    router.register_pending_clarification(
        PROJECT,
        "ou_alice",
        "大概什么时候能恢复？",
        task_ids=["backend_api"],
        occurred_at=NOW,
    )
    try:
        first = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_topic_1",
            text=message,
            occurred_at=NOW,
        )
        assert first.asks_clarification is True
        second = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_topic_2",
            text=message,
            occurred_at=NOW,
        )
        # re-ask budget (ORGPILOT_MAX_TOPIC_REASKS=1) is used up: same friendly
        # reply, but no question attached and the pending state stays open
        assert second.reply_category == ReplyKind.SMALLTALK
        assert second.asks_clarification is False
        assert "大概什么时候能恢复" not in second.reply_text
        pending = router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW)
        assert pending is not None and pending.reask_count == 2
    finally:
        await db.close()


async def test_topic_switch_total_cap_declares_probe_incomplete() -> None:
    message = "哈哈哈你们看热搜了吗"
    scripted = _result(
        _item(message, intent=IntentType.CHITCHAT, task_id=None), answers_pending=False
    )
    router, service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        session = await service.start_progress_sync(PROJECT, initiated_by="ou_carol")
        router.register_pending_clarification(
            PROJECT,
            "ou_alice",
            "请汇报您负责任务的最新进展。",
            task_ids=["backend_api"],
            source="probe",
            occurred_at=NOW,
        )
        outcome = None
        for index in range(1, 5):
            outcome = await router.handle_message(
                project_id=PROJECT,
                sender_id="ou_alice",
                message_id=f"om_topic_cap_{index}",
                text=message,
                occurred_at=NOW,
            )
        # asked_rounds(0) + reask_count(4) reached the combined cap (3 + 1):
        # the probe is declared incomplete and the pending state is cleared
        assert outcome is not None
        assert outcome.reply_category == ReplyKind.SMALLTALK
        assert outcome.asks_clarification is False
        assert "信息不全" in outcome.reply_text
        assert router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW) is None
        coordinator = await service.get_sync_coordinator(PROJECT)
        probe = coordinator.get_session(session.session_id).member_probes["ou_alice"]
        assert probe.status == ProbeMemberStatus.COLLECTED
        assert probe.incomplete is True
    finally:
        await db.close()


async def test_clarify_total_cap_clears_quietly_on_topic_switch() -> None:
    message = "哈哈哈你们看热搜了吗"
    scripted = _result(
        _item(message, intent=IntentType.CHITCHAT, task_id=None), answers_pending=False
    )
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    router.register_pending_clarification(
        PROJECT,
        "ou_alice",
        "大概什么时候能恢复？",
        task_ids=["backend_api"],
        occurred_at=NOW,
        asked_rounds=router.dialogue.max_clarification_rounds,
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_topic_clarify_cap",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.SMALLTALK
        assert outcome.asks_clarification is False
        assert "信息不全" not in outcome.reply_text
        assert router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW) is None
    finally:
        await db.close()


async def test_multi_item_reply_uses_priority_merge() -> None:
    message = "支付SDK报错了，另外帮我订会议室"
    scripted = _result(
        _item(message),
        _item(message, intent=IntentType.OUT_OF_SCOPE, task_id=None),
    )
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_multi",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.CLAIM_RECORDED  # claim beats unsupported
        assert "暂不支持" in outcome.reply_text
    finally:
        await db.close()


async def test_sync_card_confirm_and_cancel_flows() -> None:
    router, service, _adapter, db = await _make_router(ScriptedIntentClient())
    try:
        router.register_pending_sync_confirmation(
            PROJECT,
            "ou_carol",
            initiated_by="ou_carol",
            member_ids=["ou_alice"],
            occurred_at=NOW,
        )
        value = {
            "action": "sync_confirm",
            "initiated_by": "ou_carol",
            "requested_at": NOW.isoformat(),
        }
        # a non-initiator cannot confirm
        result = await router.handle_sync_card_action(
            project_id=PROJECT,
            action_value=value,
            operator_id="ou_alice",
            now=NOW,
        )
        assert result["msg"] == "sync_card_invalid"
        coordinator = await service.get_sync_coordinator(PROJECT)
        assert len(coordinator._sessions) == 0

        # the initiator confirms -> sync starts and the card is closed in place
        result = await router.handle_sync_card_action(
            project_id=PROJECT,
            action_value=value,
            operator_id="ou_carol",
            now=NOW,
        )
        assert result["msg"] == "sync_session_started"
        assert "已发起" in result["card"]["header"]["title"]["content"]
        coordinator = await service.get_sync_coordinator(PROJECT)
        assert len(coordinator._sessions) == 1

        # expiry: a stale requested_at beyond the 30-minute TTL is rejected
        router.register_pending_sync_confirmation(
            PROJECT,
            "ou_carol",
            initiated_by="ou_carol",
            member_ids=["ou_alice"],
            occurred_at=NOW - timedelta(minutes=31),
        )
        stale = {
            "action": "sync_confirm",
            "initiated_by": "ou_carol",
            "requested_at": (NOW - timedelta(minutes=31)).isoformat(),
        }
        result = await router.handle_sync_card_action(
            project_id=PROJECT,
            action_value=stale,
            operator_id="ou_carol",
            now=NOW,
        )
        assert result["msg"] == "sync_card_expired"
        assert "已过期" in result["card"]["header"]["title"]["content"]

        # cancel flow on a fresh confirmation
        router.register_pending_sync_confirmation(
            PROJECT,
            "ou_carol",
            initiated_by="ou_carol",
            member_ids=["ou_alice"],
            occurred_at=NOW,
        )
        result = await router.handle_sync_card_action(
            project_id=PROJECT,
            action_value={**value, "action": "sync_cancel"},
            operator_id="ou_carol",
            now=NOW,
        )
        assert result["msg"] == "sync_cancelled"
        assert (
            router.dialogue.get_pending_sync_confirmation(PROJECT, "ou_carol", now=NOW) is None
        )
    finally:
        await db.close()


async def test_probe_reply_flows_through_intent_pipeline() -> None:
    """3a.1: probe replies use the same classify→guard→handler path as normal
    messages, driven by the pending probe clarification in the dialogue store."""
    router, service, adapter, db = await _make_router(
        ScriptedIntentClient(
            responses={
                "支付SDK报错卡住了": _result(
                    _item(
                        "支付SDK报错卡住了",
                        intent=IntentType.ANSWER_PENDING,
                        health_status="at_risk",
                    ),
                    answers_pending=True,
                ),
                "预计排查需要到明天下午5点": _result(
                    _item(
                        "预计排查需要到明天下午5点",
                        intent=IntentType.ANSWER_PENDING,
                        health_status="delayed",
                        expected_completion=datetime(2026, 10, 14, 17, 0, tzinfo=BJ),
                        blocker="排查",
                    ),
                    answers_pending=True,
                ),
            }
        )
    )
    try:
        await service.start_progress_sync(PROJECT, initiated_by="ou_carol")
        router.register_pending_clarification(
            PROJECT,
            "ou_alice",
            "项目负责人正在同步项目进度，请汇报您负责任务的最新进展。",
            task_ids=["backend_api"],
            source="probe",
            occurred_at=NOW,
        )
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_probe1",
            text="支付SDK报错卡住了",
            occurred_at=NOW,
        )
        # vague probe answer: claim recorded but time missing → follow-up question
        assert outcome.msg == "clarification_sent"
        assert outcome.asks_clarification is True
        pending = router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW)
        assert pending is not None and pending.asked_rounds == 1

        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_probe2",
            text="预计排查需要到明天下午5点",
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.CLAIM_RECORDED
        assert outcome.asks_clarification is False
        assert router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW) is None
        events = await service.event_store.get_events(PROJECT)
        assert any(e.event_type == "task.health_reported" for e in events)
    finally:
        await db.close()


async def test_commands_run_before_probe_capture_and_skip_rounds() -> None:
    """3a.1: a probed member's /帮助 gets help and consumes NO probe round."""
    router, service, adapter, db = await _make_router(ScriptedIntentClient())
    try:
        await service.start_progress_sync(PROJECT, initiated_by="ou_carol")
        pending = router.register_pending_clarification(
            PROJECT,
            "ou_alice",
            "请汇报您负责任务的最新进展。",
            task_ids=["backend_api"],
            source="probe",
            occurred_at=NOW,
        )
        outcome = await _send(router, "/帮助", sender="ou_alice")
        assert outcome.reply_category == ReplyKind.HELP
        assert outcome.asks_clarification is False
        assert pending.asked_rounds == 0

        outcome = await _send(router, "/同步", sender="ou_alice")
        assert outcome.reply_category == ReplyKind.NO_PERMISSION
        assert pending.asked_rounds == 0
        coordinator = await service.get_sync_coordinator(PROJECT)
        # exactly the setup session — the non-PM command started no second one
        assert len(coordinator._sessions) == 1
    finally:
        await db.close()


async def test_duplicate_slash_sync_reports_session_status() -> None:
    router, service, adapter, db = await _make_router(ScriptedIntentClient())
    try:
        first = await _send(router, "/同步", sender="ou_carol")
        assert first.msg == "sync_session_started"

        again = await _send(router, "/同步", sender="ou_carol")
        assert again.msg == "sync_already_active"
        assert again.reply_category == ReplyKind.NOTED
        assert "已回复 0/1" in again.reply_text
        coordinator = await service.get_sync_coordinator(PROJECT)
        assert len(coordinator._sessions) == 1  # no second session
    finally:
        await db.close()


async def test_guard_canned_verdicts_surface_as_replies() -> None:
    message = "支付SDK的情况怎么样了？顺便帮我订会议室"
    scripted = _result(
        _item(message, intent=IntentType.QUERY_STATUS, task_id="backend_api"),
        _item(message, speech_act=SpeechAct.HYPOTHETICAL, health_status=None),
        _item(message, intent=IntentType.OUT_OF_SCOPE, task_id=None),
        _item(
            message,
            intent=IntentType.REQUEST_SYNC,
            task_id=None,
            polarity=Polarity.AFFIRMATIVE,
        ),
    )
    router, _service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",  # non-PM: request_sync hits the role gate
            message_id="om_canned",
            text=message,
            occurred_at=NOW,
        )
        text = outcome.reply_text
        assert "后端接口" in text  # QUERY answered from projection
        assert "了解，如果确认需要调整请告诉我" in text  # NOTED (hypothetical)
        assert "暂不支持自动处理" in text  # UNSUPPORTED
        assert "只有项目负责人（PM）可以发起全员同步" in text  # NO_PERMISSION
        assert outcome.reply_category == ReplyKind.NO_PERMISSION  # highest priority
    finally:
        await db.close()


async def test_clarify_source_gives_up_quietly_after_max_rounds() -> None:
    message = "还是卡着"
    scripted = _result(
        _item(message, intent=IntentType.ANSWER_PENDING, health_status="at_risk"),
        answers_pending=True,
    )
    router, service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    total_cap = (
        router.dialogue.max_clarification_rounds + router.dialogue.max_topic_reasks
    )
    router.register_pending_clarification(
        PROJECT,
        "ou_alice",
        "大概什么时候能恢复？",
        task_ids=["backend_api"],
        occurred_at=NOW,
        asked_rounds=total_cap,
    )
    try:
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_giveup",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.CLAIM_RECORDED
        assert outcome.asks_clarification is False
        assert "已达追问上限" in outcome.reply_text
        assert router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW) is None
    finally:
        await db.close()


async def test_probe_declares_incomplete_after_max_rounds() -> None:
    message = "还是卡着"
    scripted = _result(
        _item(message, intent=IntentType.ANSWER_PENDING, health_status="at_risk"),
        answers_pending=True,
    )
    router, service, _adapter, db = await _make_router(
        ScriptedIntentClient(responses={message: scripted})
    )
    try:
        session = await service.start_progress_sync(PROJECT, initiated_by="ou_carol")
        router.register_pending_clarification(
            PROJECT,
            "ou_alice",
            "请汇报您负责任务的最新进展。",
            task_ids=["backend_api"],
            source="probe",
            occurred_at=NOW,
            asked_rounds=(
                router.dialogue.max_clarification_rounds + router.dialogue.max_topic_reasks
            ),
        )
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_incomplete",
            text=message,
            occurred_at=NOW,
        )
        assert outcome.reply_category == ReplyKind.CLAIM_RECORDED
        assert "信息不全" in outcome.reply_text
        assert outcome.data.get("probe_reply") is True
        coordinator = await service.get_sync_coordinator(PROJECT)
        probe = coordinator.get_session(session.session_id).member_probes["ou_alice"]
        assert probe.status == ProbeMemberStatus.COLLECTED
        assert probe.incomplete is True
    finally:
        await db.close()

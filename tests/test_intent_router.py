"""Integration tests for the layered MessageRouter (ADR-0008 §3-§4)."""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orgpilot.adapter.mock import MockCollaborationAdapter
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


async def test_classify_failure_retries_then_falls_back_without_state_change() -> None:
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
        assert len(scripted.calls) == 2  # exactly one retry
        events = await service.event_store.get_events(PROJECT)
        assert len(events) == len(_setup_events())
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
        task_id="backend_api",
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
        task_id="backend_api",
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
        assert pending.claim is not None and pending.claim.health_status == "at_risk"
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
        task_id="backend_api",
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


async def test_probe_reply_delegates_to_coordinator_and_registers_state() -> None:
    router, service, adapter, db = await _make_router(ScriptedIntentClient())
    try:
        await service.start_progress_sync(PROJECT, initiated_by="ou_carol")
        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_probe1",
            text="支付SDK报错卡住了",
            occurred_at=NOW,
        )
        assert outcome.msg == "clarification_sent"
        assert outcome.asks_clarification is True
        pending = router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW)
        assert pending is not None and pending.source == "probe"

        outcome = await router.handle_message(
            project_id=PROJECT,
            sender_id="ou_alice",
            message_id="om_probe2",
            text="预计排查需要到明天下午5点",
            occurred_at=NOW,
        )
        assert outcome.msg == "probe_collected"
        assert router.dialogue.get_pending_clarification(PROJECT, "ou_alice", now=NOW) is None
        events = await service.event_store.get_events(PROJECT)
        assert any(e.event_type == "task.health_reported" for e in events)
    finally:
        await db.close()

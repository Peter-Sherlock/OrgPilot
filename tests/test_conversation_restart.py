"""In-flight conversations survive a gateway restart (ADR-0013).

Each flow runs twice, each time on its own file-based SQLite database: once in a
single gateway process, and once with the gateway rebuilt on the same database
at the marked point (new engine, service, router, adapter, and a FrozenClock
pinned to the same instant). Both runs must observe exactly the same outcomes.

Every flow also asserts something that only holds when the state really was
restored, or really did expire from its ORIGINAL timestamp (a restore that
reset ``created_at`` / ``updated_at`` to the restart time would keep it alive).
"""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import FrozenClock
from orgpilot.domain.enums import ProbeMemberStatus, SyncSessionStatus
from orgpilot.domain.sync_models import MemberProbeState, SyncSession
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.webhook import FeishuWebhookHandler
from orgpilot.gateway.app import build_gateway_service
from orgpilot.gateway.service import GatewayService
from orgpilot.intent.client import ScriptedIntentClient
from orgpilot.intent.dialogue import (
    DialogueSnapshot,
    DialogueStateStore,
    PendingClaimDraft,
    PendingClarification,
    PendingSyncConfirmation,
)
from orgpilot.intent.models import (
    Certainty,
    IntentItem,
    IntentResult,
    IntentType,
    Polarity,
    SpeechAct,
)
from orgpilot.intent.router import MessageRouter
from orgpilot.storage.conversation_store import SqlConversationStore
from orgpilot.storage.database import Database
from orgpilot.storage.models import DialogueSnapshotRecord, SyncSessionRecord

BJ = ZoneInfo("Asia/Shanghai")
# Microseconds on purpose: persisted instants must round-trip exactly.
T0 = datetime(2026, 10, 21, 10, 0, 0, 123456, tzinfo=BJ)
PROJECT = "proj-restart"
PM, ALICE, BOB = "ou_carol", "ou_alice", "ou_bob"

HEDGED = "后端接口可能要延期"
ANSWER = "明天下午5点前能好"
REQUEST = "大家进展怎么样了"
ACK = "好的，发吧"
ALICE_OK = "后端接口一切正常"
BOB_OK = "前端页面正常推进"
QUERY = "后端接口现在什么情况？"


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _setup_events() -> list[dict[str, Any]]:
    def member(member_id: str, name: str, role: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": PROJECT,
            "event_id": f"evt-restart-{member_id}",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": T0.isoformat(),
            "received_at": T0.isoformat(),
            "payload": {"member_id": member_id, "display_name": name, "role": role},
        }

    def task(task_id: str, title: str, owner: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": PROJECT,
            "event_id": f"evt-restart-{task_id}",
            "event_type": "task.created",
            "source": "task",
            "source_ref": "setup",
            "occurred_at": T0.isoformat(),
            "received_at": T0.isoformat(),
            "payload": {
                "task_id": task_id,
                "title": title,
                "owner_id": owner,
                "workflow_status": "doing",
                "deadline": "2026-10-30T18:00:00+08:00",
            },
        }

    return [
        member(PM, "Carol", "pm"),
        member(ALICE, "Alice", "backend"),
        member(BOB, "Bob", "frontend"),
        task("backend_api", "后端接口", ALICE),
        task("frontend_page", "前端页面", BOB),
    ]


def _item(message: str, **overrides: Any) -> IntentItem:
    values: dict[str, Any] = {
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
    return IntentItem(**values)


def _answer(message: str, task_id: str | None, health: str, **overrides: Any) -> IntentResult:
    item = _item(
        message,
        intent=IntentType.ANSWER_PENDING,
        task_id=task_id,
        health_status=health,
        **overrides,
    )
    return IntentResult(items=[item], answers_pending=True)


RESPONSES: dict[str, Any] = {
    # hedged status without a time: the router asks for one (R6) and remembers it
    HEDGED: IntentResult(items=[_item(HEDGED, certainty=Certainty.HEDGED)]),
    # a task-less answer only lands on backend_api through the pending question
    ANSWER: _answer(
        ANSWER, None, "at_risk", expected_completion=datetime(2026, 10, 22, 17, 0, tzinfo=BJ)
    ),
    REQUEST: IntentResult(
        items=[
            _item(
                REQUEST,
                intent=IntentType.REQUEST_SYNC,
                task_id=None,
                speech_act=SpeechAct.IMPERATIVE,
            )
        ]
    ),
    ACK: IntentResult(items=[_item(ACK, intent=IntentType.ACKNOWLEDGE, task_id=None)]),
    ALICE_OK: _answer(ALICE_OK, "backend_api", "on_track"),
    BOB_OK: _answer(BOB_OK, "frontend_page", "on_track"),
    QUERY: IntentResult(items=[_item(QUERY, intent=IntentType.QUERY_STATUS)]),
}


class RecordingAdapter(MockCollaborationAdapter):
    """Mock adapter with a Feishu client so replies and cards are observable."""

    def __init__(self, project_id: str) -> None:
        super().__init__(project_id=project_id)
        self.client = MockFeishuClient()


class Gateway:
    """One gateway process: database engine, service, router, adapter, and clock."""

    def __init__(self, db_url: str, now: datetime) -> None:
        self.db = Database(db_url)
        self.clock = FrozenClock(now)
        self.adapter = RecordingAdapter(PROJECT)
        self.service = GatewayService(
            self.db, adapter_factory=lambda _project: self.adapter, clock=self.clock
        )
        self.intent = ScriptedIntentClient(responses=RESPONSES)
        self.router = MessageRouter(self.service, intent_client=self.intent)


class Run:
    """Drives one flow; ``restart_point`` rebuilds the gateway only when asked to."""

    def __init__(self, db_path: Path, *, restart: bool) -> None:
        self.db_url = _db_url(db_path)
        self.restart = restart
        self.restarts = 0
        self.gw = Gateway(self.db_url, T0)
        self._message_count = 0

    async def start(self) -> None:
        await self.gw.db.init_db()
        await self.gw.service.ingest_raw_events(PROJECT, _setup_events())

    async def restart_point(self) -> None:
        if not self.restart:
            return
        now = self.gw.clock.now()
        await self.gw.db.close()
        self.gw = Gateway(self.db_url, now)
        await self.gw.db.init_db()
        self.restarts += 1

    def advance(self, delta: timedelta) -> None:
        self.gw.clock.advance(delta)

    async def send(self, sender: str, text: str) -> tuple[str, str, str]:
        self._message_count += 1
        outcome = await self.gw.router.handle_message(
            project_id=PROJECT,
            sender_id=sender,
            message_id=f"om-restart-{self._message_count}",
            text=text,
            occurred_at=self.gw.clock.now(),
        )
        return outcome.msg, outcome.reply_category.value, outcome.reply_text

    async def click(self, operator: str, value: dict[str, Any]) -> tuple[str, str]:
        result = await self.gw.router.handle_sync_card_action(
            project_id=PROJECT, action_value=value, operator_id=operator, now=self.gw.clock.now()
        )
        return result["msg"], result["card"]["header"]["title"]["content"]

    def card_button(self, action: str) -> dict[str, Any]:
        card = self.gw.adapter.client.sent_cards[-1]["card"]
        buttons = card["elements"][-1]["actions"]
        return next(b["value"] for b in buttons if b["value"]["action"] == action)

    def last_context_had_pending_question(self) -> bool:
        return self.gw.intent.calls[-1][1].pending_question is not None

    async def claims(self) -> list[tuple[str, str, str | None]]:
        events = await self.gw.service.event_store.get_events(PROJECT)
        return [
            (
                e.payload.task_id,
                str(e.payload.health_status),
                e.payload.expected_completion.isoformat()
                if e.payload.expected_completion
                else None,
            )
            for e in events
            if e.event_type == "task.health_reported"
        ]

    async def session_view(self) -> dict[str, Any] | None:
        coordinator = await self.gw.service.get_sync_coordinator(PROJECT)
        sessions = coordinator.sessions()
        if not sessions:
            return None
        session = sessions[-1]
        briefings_sent = [
            command
            for command, _ in self.gw.adapter.audit_log
            if command.payload.get("is_executive_briefing")
        ]
        return {
            "sessions": len(sessions),
            "status": session.status.value,
            "probes": {
                member_id: (probe.status.value, probe.incomplete)
                for member_id, probe in sorted(session.member_probes.items())
            },
            "briefing": session.briefing is not None,
            "briefings_sent_by_this_process": len(briefings_sent),
            "updated_at": session.updated_at,
        }


Flow = Callable[[Run], Awaitable[list[Any]]]


async def _observe(db_path: Path, flow: Flow, *, restart: bool) -> list[Any]:
    run = Run(db_path, restart=restart)
    await run.start()
    try:
        observed = await flow(run)
        assert run.restarts == (1 if restart else 0)
        return observed
    finally:
        await run.gw.db.close()


async def _same_with_and_without_restart(tmp_path: Path, flow: Flow) -> list[Any]:
    single = await _observe(tmp_path / "single.db", flow, restart=False)
    restarted = await _observe(tmp_path / "restarted.db", flow, restart=True)
    assert restarted == single
    return restarted


# --------------------------------------------------------------- clarification


def _clarification_flow(before: timedelta, after: timedelta) -> Flow:
    async def flow(run: Run) -> list[Any]:
        asked = await run.send(ALICE, HEDGED)
        run.advance(before)
        await run.restart_point()
        run.advance(after)
        answered = await run.send(ALICE, ANSWER)
        return [
            asked,
            answered,
            run.last_context_had_pending_question(),
            await run.claims(),
        ]

    return flow


async def test_pending_clarification_survives_restart(tmp_path: Path) -> None:
    asked, answered, saw_pending, claims = await _same_with_and_without_restart(
        tmp_path, _clarification_flow(timedelta(hours=1), timedelta(hours=1))
    )
    assert asked[0] == "clarification_sent"
    assert saw_pending is True
    # the task-less answer reached backend_api only through the restored question
    assert answered[1] == "claim_recorded"
    assert "信息完整" in answered[2]
    assert claims == [("backend_api", "at_risk", "2026-10-22T17:00:00+08:00")]


async def test_expired_clarification_still_expires_after_restart(tmp_path: Path) -> None:
    # 20h before the restart + 5h after: 25h since the question, past the 24h TTL,
    # but only 5h since the restart.
    asked, answered, saw_pending, claims = await _same_with_and_without_restart(
        tmp_path, _clarification_flow(timedelta(hours=20), timedelta(hours=5))
    )
    assert asked[0] == "clarification_sent"
    assert saw_pending is False
    # without the question the answer names no task, so the bot asks which one
    assert answered[0] == "clarification_sent"
    assert claims == []


# --------------------------------------------------------- confirmation card


def _confirmation_flow(before: timedelta, after: timedelta, *, by_click: bool) -> Flow:
    async def flow(run: Run) -> list[Any]:
        card_sent = await run.send(PM, REQUEST)
        confirm = run.card_button("sync_confirm")  # what Feishu would call back with
        run.advance(before)
        await run.restart_point()
        run.advance(after)
        answered = await run.click(PM, confirm) if by_click else await run.send(PM, ACK)
        pending = run.gw.router.dialogue.get_pending_sync_confirmation(
            PROJECT, PM, now=run.gw.clock.now()
        )
        return [card_sent, answered, pending is None, await run.session_view()]

    return flow


@pytest.mark.parametrize("by_click", [True, False], ids=["card-click", "text-ack"])
async def test_pending_confirmation_card_survives_restart(tmp_path: Path, by_click: bool) -> None:
    card_sent, answered, cleared, session = await _same_with_and_without_restart(
        tmp_path,
        _confirmation_flow(timedelta(minutes=10), timedelta(minutes=5), by_click=by_click),
    )
    assert card_sent[1] == "sync_confirm"
    assert answered[0] == "sync_session_started"
    assert cleared is True
    assert session is not None and session["status"] == SyncSessionStatus.PROBING.value
    assert set(session["probes"]) == {ALICE, BOB}


@pytest.mark.parametrize("by_click", [True, False], ids=["card-click", "text-ack"])
async def test_expired_confirmation_card_still_expires_after_restart(
    tmp_path: Path, by_click: bool
) -> None:
    # 20 min before the restart + 11 min after: past the 30-minute TTL overall.
    card_sent, answered, cleared, session = await _same_with_and_without_restart(
        tmp_path,
        _confirmation_flow(timedelta(minutes=20), timedelta(minutes=11), by_click=by_click),
    )
    assert card_sent[1] == "sync_confirm"
    if by_click:
        assert answered == ("sync_card_expired", "📋 全员进度同步 [已关闭：已过期]")
    else:
        assert answered == ("success", "smalltalk", "收到。")
    assert cleared is True
    assert session is None  # nothing was started


# ---------------------------------------------------------------- /同步 session


async def _sync_converges_flow(run: Run) -> list[Any]:
    started = await run.send(PM, "/同步")
    run.advance(timedelta(minutes=30))
    alice = await run.send(ALICE, ALICE_OK)
    mid_way = await run.session_view()
    run.advance(timedelta(minutes=30))
    await run.restart_point()
    run.advance(timedelta(minutes=30))
    bob = await run.send(BOB, BOB_OK)
    converged = await run.session_view()
    return [started, alice, mid_way, bob, run.last_context_had_pending_question(), converged]


async def test_sync_session_mid_way_survives_restart_and_ends_in_briefing(
    tmp_path: Path,
) -> None:
    started, alice, mid_way, bob, saw_probe, converged = await _same_with_and_without_restart(
        tmp_path, _sync_converges_flow
    )
    assert started[0] == "sync_session_started"
    assert "信息完整" in alice[2]
    assert mid_way["status"] == SyncSessionStatus.PROBING.value
    assert mid_way["probes"][ALICE] == (ProbeMemberStatus.COLLECTED.value, False)
    assert mid_way["probes"][BOB] == (ProbeMemberStatus.PENDING.value, False)
    # Bob's reply is still the answer to his probe, and it converges the session
    assert saw_probe is True
    assert "信息完整" in bob[2]
    assert converged["status"] == SyncSessionStatus.COMPLETED.value
    assert converged["probes"] == {
        ALICE: (ProbeMemberStatus.COLLECTED.value, False),
        BOB: (ProbeMemberStatus.COLLECTED.value, False),
    }
    assert converged["briefing"] is True
    assert converged["briefings_sent_by_this_process"] == 1
    assert converged["sessions"] == 1


async def test_completed_session_and_cleared_questions_are_persisted(tmp_path: Path) -> None:
    run = Run(tmp_path / "orgpilot.db", restart=True)
    await run.start()
    try:
        await _sync_converges_flow(run)
        await run.restart_point()
        view = await run.session_view()
        assert view is not None and view["status"] == SyncSessionStatus.COMPLETED.value
        assert view["briefing"] is True
        await run.gw.router.restore(PROJECT)
        dialogue = run.gw.router.dialogue
        for member in (ALICE, BOB):
            assert dialogue.get_pending_clarification(PROJECT, member) is None
    finally:
        await run.gw.db.close()


async def _sync_times_out_flow(run: Run) -> list[Any]:
    started = await run.send(PM, "/同步")
    run.advance(timedelta(hours=1))
    alice = await run.send(ALICE, ALICE_OK)  # last session activity: T0 + 1h
    run.advance(timedelta(hours=1))
    await run.restart_point()
    # T0 + 25h01m: idle for 24h01m since Alice's reply, only 23h01m since the restart
    run.advance(timedelta(hours=23, minutes=1))
    query = await run.send(PM, QUERY)  # any inbound message ticks the session clock
    return [started, alice, query, await run.session_view()]


async def test_idle_sync_session_still_times_out_after_restart(tmp_path: Path) -> None:
    started, alice, query, timed_out = await _same_with_and_without_restart(
        tmp_path, _sync_times_out_flow
    )
    assert started[0] == "sync_session_started"
    assert query[1] == "query_answer"
    assert timed_out["status"] == SyncSessionStatus.TIMEOUT.value
    assert timed_out["probes"] == {
        ALICE: (ProbeMemberStatus.COLLECTED.value, False),
        BOB: (ProbeMemberStatus.TIMEOUT.value, False),
    }
    assert timed_out["briefing"] is True
    assert timed_out["briefings_sent_by_this_process"] == 1
    assert timed_out["updated_at"] == T0 + timedelta(hours=25, minutes=1)


# ---------------------------------------------------- the real Feishu wiring


def _feishu_message(sender: str, text: str, message_id: str, at: datetime) -> dict[str, Any]:
    millis = str(int(at.timestamp() * 1000))
    return {
        "header": {"event_type": "im.message.receive_v1", "create_time": millis},
        "event": {
            "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
            "message": {
                "message_id": message_id,
                "message_type": "text",
                "chat_type": "p2p",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "create_time": millis,
            },
        },
    }


async def _feishu_gateway(db_url: str, now: datetime) -> tuple[Any, MockFeishuClient, Any]:
    settings = OrgPilotSettings(
        collaboration_adapter="feishu",
        feishu_app_id="cli_restart",
        feishu_app_secret="restart-secret",
        feishu_project_id=PROJECT,
    )
    db = Database(db_url)
    await db.init_db()
    client = MockFeishuClient()
    service, _ = build_gateway_service(settings, db, FrozenClock(now), feishu_client=client)
    service.intent_router.intent_client = ScriptedIntentClient(responses=RESPONSES)
    return service, client, FeishuWebhookHandler(service=service, project_id=PROJECT)


async def test_feishu_card_click_after_restart_starts_the_sync(tmp_path: Path) -> None:
    """The same restart through ``build_gateway_service`` and the webhook handler."""
    db_url = _db_url(tmp_path / "orgpilot.db")
    service, client, handler = await _feishu_gateway(db_url, T0)
    try:
        await service.ingest_raw_events(PROJECT, _setup_events())
        reply = await handler.handle_event(_feishu_message(PM, REQUEST, "om_request", T0))
        assert reply["data"]["reply_category"] == "sync_confirm"
        [card] = client.sent_cards
        confirm = next(
            b["value"]
            for b in card["card"]["elements"][-1]["actions"]
            if b["value"]["action"] == "sync_confirm"
        )
    finally:
        await service.db.close()

    service, client, handler = await _feishu_gateway(db_url, T0 + timedelta(minutes=10))
    try:
        clicked = await handler.handle_event(
            {
                "open_message_id": "om_card",
                "operator": {"open_id": PM},
                "action": {"value": confirm},
            }
        )
        assert clicked["msg"] == "sync_session_started"
        assert "已发起" in clicked["card"]["header"]["title"]["content"]
        probed = sorted(m["receive_id"] for m in client.sent_messages)
        assert probed == [ALICE, BOB]
    finally:
        await service.db.close()


# ------------------------------------------------------------- store details


async def test_sync_sessions_reload_in_start_order(tmp_path: Path) -> None:
    db_url = _db_url(tmp_path / "orgpilot.db")
    first = Gateway(db_url, T0)
    await first.db.init_db()
    try:
        await first.service.ingest_raw_events(PROJECT, _setup_events())
        # Same frozen instant for both: only the stored order can tell them apart.
        older = await first.service.start_progress_sync(PROJECT, initiated_by=PM)
        newer = await first.service.start_progress_sync(PROJECT, initiated_by=PM)
        await first.service.save_conversation_state(PROJECT)
    finally:
        await first.db.close()

    second = Gateway(db_url, T0)
    await second.db.init_db()
    try:
        coordinator = await second.service.get_sync_coordinator(PROJECT)
        assert [s.session_id for s in coordinator.sessions()] == [
            older.session_id,
            newer.session_id,
        ]
        active = coordinator.get_active_session(PROJECT)
        assert active is not None and active.session_id == newer.session_id
    finally:
        await second.db.close()


async def test_persisted_timestamps_round_trip_exactly(tmp_path: Path) -> None:
    db = Database(_db_url(tmp_path / "orgpilot.db"))
    await db.init_db()
    try:
        completion = datetime(2026, 10, 22, 17, 30, 0, 654321, tzinfo=BJ)
        snapshot = DialogueSnapshot(
            clarifications={
                ALICE: PendingClarification(
                    question="大概什么时候能恢复？",
                    task_ids=["backend_api"],
                    claims={
                        "backend_api": PendingClaimDraft(
                            task_id="backend_api",
                            health_status="at_risk",
                            expected_completion=completion,
                        )
                    },
                    missing={"backend_api": ["预计完成时间"]},
                    asked_rounds=2,
                    reask_count=1,
                    created_at=T0,
                    source="probe",
                )
            },
            confirmations={
                PM: PendingSyncConfirmation(initiated_by=PM, member_ids=[ALICE], created_at=T0)
            },
        )
        session = SyncSession(
            session_id="sync-roundtrip",
            project_id=PROJECT,
            initiated_by=PM,
            status=SyncSessionStatus.PROBING,
            created_at=T0,
            updated_at=T0 + timedelta(seconds=1),
            member_probes={
                ALICE: MemberProbeState(member_id=ALICE, inquiry_sent_at=T0, last_reply_at=T0)
            },
        )
        await SqlConversationStore(db).save(PROJECT, dialogue=snapshot, sync_sessions=[session])

        fresh = SqlConversationStore(db)  # no write cache: reads what is on disk
        loaded = await fresh.load_dialogue(PROJECT)
        [loaded_session] = await fresh.load_sync_sessions(PROJECT)
        assert loaded == snapshot
        assert loaded_session == session
        restored = loaded.clarifications[ALICE]
        assert restored.created_at == T0
        assert restored.created_at.utcoffset() == T0.utcoffset()
        assert restored.claims["backend_api"].expected_completion == completion
        assert loaded_session.updated_at.utcoffset() == T0.utcoffset()
        assert await fresh.load_dialogue("another-project") is None
        assert await fresh.load_sync_sessions("another-project") == []
    finally:
        await db.close()


async def test_naive_timestamps_are_rejected_before_anything_is_written(
    tmp_path: Path,
) -> None:
    db = Database(_db_url(tmp_path / "orgpilot.db"))
    await db.init_db()
    try:
        store = SqlConversationStore(db)
        naive = datetime(2026, 10, 21, 10, 0)
        with pytest.raises(ValueError, match=r"timezone.*confirmations\['ou_carol'\]"):
            await store.save(
                PROJECT,
                dialogue=DialogueSnapshot(
                    confirmations={PM: PendingSyncConfirmation(initiated_by=PM, created_at=naive)}
                ),
            )
        with pytest.raises(ValueError, match=r"timezone.*inquiry_sent_at"):
            await store.save(
                PROJECT,
                sync_sessions=[
                    SyncSession(
                        session_id="sync-naive",
                        project_id=PROJECT,
                        initiated_by=PM,
                        created_at=T0,
                        updated_at=T0,
                        member_probes={
                            ALICE: MemberProbeState(member_id=ALICE, inquiry_sent_at=naive)
                        },
                    )
                ],
            )
        assert await store.load_dialogue(PROJECT) is None
        assert await store.load_sync_sessions(PROJECT) == []
    finally:
        await db.close()


def test_restore_keeps_entries_registered_in_memory_first() -> None:
    dialogue = DialogueStateStore(clock=FrozenClock(T0))
    newer = dialogue.register_pending_clarification(PROJECT, ALICE, "新的问题")
    persisted = DialogueSnapshot(
        clarifications={
            ALICE: PendingClarification(question="旧的问题", created_at=T0 - timedelta(hours=1)),
            BOB: PendingClarification(question="Bob 的问题", created_at=T0),
        }
    )
    dialogue.restore_project(PROJECT, persisted)
    assert dialogue.get_pending_clarification(PROJECT, ALICE) is newer
    bob = dialogue.get_pending_clarification(PROJECT, BOB)
    assert bob is not None and bob.question == "Bob 的问题"
    assert dialogue.export_project("another-project") == DialogueSnapshot()


async def test_restore_runs_once_per_project(tmp_path: Path) -> None:
    gateway = Gateway(_db_url(tmp_path / "orgpilot.db"), T0)
    await gateway.db.init_db()
    try:
        router = gateway.router
        await router.restore(PROJECT)
        # a later write by someone else is not re-read: memory is the live copy
        await SqlConversationStore(gateway.db).save(
            PROJECT,
            dialogue=DialogueSnapshot(
                clarifications={BOB: PendingClarification(question="?", created_at=T0)}
            ),
        )
        await router.restore(PROJECT)
        assert router.dialogue.get_pending_clarification(PROJECT, BOB) is None
    finally:
        await gateway.db.close()


async def test_unreadable_snapshots_are_dropped_instead_of_blocking_messages(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A row written by an older model version must not break routing."""
    run = Run(tmp_path / "orgpilot.db", restart=True)
    await run.start()
    try:
        await run.send(PM, "/同步")
        [kept] = (await run.gw.service.get_sync_coordinator(PROJECT)).sessions()
        async with run.gw.db.session() as session:
            # a field this model version no longer has (StrictModel forbids extras)
            await session.execute(
                update(DialogueSnapshotRecord)
                .where(DialogueSnapshotRecord.project_id == PROJECT)
                .values(state_json=json.dumps({"clarifications": {}, "retired_field": 1}))
            )
            session.add(
                SyncSessionRecord(
                    session_id="sync-from-older-version",
                    project_id=PROJECT,
                    status="probing",
                    data_json=json.dumps({"session_id": "sync-from-older-version"}),
                    updated_at=T0,
                )
            )
        await run.restart_point()

        with caplog.at_level("WARNING", logger="orgpilot.storage.conversation_store"):
            outcome = await run.send(ALICE, ALICE_OK)
        assert "Dropping unreadable dialogue snapshot" in caplog.text
        assert "Dropping unreadable sync session 'sync-from-older-version'" in caplog.text
        # routing still works; the probe question was lost with the dropped row,
        # so the answer is recorded as a plain claim (the pre-ADR-0013 behaviour)
        assert outcome[1] == "claim_recorded"
        coordinator = await run.gw.service.get_sync_coordinator(PROJECT)
        assert [s.session_id for s in coordinator.sessions()] == [kept.session_id]
        # the turn's checkpoint replaced the unreadable dialogue row
        assert await SqlConversationStore(run.gw.db).load_dialogue(PROJECT) == DialogueSnapshot()
    finally:
        await run.gw.db.close()

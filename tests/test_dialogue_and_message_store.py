"""Tests for dialogue state store and the processed-message log."""

from datetime import UTC, datetime, timedelta

import pytest

from orgpilot.intent.dialogue import (
    DialogueStateStore,
    PendingClaimDraft,
)
from orgpilot.storage.database import Database
from orgpilot.storage.message_store import SqlMessageStore

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)


def test_pending_clarification_roundtrip_and_clear() -> None:
    store = DialogueStateStore()
    entry = store.register_pending_clarification(
        "proj",
        "ou_a",
        "预计什么时候能恢复？",
        task_ids=["backend_api"],
        claims={"backend_api": PendingClaimDraft(task_id="backend_api", health_status="at_risk")},
        created_at=NOW,
    )
    assert entry.asked_rounds == 0
    assert entry.source == "clarify"
    got = store.get_pending_clarification("proj", "ou_a", now=NOW)
    assert got is entry
    got.asked_rounds += 1
    assert store.get_pending_clarification("proj", "ou_a", now=NOW).asked_rounds == 1
    store.clear_pending_clarification("proj", "ou_a")
    assert store.get_pending_clarification("proj", "ou_a", now=NOW) is None


def test_pending_clarification_expires_after_ttl() -> None:
    store = DialogueStateStore(clarification_ttl=timedelta(hours=24))
    store.register_pending_clarification("proj", "ou_a", "？", created_at=NOW)
    assert store.get_pending_clarification("proj", "ou_a", now=NOW + timedelta(hours=23))
    assert (
        store.get_pending_clarification("proj", "ou_a", now=NOW + timedelta(hours=25)) is None
    )
    # the expired entry was purged
    assert store.get_pending_clarification("proj", "ou_a", now=NOW) is None


def test_pending_sync_confirmation_ttl_and_clear() -> None:
    store = DialogueStateStore(confirmation_ttl=timedelta(minutes=30))
    store.register_pending_sync_confirmation(
        "proj", "ou_pm", initiated_by="ou_pm", member_ids=["ou_a", "ou_b"], created_at=NOW
    )
    got = store.get_pending_sync_confirmation("proj", "ou_pm", now=NOW + timedelta(minutes=29))
    assert got is not None and got.member_ids == ["ou_a", "ou_b"]
    assert (
        store.get_pending_sync_confirmation("proj", "ou_pm", now=NOW + timedelta(minutes=31))
        is None
    )
    store.register_pending_sync_confirmation(
        "proj", "ou_pm", initiated_by="ou_pm", member_ids=[], created_at=NOW
    )
    store.clear_pending_sync_confirmation("proj", "ou_pm")
    assert store.get_pending_sync_confirmation("proj", "ou_pm", now=NOW) is None


def test_state_is_per_project_and_sender() -> None:
    store = DialogueStateStore()
    store.register_pending_clarification("proj", "ou_a", "？", created_at=NOW)
    assert store.get_pending_clarification("proj", "ou_b", now=NOW) is None
    assert store.get_pending_clarification("other", "ou_a", now=NOW) is None


async def test_message_store_roundtrip_idempotent_and_recent_turns() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    store = SqlMessageStore(db)
    try:
        base = {
            "project_id": "proj",
            "message_id": "om_1",
            "sender_id": "ou_a",
            "message_text": "消息1",
            "intent_summary": {"items": [{"intent": "report_status"}]},
            "reply_category": "claim_recorded",
            "reply_text": "已记录",
            "response": {"code": 0, "msg": "success"},
            "created_at": NOW,
        }
        await store.record_outcome(**base)
        # recording the same message_id again keeps the first outcome
        await store.record_outcome(**{**base, "reply_text": "changed"})

        got = await store.get_outcome("proj", "om_1")
        assert got is not None
        assert got.reply_text == "已记录"
        assert got.intent_summary == '{"items": [{"intent": "report_status"}]}'
        assert await store.get_outcome("proj", "om_missing") is None

        for index in range(2, 10):
            await store.record_outcome(
                **{
                    **base,
                    "message_id": f"om_{index}",
                    "message_text": f"消息{index}",
                    "created_at": NOW + timedelta(minutes=index),
                }
            )
        turns = await store.recent_turns("proj", "ou_a", limit=3)
        assert [turn["message"] for turn in turns] == ["消息7", "消息8", "消息9"]
        assert all(turn["reply"] == "已记录" for turn in turns)
    finally:
        await db.close()


async def test_message_store_requires_init_db() -> None:
    db = Database("sqlite+aiosqlite:///:memory:")
    store = SqlMessageStore(db)
    try:
        # The table does not exist before init_db(); sqlite surfaces that as
        # OperationalError ("no such table").
        with pytest.raises(Exception, match="no such table"):
            await store.get_outcome("proj", "om_1")
    finally:
        await db.close()

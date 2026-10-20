"""L3-A isolation, durable one-shot send, and real approval/callback contracts."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from orgpilot.feishu.acceptance import AcceptanceWindow
from orgpilot.feishu.approval_acceptance import L3_DESCRIPTION, L3ApprovalAcceptance
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.storage.database import Database
from orgpilot.storage.models import OutboxRecord
from orgpilot.storage.outbox_store import SqlOutboxStore

CHALLENGE = "ORGPILOT-L3:offline-test-challenge-only"
SENDER = {"open_id": "ou_private_test", "sender_type": "user"}
MESSAGE = {
    "message_id": "om_inbound_private",
    "message_type": "text",
    "chat_type": "p2p",
    "content": json.dumps({"text": CHALLENGE}),
}


def make_probe(tmp_path: Path, client=None):
    client = client or MockFeishuClient()
    probe = L3ApprovalAcceptance(CHALLENGE, client, tmp_path / "feishu-l3-offline")
    probe.arm(30)
    return probe, client


def callback_for(client: MockFeishuClient, decision="approved") -> dict:
    sent = client.sent_cards[0]
    button = next(
        b for b in sent["card"]["elements"][-1]["actions"] if b["value"]["action"] == decision
    )
    return {
        "operator_id": sent["receive_id"],
        "message_id": sent["message_id"],
        "value": deepcopy(button["value"]),
    }


async def rows_for(probe):
    db = Database(f"sqlite+aiosqlite:///{probe.db_path.as_posix()}")
    try:
        return await SqlOutboxStore(db).list_rows(probe.run_id)
    finally:
        await db.close()


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_one_card_outbox_approval_reopen_and_redaction(tmp_path, decision):
    probe, client = make_probe(tmp_path)
    await asyncio.gather(
        probe.handle_message(MESSAGE, SENDER), probe.handle_message(MESSAGE, SENDER)
    )
    assert len(client.sent_cards) == 1
    assert client.sent_cards[0]["card"]["elements"][0]["text"]["content"] == L3_DESCRIPTION
    assert probe.snapshot()["filtered"] == {"duplicate_or_extra": 1}
    assert probe.snapshot()["reply_status"] == "api_accepted"
    assert probe.snapshot()["window_closed"] is False
    assert (await rows_for(probe))[0]["status"] == "delivered"
    response = await probe.handle_card(callback_for(client, decision))
    assert response["toast"]["type"] == "success" and "card" not in response
    assert await probe.handle_card(callback_for(client, decision)) is None
    await probe.handle_message(MESSAGE, SENDER)
    probe.close()
    probe.record_shutdown(True)
    request = probe.approvals.get_all_requests()[0]
    assert request.status.value == decision
    assert request.consumed is False
    assert probe.snapshot()["accepted_callbacks"] == 1
    assert client.sent_messages == [] and client.updated_tasks == []
    assert len(client.sent_cards) == 1
    report = (tmp_path / "feishu-l3-offline/report.json").read_text(encoding="utf-8")
    assert json.loads(report)["approval_status"] == decision
    assert not json.loads(report)["user_visible_confirmed"]
    db = Database(f"sqlite+aiosqlite:///{probe.db_path.as_posix()}")
    try:
        store = SqlOutboxStore(db)
        assert await store.due_commands(probe.run_id, datetime.now(UTC)) == []
        async with db.session() as session:
            command_json = (await session.execute(select(OutboxRecord.command_json))).scalar_one()
        for private in (
            SENDER["open_id"],
            MESSAGE["message_id"],
            CHALLENGE,
            client.sent_cards[0]["message_id"],
            callback_for(client)["value"]["nonce"],
        ):
            assert private not in report + command_json
    finally:
        await db.close()
    with pytest.raises(FileExistsError):
        L3ApprovalAcceptance(CHALLENGE, client, tmp_path / "feishu-l3-offline")


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("operator", "wrong_operator"),
        ("message", "wrong_card"),
        ("missing_operator", "wrong_operator"),
        ("missing_message", "wrong_card"),
        ("run_id", "wrong_approval"),
        ("approval_id", "wrong_approval"),
        ("nonce", "wrong_nonce"),
        ("unicode_nonce", "wrong_nonce"),
        ("bad_nonce_type", "wrong_nonce"),
        ("extra", "malformed_action"),
        ("malformed", "malformed_action"),
        ("decision", "unsupported_decision"),
    ],
)
async def test_callback_identity_and_card_binding(tmp_path, fault, reason):
    probe, client = make_probe(tmp_path)
    await probe.handle_message(MESSAGE, SENDER)
    callback = callback_for(client)
    if fault == "operator":
        callback["operator_id"] = "ou_attacker"
    elif fault == "message":
        callback["message_id"] = "om_other_card"
    elif fault == "missing_operator":
        callback.pop("operator_id")
    elif fault == "missing_message":
        callback.pop("message_id")
    elif fault in {"run_id", "approval_id", "nonce"}:
        callback["value"][fault] = "wrong"
    elif fault == "bad_nonce_type":
        callback["value"]["nonce"] = None
    elif fault == "unicode_nonce":
        callback["value"]["nonce"] = "错误的随机标识"
    elif fault == "extra":
        callback["value"]["operator_id"] = SENDER["open_id"]
    elif fault == "malformed":
        callback["value"] = []
    elif fault == "decision":
        callback["value"]["action"] = "update_task"
    assert await probe.handle_card(callback) is None
    assert probe.snapshot()["accepted_callbacks"] == 0
    assert probe.snapshot()["filtered"][reason] == 1
    assert (await probe.handle_card(callback_for(client)))["toast"]
    assert len(client.sent_cards) == 1
    probe.close()


async def test_l3_filters_challenges_and_late_or_early_callbacks(tmp_path):
    probe, client = make_probe(tmp_path)
    assert await probe.handle_card({}) is None
    await probe.handle_message({**MESSAGE, "chat_type": "group"}, SENDER)
    await probe.handle_message({**MESSAGE, "content": '{"text":"wrong"}'}, SENDER)
    await probe.handle_message(MESSAGE, {**SENDER, "open_id": "oc_group"})
    assert client.sent_cards == []
    assert probe.snapshot()["filtered"] == {
        "card_not_ready": 1,
        "non_private": 1,
        "challenge_mismatch": 1,
        "missing_open_id": 1,
    }
    await probe.handle_message(MESSAGE, SENDER)
    probe._deadline = 0
    assert await probe.handle_card(callback_for(client)) is None
    probe.close()
    assert probe.snapshot()["approval_status"] == "expired"
    assert probe.snapshot()["accepted_callbacks"] == 0


@pytest.mark.parametrize("fault", ["timeout", "bad_response", "cancel"])
async def test_uncertain_card_is_held_and_never_retried(tmp_path, fault):
    entered = asyncio.Event()

    class Client(MockFeishuClient):
        async def send_card(self, **kwargs):
            response = await super().send_card(**kwargs)
            before = await rows_for(probe)
            assert before[0]["status"] == "held" and before[0]["attempts"] == 1
            entered.set()
            if fault == "timeout":
                raise TimeoutError("private_exception")
            if fault == "bad_response":
                return {"code": 0}
            await asyncio.Event().wait()
            return response

    probe, client = make_probe(tmp_path, Client())
    task = asyncio.create_task(probe.handle_message(MESSAGE, SENDER))
    await entered.wait()
    if fault == "cancel":
        probe.close()
    await task
    await probe.handle_message(MESSAGE, SENDER)
    assert probe.snapshot()["reply_status"] == "unknown"
    assert probe.snapshot()["outbox_status"] == "held"
    assert len(client.sent_cards) == 1
    assert (await rows_for(probe))[0]["status"] == "held"
    assert "private_exception" not in json.dumps(probe.snapshot())
    db = Database(f"sqlite+aiosqlite:///{probe.db_path.as_posix()}")
    try:
        store = SqlOutboxStore(db)
        assert not await store.reserve_once(probe.run_id, f"{probe.run_id}:card", datetime.now(UTC))
        assert await store.due_commands(probe.run_id, datetime.now(UTC)) == []
        assert await store.due_projects(datetime.now(UTC)) == []
    finally:
        await db.close()


@pytest.mark.parametrize(
    "stage", ["receipt", "pre_send", "callback", "enqueue", "reserve", "settle"]
)
async def test_audit_and_database_failures_are_fail_closed(tmp_path, monkeypatch, stage):
    probe, client = make_probe(tmp_path)
    original = AcceptanceWindow._persist

    def fail_at_stage(window):
        state = window.snapshot()
        if (
            stage == "receipt"
            or (stage == "pre_send" and state["reply_status"] == "in_flight")
            or (stage == "callback" and state["accepted_callbacks"] == 1)
        ):
            raise OSError("private_exception")
        return original(window)

    async def database_failure(*args, **kwargs):
        raise RuntimeError("private_exception")

    monkeypatch.setattr(AcceptanceWindow, "_persist", fail_at_stage)
    if stage in {"enqueue", "reserve", "settle"}:
        monkeypatch.setattr(
            SqlOutboxStore, "reserve_once" if stage == "reserve" else stage, database_failure
        )
    await probe.handle_message(MESSAGE, SENDER)
    if stage == "callback":
        assert await probe.handle_card(callback_for(client)) is None
        assert probe.snapshot()["approval_status"] == "unknown"
    assert probe.snapshot()["window_closed"] is True
    assert len(client.sent_cards) == (1 if stage in {"callback", "settle"} else 0)
    assert "private_exception" not in json.dumps(probe.snapshot())


async def test_outbox_once_reservation_is_atomic_and_survives_reopen(tmp_path):
    probe, _client = make_probe(tmp_path)
    probe._actor_id = SENDER["open_id"]
    command, _card = probe._command_and_card()
    db = Database(f"sqlite+aiosqlite:///{probe.db_path.as_posix()}")
    await db.init_db()
    store = SqlOutboxStore(db)
    await store.enqueue(probe.run_id, command, datetime.now(UTC))
    results = await asyncio.gather(
        *[
            store.reserve_once(probe.run_id, command.idempotency_key, datetime.now(UTC))
            for _ in range(4)
        ]
    )
    assert results.count(True) == 1
    assert await store.due_commands(probe.run_id, datetime.now(UTC)) == []
    assert await store.pending_count(probe.run_id) == 1
    await db.close()
    assert (await rows_for(probe))[0]["attempts"] == 1
    probe.close()


@pytest.mark.parametrize("close_window", [False, True])
async def test_l3_expiration_or_close_before_reserved_send_prevents_io(
    tmp_path, monkeypatch, close_window
):
    probe, client = make_probe(tmp_path)
    original = SqlOutboxStore.reserve_once

    async def pause_before_send(store, *args):
        reserved = await original(store, *args)
        if close_window:
            # Simulate main-thread closure without cancelling this test task.
            probe._closed = True
        else:
            probe._deadline = 0
        return reserved

    monkeypatch.setattr(SqlOutboxStore, "reserve_once", pause_before_send)
    await probe.handle_message(MESSAGE, SENDER)
    assert probe.snapshot()["send_attempts"] == 0
    assert probe.snapshot()["reply_status"] == "not_attempted"
    assert probe.snapshot()["window_closed"]
    assert not client.sent_cards
    assert (await rows_for(probe))[0]["status"] == "held"

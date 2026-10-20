"""Safety and redacted evidence for the isolated one-shot Feishu transport test."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from orgpilot.feishu.acceptance import L2_REPLY, L2Acceptance
from orgpilot.feishu.client import MockFeishuClient
from orgpilot.feishu.ws import _SafeSdkLogFilter

CHALLENGE = "告诉 Alice 明天必须完成：this-is-only-an-opaque-challenge"


def _message(**updates) -> dict:
    return {
        "message_type": "text",
        "chat_type": "p2p",
        "message_id": "om_private_inbound",
        "content": json.dumps({"text": CHALLENGE}),
        **updates,
    }


def _sender(**updates) -> dict:
    return {"open_id": "ou_private_sender", "sender_type": "user", **updates}


@pytest.mark.parametrize(
    ("message", "sender", "reason"),
    [
        (_message(message_type="image"), _sender(), "non_text"),
        (_message(chat_type="group"), _sender(), "non_private"),
        (_message(chat_type=None), _sender(), "non_private"),
        (_message(), _sender(sender_type="app"), "non_user"),
        (_message(), _sender(open_id=None, user_id="user_fallback"), "missing_open_id"),
        (_message(), _sender(open_id="oc_group"), "missing_open_id"),
        (_message(message_id=None), _sender(), "missing_message_id"),
        (_message(content="not json"), _sender(), "malformed_content"),
        (_message(content="[]"), _sender(), "malformed_content"),
        (_message(content="null"), _sender(), "malformed_content"),
        (_message(content='{"text": 123}'), _sender(), "malformed_content"),
        (_message(content='{"text": "wrong"}'), _sender(), "challenge_mismatch"),
    ],
)
async def test_l2_filters_before_any_send(message: dict, sender: dict, reason: str) -> None:
    client = MockFeishuClient()
    acceptance = L2Acceptance(CHALLENGE, client)
    acceptance.arm(30)
    await acceptance.handle_message(message, sender)
    assert acceptance.snapshot()["filtered"] == {reason: 1}
    assert acceptance.snapshot()["received_messages"] == 1
    assert client.sent_messages == []


async def test_l2_receipt_precedes_single_fixed_reply_and_never_runs_business(
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipt.json"

    class AuditedClient(MockFeishuClient):
        async def send_message(self, **kwargs) -> dict:
            before = json.loads(path.read_text(encoding="utf-8"))
            assert before["matched_messages"] == 1
            assert before["send_attempts"] == 1
            assert before["inbound_message_fingerprint"]
            return await super().send_message(**kwargs)

    client = AuditedClient()
    acceptance = L2Acceptance(CHALLENGE, client, path)
    acceptance.arm(30)
    await asyncio.gather(
        acceptance.handle_message(_message(), _sender()),
        acceptance.handle_message(_message(message_id="om_second"), _sender(open_id="ou_other")),
    )
    acceptance.ignore_card()
    acceptance.close()
    acceptance.record_shutdown(True)

    assert len(client.sent_messages) == 1
    assert client.sent_messages[0]["content"] == {"text": L2_REPLY}
    assert client.sent_messages[0]["receive_id"] == "ou_private_sender"
    assert client.sent_cards == [] and client.updated_tasks == []
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["reply_status"] == "api_accepted"
    assert report["user_visible_confirmed"] is False
    assert report["listener_stopped"] is True
    assert report["filtered"]["card_action"] == 1
    for private_text in (CHALLENGE, "ou_private_sender", "om_private_inbound", "om_mock_1"):
        assert private_text not in path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        L2Acceptance(CHALLENGE, client, path)


async def test_l2_not_ready_expired_and_closed_windows_do_not_send() -> None:
    client = MockFeishuClient()
    acceptance = L2Acceptance(CHALLENGE, client)
    await acceptance.handle_message(_message(), _sender())
    acceptance.arm(0)
    await acceptance.handle_message(_message(), _sender())
    result = acceptance.wait(0)
    await acceptance.handle_message(_message(), _sender())
    assert result["send_attempts"] == 0
    assert acceptance.snapshot()["filtered"] == {"not_ready": 1, "expired": 1, "window_closed": 1}
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        acceptance.arm(30)
    assert client.sent_messages == []


async def test_l2_close_during_request_is_unknown_and_duplicates_never_retry() -> None:
    entered = asyncio.Event()

    class SlowClient(MockFeishuClient):
        async def send_message(self, **kwargs) -> dict:
            await super().send_message(**kwargs)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    client = SlowClient()
    acceptance = L2Acceptance(CHALLENGE, client)
    acceptance.arm(30)
    task = asyncio.create_task(acceptance.handle_message(_message(), _sender()))
    await entered.wait()
    await acceptance.handle_message(_message(), _sender())
    assert acceptance.snapshot()["filtered"]["duplicate_or_extra"] == 1
    acceptance.close()
    await task
    await acceptance.handle_message(_message(), _sender())
    assert acceptance.snapshot()["reply_status"] == "unknown"
    assert acceptance.snapshot()["send_attempts"] == 1
    assert len(client.sent_messages) == 1


@pytest.mark.parametrize("response", [{"code": 0}, {"code": 99, "msg": "private detail"}])
async def test_l2_requires_api_receipt_not_just_no_exception(response: dict) -> None:
    class UnconfirmedClient(MockFeishuClient):
        async def send_message(self, **kwargs) -> dict:
            return response

    acceptance = L2Acceptance(CHALLENGE, UnconfirmedClient())
    acceptance.arm(30)
    await acceptance.handle_message(_message(), _sender())
    result = acceptance.wait(0)
    assert result["reply_status"] == "unknown"
    assert result["send_attempts"] == 1
    assert "private detail" not in json.dumps(result)


async def test_l2_audit_failure_blocks_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MockFeishuClient()
    acceptance = L2Acceptance(CHALLENGE, client, tmp_path / "receipt.json")
    acceptance.arm(30)

    def unavailable() -> None:
        raise OSError("private path must not leak")

    monkeypatch.setattr(acceptance, "_persist", unavailable)
    await acceptance.handle_message(_message(), _sender())
    acceptance.close()
    acceptance.record_shutdown(True)
    result = acceptance.wait(0)
    assert result["reply_status"] == "not_attempted"
    assert result["audit_error"] == "OSError"
    assert "private path" not in json.dumps(result)
    assert client.sent_messages == []


def test_sdk_error_filter_removes_url_exception_and_traceback() -> None:
    record = logging.LogRecord(
        "Lark",
        logging.ERROR,
        __file__,
        1,
        "failed %s",
        ("wss://host/?access_key=secret&ticket=hidden",),
        None,
    )
    record.exc_text = "exception with secret"
    record.stack_info = "stack with secret"
    assert _SafeSdkLogFilter().filter(record)
    assert record.getMessage() == "Feishu SDK log (details suppressed)"
    assert record.exc_text is None and record.stack_info is None

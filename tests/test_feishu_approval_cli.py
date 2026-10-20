"""Offline L3 CLI and SDK callback tests. Real write clients are replaced before invocation."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from orgpilot import cli
from orgpilot.feishu import approval_cli
from orgpilot.feishu.acceptance import AcceptanceWindow
from orgpilot.feishu.approval_acceptance import L3ApprovalAcceptance
from orgpilot.feishu.ws import FeishuWebSocketListener
from tests.test_feishu_approval_acceptance import MESSAGE, SENDER, callback_for, make_probe


async def test_l3_sdk_callback_uses_platform_operator_and_message_context(tmp_path):
    probe, client = make_probe(tmp_path)
    await probe.handle_message(MESSAGE, SENDER)
    callback = callback_for(client)
    listener = FeishuWebSocketListener("cli_offline", "unused", acceptance=probe)
    data = P2CardActionTrigger(
        {
            "event": {
                "operator": {"open_id": callback["operator_id"]},
                "context": {"open_message_id": callback["message_id"]},
                "action": {"value": callback["value"]},
            }
        }
    )
    response = await listener.handle_card_action_async(data)
    assert response.toast.type == "success"
    assert response.card is None
    assert listener.webhook_handler is None
    assert await listener.handle_card_action_async(data) is None
    assert len(client.sent_cards) == 1
    probe.close()


def test_l3_preview_requires_no_settings_network_or_runtime_directory(
    tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("preview must not load runtime settings or a client")

    monkeypatch.setattr(approval_cli.OrgPilotSettings, "from_env", forbidden)
    monkeypatch.setattr(approval_cli, "AsyncFeishuClient", forbidden)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["orgpilot", "feishu-l3-approval", "--preview"])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "同意测试" in output and "拒绝测试" in output and "不创建或修改任何任务" in output
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("approved", 0),
        ("rejected", 0),
        ("no_callback", 1),
        ("no_challenge", 1),
        ("connection_failure", 1),
        ("send_failure", 1),
        ("shutdown_failure", 1),
        ("shutdown_exception", 1),
    ],
)
def test_l3_cli_isolated_lifecycle(tmp_path, monkeypatch, capsys, mode, expected):
    from orgpilot.feishu.client import MockFeishuClient

    settings = SimpleNamespace(
        collaboration_adapter="feishu",
        feishu_use_ws=True,
        feishu_allow_writes=False,
        demo_bootstrap=False,
        feishu_app_id="cli_secret_canary",
        feishu_app_secret="private_canary",
    )
    monkeypatch.setattr(approval_cli.OrgPilotSettings, "from_env", lambda: settings)
    monkeypatch.chdir(tmp_path)
    seen = {}

    class Client(MockFeishuClient):
        def __init__(self, *args, **kwargs):
            super().__init__()
            assert kwargs["allow_writes"] is True
            seen["client"] = self

        async def send_card(self, **kwargs):
            response = await super().send_card(**kwargs)
            if mode == "send_failure":
                raise TimeoutError("private_canary")
            return response

    class Listener:
        connection_error_type = "OSError"

        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

        def start(self):
            seen["started"] = True

        def wait_until_connected(self, timeout):
            return mode != "connection_failure"

        def stop(self):
            seen["stopped"] = True
            if mode == "shutdown_exception":
                raise RuntimeError("private_canary")
            return mode != "shutdown_failure"

    original_wait = AcceptanceWindow.wait

    def deliver(probe, timeout):
        async def exercise():
            if mode == "no_challenge":
                return
            await probe.handle_message(
                {**MESSAGE, "content": json.dumps({"text": probe.challenge})}, SENDER
            )
            if mode not in {"no_callback", "send_failure"}:
                await probe.handle_card(
                    callback_for(seen["client"], "rejected" if mode == "rejected" else "approved")
                )

        asyncio.run(exercise())
        return original_wait(probe, 0)

    monkeypatch.setattr(approval_cli, "AsyncFeishuClient", Client)
    monkeypatch.setattr(approval_cli, "FeishuWebSocketListener", Listener)
    monkeypatch.setattr(L3ApprovalAcceptance, "wait", deliver)
    monkeypatch.setattr(
        sys,
        "argv",
        ["orgpilot", "feishu-l3-approval", "--run-id", "feishu-l3-cli", "--confirm-single-card"],
    )
    assert cli.main() == expected
    assert seen["stopped"]
    assert "gateway_service" not in seen
    report_text = (tmp_path / "var/feishu-acceptance/feishu-l3-cli/report.json").read_text(
        encoding="utf-8"
    )
    report = json.loads(report_text)
    assert report["window_closed"]
    assert not report["approval_consumed"] and not report["user_visible_confirmed"]
    assert not (tmp_path / "orgpilot.db").exists()
    assert len(seen["client"].sent_cards) <= 1 and not seen["client"].updated_tasks
    output = capsys.readouterr().out
    for private in (
        "private_canary",
        "cli_secret_canary",
        SENDER["open_id"],
        MESSAGE["message_id"],
    ):
        assert private not in report_text + output
    with pytest.raises(SystemExit, match="already exists"):
        cli.main()


@pytest.mark.parametrize(
    "run_id,timeout,confirmed,error",
    [
        ("feishu-l3-test", 30, False, "confirm-single-card"),
        (None, 30, True, "run-id"),
        ("feishu-l3-../../escape", 30, True, "run-id"),
        ("feishu-l3-test", 301, True, "between 30 and 300"),
    ],
)
def test_l3_cli_rejects_invalid_scope(run_id, timeout, confirmed, error):
    with pytest.raises(SystemExit, match=error):
        approval_cli.run_approval_probe(run_id, timeout, confirmed, False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("collaboration_adapter", "mock"),
        ("feishu_use_ws", False),
        ("feishu_app_id", ""),
        ("feishu_app_secret", ""),
        ("demo_bootstrap", True),
        ("feishu_allow_writes", True),
    ],
)
def test_l3_cli_rejects_unsafe_settings(tmp_path, monkeypatch, field, value):
    settings = SimpleNamespace(
        collaboration_adapter="feishu",
        feishu_use_ws=True,
        feishu_app_id="cli_offline",
        feishu_app_secret="unused",
        demo_bootstrap=False,
        feishu_allow_writes=False,
    )
    setattr(settings, field, value)
    monkeypatch.setattr(approval_cli.OrgPilotSettings, "from_env", lambda: settings)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        approval_cli.run_approval_probe("feishu-l3-test", 30, True, False)
    assert not list(tmp_path.iterdir())

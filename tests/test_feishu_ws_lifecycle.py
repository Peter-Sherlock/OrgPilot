"""Real SDK lifecycle regression with external networking blocked (Windows self-pipes allowed)."""

import asyncio
import gc
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    "mode",
    [
        "connected",
        "connect_failure",
        "constructor_failure",
        "disconnect_failure",
        "immediate_stop",
        "connect_transition_stop",
        "caller_loop",
    ],
)
def test_real_sdk_lifecycle_has_no_orphaned_tasks(mode: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-u", str(Path(__file__).resolve()), mode],
        cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SDK_LIFECYCLE_OK" in result.stdout
    for forbidden in (
        "Event loop is closed",
        "Task was destroyed",
        "Exception ignored",
        "never awaited",
        "offline_secret_canary",
    ):
        assert forbidden not in result.stdout + result.stderr
    if mode in {"connected", "caller_loop"}:
        assert "[ERROR]" not in result.stdout + result.stderr
        assert "Feishu WS asynchronous failure" not in result.stderr


def _exercise_sdk(mode: str) -> None:
    import httpx
    import lark_oapi.ws.client as sdk
    import requests
    from websockets.exceptions import ConnectionClosedOK
    from websockets.frames import Close

    from orgpilot.feishu.acceptance import L2Acceptance
    from orgpilot.feishu.client import MockFeishuClient
    from orgpilot.feishu.ws import FeishuWebSocketListener

    blocked_requests = []

    # These synthetic credentials never leave the process. Do not load settings/.env.
    def network_forbidden(*_args, **_kwargs):
        blocked_requests.append(True)
        raise AssertionError("Network is forbidden in SDK lifecycle regression")

    original_connect = socket.socket.connect

    def self_pipe_only(sock, address):
        # Windows asyncio creates socketpair() via loopback; it is not an API request.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return network_forbidden()

    class LocalConnection:
        def __init__(self) -> None:
            self.closed = asyncio.Event()
            self.close_calls = 0

        async def recv(self):
            await self.closed.wait()
            raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)

        async def send(self, _data):
            pass  # SDK ping frame, never a business send.

        async def close(self):
            self.close_calls += 1
            if mode == "disconnect_failure":
                raise OSError("offline_secret_canary")
            self.closed.set()
            await asyncio.sleep(0)

    connections = []

    async def connect_locally(*_args, **_kwargs):
        if mode == "connect_failure":
            raise OSError("offline_secret_canary")
        connection = LocalConnection()
        connections.append(connection)
        return connection

    previous_sdk_loop = sdk.loop
    created_clients = []
    original_init = sdk.Client.__init__
    original_sdk_connect = sdk.Client._connect
    active_listener = None
    transition_stopped = threading.Event()

    async def connect_then_stop_at_boundary(client):
        await original_sdk_connect(client)
        if mode == "connect_transition_stop":
            # Stop in the final tick of run_until_complete(_connect()), immediately
            # before the real SDK starts its second run_until_complete(_select()).
            def stop_at_boundary():
                active_listener._stopping.set()
                active_listener._request_sdk_stop()
                transition_stopped.set()

            asyncio.get_running_loop().call_soon(stop_at_boundary)

    def tracked_init(client, *args, **kwargs):
        original_init(client, *args, **kwargs)
        created_clients.append(client)
        if mode == "constructor_failure":
            raise ValueError("offline_secret_canary")

    def exercise_listener():
        nonlocal active_listener
        connections.clear()
        acceptance = L2Acceptance("offline-long-challenge", MockFeishuClient())
        listener = FeishuWebSocketListener(
            "cli_offline", "offline_secret_canary", acceptance=acceptance
        )
        active_listener = listener
        try:
            if mode == "constructor_failure":
                with pytest.raises(RuntimeError, match="initialization failed"):
                    listener.start()
                client = created_clients[-1]
                assert listener.connection_error_type == "ValueError"
            else:
                client = listener.start()
                if mode == "connect_transition_stop":
                    assert transition_stopped.wait(2.0)
                elif mode != "immediate_stop":
                    assert listener.wait_until_connected(2.0) is (mode != "connect_failure")
            assert client._cache._cron.get_loop() is listener._ws_loop, (
                "cache must belong to SDK loop"
            )
        finally:
            assert listener.stop() is (mode != "disconnect_failure")
        assert listener.stop() is (mode != "disconnect_failure"), "stop must be idempotent"
        assert client._cache._cron.done(), "cache cancellation must be awaited before loop close"
        assert not listener._ws_thread.is_alive()
        assert listener._ws_loop.is_closed()
        if mode == "caller_loop":
            assert listener._loop_thread is None
            assert listener._loop is asyncio.get_running_loop()
            assert not listener._loop.is_closed()
        else:
            assert not listener._loop_thread.is_alive()
            assert listener._loop.is_closed()
        assert sdk.loop is previous_sdk_loop
        assert acceptance.snapshot()["send_attempts"] == 0
        if connections:
            assert connections[0].close_calls == 1
        if mode == "connect_failure":
            assert listener.connection_error_type == "OSError"
        del client, listener
        active_listener = None
        created_clients.clear()
        gc.collect()

    async def exercise_caller_loop():
        sentinel = asyncio.create_task(asyncio.sleep(0, result=42))
        exercise_listener()
        assert not sentinel.cancelled(), "caller-owned tasks must survive listener shutdown"
        assert await sentinel == 42

    with (
        patch.object(socket.socket, "connect", self_pipe_only),
        patch.object(socket, "create_connection", network_forbidden),
        patch.object(requests.sessions.Session, "request", network_forbidden),
        patch.object(httpx.AsyncClient, "request", network_forbidden),
        patch.object(sdk.Client, "__init__", tracked_init),
        patch.object(sdk.Client, "_connect", connect_then_stop_at_boundary),
        patch.object(
            sdk.Client,
            "_get_conn_url",
            return_value="wss://offline.invalid/?device_id=test&service_id=1",
        ),
        patch.object(sdk.websockets, "connect", connect_locally),
    ):
        if mode == "caller_loop":
            asyncio.run(exercise_caller_loop())
        else:
            # Repeat the transition case without repeated interpreter startup to
            # exercise sequential ownership restoration and the stop boundary.
            for _ in range(5 if mode == "connect_transition_stop" else 1):
                exercise_listener()
        assert not blocked_requests, "no external networking should even be attempted"
    # The SDK created this idle loop on import; the listener must not take ownership of it.
    previous_sdk_loop.close()
    asyncio.set_event_loop(None)
    gc.collect()
    print("SDK_LIFECYCLE_OK", flush=True)


if __name__ == "__main__":
    _exercise_sdk(sys.argv[1])

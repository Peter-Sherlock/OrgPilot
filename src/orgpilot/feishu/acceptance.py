"""Bounded acceptance window and the one-shot L2 text transport test."""

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orgpilot.feishu.client import FeishuClient

L2_REPLY = "OrgPilot L2 连通性测试已收到。本次仅回复这一条测试消息，未执行任务或审批操作。"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class AcceptanceWindow:
    """Shared deadline, exact private challenge, and durable redacted audit lifecycle."""

    def __init__(
        self,
        challenge: str,
        client: FeishuClient,
        report_path: Path | None = None,
    ) -> None:
        self.challenge = challenge
        self._client = client
        self._report_path = report_path
        self._lock = threading.RLock()
        self._complete = threading.Event()
        self._deadline: float | None = None
        self._closed = False
        self._claimed = False
        self._task: asyncio.Task | None = None
        self._filtered: Counter[str] = Counter()
        self._state: dict[str, Any] = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "connected": False,
            "received_messages": 0,
            "matched_messages": 0,
            "send_attempts": 0,
            "reply_status": "not_attempted",
            "window_closed": False,
            "user_visible_confirmed": False,
        }
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            # An existing run can never be silently reused after a crash/uncertain send.
            with report_path.open("x", encoding="utf-8") as report:
                report.write(json.dumps(self.snapshot(), ensure_ascii=False, indent=2))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._state, "filtered": dict(self._filtered)}

    def _persist(self) -> None:
        if self._report_path is None:
            return
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._report_path.parent, delete=False
            ) as report:
                temporary = Path(report.name)
                json.dump(self.snapshot(), report, ensure_ascii=False, indent=2)
                report.flush()
                os.fsync(report.fileno())
            os.replace(temporary, self._report_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def arm(self, timeout_seconds: float) -> None:
        with self._lock:
            if self._closed or self._deadline is not None:
                raise RuntimeError("L2 window cannot be reopened")
            self._deadline = time.monotonic() + timeout_seconds
            self._state["connected"] = True
            self._persist()

    def ignore_card(self) -> None:
        with self._lock:
            self._filtered["card_action"] += 1

    async def handle_message(self, message: dict[str, Any], sender: dict[str, Any]) -> None:
        raise NotImplementedError

    async def handle_card(self, callback: dict[str, Any]) -> dict[str, Any] | None:
        self.ignore_card()
        return None

    def _filter_reason(self, message: dict[str, Any], sender: dict[str, Any]) -> str | None:
        if self._closed:
            return "window_closed"
        if self._deadline is None:
            return "not_ready"
        if time.monotonic() >= self._deadline:
            return "expired"
        if self._claimed:
            return "duplicate_or_extra"
        if message.get("message_type") != "text":
            return "non_text"
        if message.get("chat_type") != "p2p":
            return "non_private"
        if sender.get("sender_type") != "user":
            return "non_user"
        actor_id = sender.get("open_id")
        if not isinstance(actor_id, str) or not actor_id.startswith("ou_"):
            return "missing_open_id"
        if not isinstance(message.get("message_id"), str) or not message["message_id"]:
            return "missing_message_id"
        try:
            content = json.loads(message.get("content") or "")
        except (TypeError, ValueError):
            return "malformed_content"
        if not isinstance(content, dict) or not isinstance(content.get("text"), str):
            return "malformed_content"
        if content["text"] != self.challenge:
            return "challenge_mismatch"
        return None

    def wait(self, timeout_seconds: float) -> dict[str, Any]:
        if not self._complete.wait(timeout_seconds):
            self.close()
        return self.snapshot()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._state["window_closed"] = True
            if self._state["reply_status"] == "in_flight":
                self._state["reply_status"] = "unknown"
            task = self._task
            if task is not None and not task.done():
                task.get_loop().call_soon_threadsafe(task.cancel)
            try:
                self._persist()
            except OSError as exc:
                self._state["audit_error"] = type(exc).__name__
            self._complete.set()

    def record_shutdown(self, stopped: bool) -> None:
        with self._lock:
            self._state["listener_stopped"] = stopped
            try:
                self._persist()
            except OSError as exc:
                self._state["audit_error"] = type(exc).__name__


class L2Acceptance(AcceptanceWindow):
    """One fixed text attempt; no GatewayService, task adapter, or model dependency."""

    async def handle_message(self, message: dict[str, Any], sender: dict[str, Any]) -> None:
        with self._lock:
            self._state["received_messages"] += 1
            reason = self._filter_reason(message, sender)
            if reason:
                self._filtered[reason] += 1
                return
            self._claimed = True
            self._task = asyncio.current_task()
            actor_id = sender["open_id"]
            self._state.update(
                matched_messages=1,
                actor_fingerprint=_fingerprint(actor_id),
                inbound_message_fingerprint=_fingerprint(message["message_id"]),
            )
            # Receipt and consumed send budget must be durable before the request starts.
            try:
                self._persist()
                self._state.update(send_attempts=1, reply_status="in_flight")
                self._persist()
            except OSError as exc:
                self._state.update(
                    send_attempts=0, reply_status="not_attempted", audit_error=type(exc).__name__
                )
                self._closed = True
                self._task = None
                self._state["window_closed"] = True
                self._complete.set()
                return
            remaining = max(0.0, self._deadline - time.monotonic())
            if remaining == 0:
                self._state.update(send_attempts=0, reply_status="not_attempted")
                self._task = None
                self.close()
                self._complete.set()
                return

        try:
            async with asyncio.timeout(min(15.0, remaining)):
                response = await self._client.send_message(
                    receive_id=actor_id,
                    receive_id_type="open_id",
                    msg_type="text",
                    content={"text": L2_REPLY},
                )
            data = response.get("data")
            message_id = data.get("message_id") if isinstance(data, dict) else None
            if response.get("code") != 0 or not isinstance(message_id, str) or not message_id:
                raise ValueError("L2 send response did not confirm API acceptance")
            with self._lock:
                self._state.update(
                    reply_status="api_accepted",
                    outbound_message_fingerprint=_fingerprint(message_id),
                )
        except (Exception, asyncio.CancelledError) as exc:
            with self._lock:
                # A timeout/error can occur after server acceptance. Never auto-resend.
                self._state.update(reply_status="unknown", error_type=type(exc).__name__)
        finally:
            with self._lock:
                self._closed = True
                self._task = None
                self._state["window_closed"] = True
                try:
                    self._persist()
                except OSError as exc:
                    self._state["audit_error"] = type(exc).__name__
                self._complete.set()

"""L3-A: one test approval card, bound callback, isolated approval state and outbox."""

import asyncio
import hmac
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orgpilot.coordination.approval import ApprovalManager
from orgpilot.domain.enums import ActionType, ApprovalStatus
from orgpilot.domain.models import ActionCommand, CoordinationAction
from orgpilot.feishu.acceptance import AcceptanceWindow, _fingerprint
from orgpilot.feishu.client import FeishuClient
from orgpilot.storage.database import Database
from orgpilot.storage.outbox_store import SqlOutboxStore

L3_TITLE = "OrgPilot L3 审批链路测试"
L3_DESCRIPTION = (
    "请选择“同意测试”或“拒绝测试”。本次只记录审批选择，不创建或修改任何任务，"
    "不触发催办，也不会再次发送消息。按钮在本轮验收窗口结束后失效。"
)


def build_test_approval_card(run_id: str, approval_id: str, nonce: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": L3_TITLE}, "template": "blue"},
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": L3_DESCRIPTION}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": run_id}]},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": label},
                        "type": style,
                        "value": {
                            "run_id": run_id,
                            "approval_id": approval_id,
                            "nonce": nonce,
                            "action": action,
                        },
                    }
                    for action, label, style in (
                        ("approved", "同意测试", "primary"),
                        ("rejected", "拒绝测试", "default"),
                    )
                ],
            },
        ],
    }


class L3ApprovalAcceptance(AcceptanceWindow):
    """Reuses ApprovalManager and SQL outbox; never consumes/executes an approval.

    A fresh directory prevents resuming uncertain runs. The outbox contains only
    synthetic targets/payloads; real IDs and callback nonce stay in memory.
    """

    def __init__(self, challenge: str, client: FeishuClient, run_dir: Path) -> None:
        self.run_id = run_dir.name
        if not re.fullmatch(r"feishu-l3-[a-zA-Z0-9_-]{1,64}", self.run_id):
            raise ValueError("L3 run ID must start with 'feishu-l3-' and use a safe unique suffix")
        run_dir.mkdir(parents=True, exist_ok=False)
        super().__init__(challenge, client, run_dir / "report.json")
        self.db_path = run_dir / "outbox.db"
        self.approvals = ApprovalManager()
        self._actor_id: str | None = None
        self._outbound_id: str | None = None
        self._approval_id: str | None = None
        self._nonce = secrets.token_urlsafe(24)
        self._state.update(
            mode="l3_approval",
            received_callbacks=0,
            accepted_callbacks=0,
            approval_status="not_created",
            approval_consumed=False,
            outbox_status="not_created",
            callback_response_confirmed=False,
        )
        self._persist()

    def _persist(self) -> None:
        try:
            super()._persist()
        except OSError as exc:
            self._state["audit_error"] = type(exc).__name__
            raise

    def _command_and_card(self) -> tuple[ActionCommand, dict[str, Any]]:
        now = datetime.now(UTC)
        command = ActionCommand(
            command_id=f"{self.run_id}:card",
            action_id="approval-probe",
            action_type=ActionType.PROPOSE_RESCHEDULE,
            targets=("synthetic-l3-approver",),
            payload={"acceptance_only": True, "run_id": self.run_id},
            idempotency_key=f"{self.run_id}:card",
            created_at=now,
        )
        action = CoordinationAction(
            action_id=command.action_id,
            action_type=command.action_type,
            targets=command.targets,
            reason_refs=(),
            expected_effect="Record one isolated test approval choice",
        )
        request = self.approvals.create_request(
            case_id=self.run_id,
            action=action,
            proposed_command=command,
            approver_id=self._actor_id,
            current_time=now,
            expires_at=now + timedelta(seconds=max(0, self._deadline - time.monotonic())),
        )
        self._approval_id = request.approval_id
        self._state["approval_status"] = request.status.value
        return command, build_test_approval_card(self.run_id, request.approval_id, self._nonce)

    async def handle_message(self, message: dict[str, Any], sender: dict[str, Any]) -> None:
        with self._lock:
            self._state["received_messages"] += 1
            reason = self._filter_reason(message, sender)
            if reason:
                self._filtered[reason] += 1
                return
            self._claimed = True
            self._task = asyncio.current_task()
            self._actor_id = sender["open_id"]
            self._state.update(
                matched_messages=1,
                actor_fingerprint=_fingerprint(self._actor_id),
                inbound_message_fingerprint=_fingerprint(message["message_id"]),
            )

        db = Database(f"sqlite+aiosqlite:///{self.db_path.as_posix()}")
        try:
            with self._lock:
                self._persist()
                command, card = self._command_and_card()
            await db.init_db()
            store = SqlOutboxStore(db)
            await store.enqueue(self.run_id, command, datetime.now(UTC))
            if not await store.reserve_once(
                self.run_id, command.idempotency_key, datetime.now(UTC)
            ):
                raise RuntimeError("L3 send budget was already reserved")
            with self._lock:
                self._state["outbox_status"] = "held"
                remaining = self._deadline - time.monotonic()
                if self._closed or remaining <= 0:
                    return
                self._state.update(send_attempts=1, reply_status="in_flight")
                try:
                    self._persist()
                except OSError:
                    self._state.update(send_attempts=0, reply_status="not_attempted")
                    raise
            async with asyncio.timeout(min(15.0, remaining)):
                response = await self._client.send_card(
                    receive_id=self._actor_id, receive_id_type="open_id", card=card
                )
            data = response.get("data")
            message_id = data.get("message_id") if isinstance(data, dict) else None
            if response.get("code") != 0 or not isinstance(message_id, str) or not message_id:
                raise ValueError("L3 card response did not confirm API acceptance")
            with self._lock:
                self._outbound_id = message_id
                self._state.update(
                    reply_status="api_accepted",
                    outbound_message_fingerprint=_fingerprint(message_id),
                )
            now = datetime.now(UTC)
            await store.settle(
                self.run_id,
                command.idempotency_key,
                delivered=True,
                attempts=1,
                error=None,
                retry_at=now,
                now=now,
            )
            with self._lock:
                self._state["outbox_status"] = "delivered"
                self._persist()
        except (Exception, asyncio.CancelledError) as exc:
            with self._lock:
                self._state["error_type"] = type(exc).__name__
                if self._state["reply_status"] == "in_flight":
                    self._state["reply_status"] = "unknown"
        finally:
            try:
                await db.close()
            except Exception as exc:
                with self._lock:
                    self._state["storage_error"] = type(exc).__name__
            with self._lock:
                self._task = None
                if (
                    self._state["outbox_status"] != "delivered"
                    or self._state.get("error_type")
                    or self._state.get("storage_error")
                ):
                    self.close()

    def _callback_filter(self, callback: dict[str, Any]) -> str | None:
        if self._closed:
            return "window_closed"
        if self._deadline is None or time.monotonic() >= self._deadline:
            return "not_ready_or_expired"
        if self._state["outbox_status"] != "delivered" or self._task is not None:
            return "card_not_ready"
        if callback.get("operator_id") != self._actor_id:
            return "wrong_operator"
        if callback.get("message_id") != self._outbound_id:
            return "wrong_card"
        value = callback.get("value")
        if not isinstance(value, dict) or set(value) != {
            "action",
            "run_id",
            "approval_id",
            "nonce",
        }:
            return "malformed_action"
        if value["run_id"] != self.run_id or value["approval_id"] != self._approval_id:
            return "wrong_approval"
        nonce = value["nonce"]
        if not isinstance(nonce, str) or not hmac.compare_digest(
            nonce.encode(), self._nonce.encode()
        ):
            return "wrong_nonce"
        if value["action"] not in ("approved", "rejected"):
            return "unsupported_decision"
        return None

    async def handle_card(self, callback: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self._state["received_callbacks"] += 1
            reason = self._callback_filter(callback)
            if reason:
                self._filtered[reason] += 1
                return None
            decision = callback["value"]["action"]
            try:
                if decision == "approved":
                    request = self.approvals.approve(
                        self._approval_id, self._actor_id, datetime.now(UTC)
                    )
                else:
                    request = self.approvals.reject(
                        self._approval_id,
                        self._actor_id,
                        "User rejected isolated test",
                        datetime.now(UTC),
                    )
                self._state.update(
                    approval_status=request.status.value,
                    approval_consumed=request.consumed,
                    accepted_callbacks=1,
                    window_closed=True,
                )
                self._closed = True
                self._persist()
            except Exception as exc:
                self._state.update(audit_error=type(exc).__name__, approval_status="unknown")
                self.close()
                return None
            # Give the SDK time to return its toast ACK before disconnecting. This
            # grace period is not proof that the response was rendered in Feishu.
            asyncio.get_running_loop().call_later(1.0, self._complete.set)
            label = "同意测试" if decision == "approved" else "拒绝测试"
            return {"toast": {"type": "success", "content": f"已记录：{label}。未执行任务操作。"}}

    def close(self) -> None:
        with self._lock:
            if (
                self._state.get("approval_status") == "pending"
                and self._deadline is not None
                and time.monotonic() >= self._deadline
            ):
                self._state["approval_status"] = "expired"
                request = self.approvals.get_request(self._approval_id)
                if request is not None:
                    request.status = ApprovalStatus.EXPIRED
            super().close()

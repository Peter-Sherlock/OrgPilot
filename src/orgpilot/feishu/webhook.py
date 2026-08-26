"""Feishu Webhook event parser and dispatcher for URL challenge, messages, and card actions."""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from orgpilot.feishu.cards import build_approval_updated_card

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService


class FeishuWebhookHandler:
    """Handles Feishu OpenAPI webhook callbacks and card interactions."""

    def __init__(self, service: Any, project_id: str = "feishu-default") -> None:
        self.service: GatewayService = service
        self.project_id = project_id

    async def handle_event(self, body: dict[str, Any]) -> dict[str, Any]:
        """Dispatches Feishu webhook payload according to event type."""
        # 1. Handle URL verification challenge
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge", "")}

        # 2. Handle card interactive button callbacks (card.action.trigger)
        if "action" in body and "open_message_id" in body:
            return await self._handle_card_action(body)

        # 3. Handle v2 schema event wrapper
        header = body.get("header", {})
        event_type = header.get("event_type")

        if event_type == "im.message.receive_v1":
            ev = body.get("event", {})
            if "create_time" not in ev and "create_time" in header:
                ev["create_time"] = header["create_time"]
            return await self._handle_message_received(ev)

        if event_type == "card.action.trigger":
            return await self._handle_card_action(body.get("event", {}))

        return {"code": 0, "msg": "event ignored"}

    async def _handle_message_received(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Processes an incoming chat message from a team member."""
        message = event_data.get("message", {})
        sender = event_data.get("sender", {}).get("sender_id", {})
        actor_id = sender.get("open_id") or sender.get("user_id") or "feishu_user"

        msg_type = message.get("message_type")
        if msg_type != "text":
            return {"code": 0, "msg": f"unsupported message type {msg_type}"}

        content_str = message.get("content", "{}")
        try:
            content_dict = json.loads(content_str)
            raw_text = content_dict.get("text", "")
        except Exception:
            raw_text = content_str

        occurred_ts = message.get("create_time") or event_data.get("create_time")
        occurred_at = (
            datetime.fromtimestamp(int(occurred_ts) / 1000, tz=UTC)
            if occurred_ts
            else datetime.now(UTC)
        )

        is_act, ext_events, agent, reason, round_num = await self.service.ingest_message(
            project_id=self.project_id,
            message=raw_text,
            actor_id=actor_id,
            occurred_at=occurred_at,
            auto_run_turn=True,
        )

        return {
            "code": 0,
            "msg": "success",
            "data": {
                "is_actionable": is_act,
                "extracted_events_count": len(ext_events),
                "turn_termination_reason": reason,
                "round_number": round_num,
            },
        }

    async def _handle_card_action(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Processes a card action button click and returns in-place updated card JSON."""
        action_val = event_data.get("action", {}).get("value", {})
        decision = action_val.get("action", "approved")
        approval_id = action_val.get("approval_id")

        operator = event_data.get("operator", {}).get("open_id") or "approver"
        now = datetime.now(UTC)

        if not approval_id:
            return {"code": 400, "msg": "missing approval_id in button value"}

        agent = await self.service.get_or_replay_agent(self.project_id)
        req = agent.approval_manager.get_request(approval_id)
        if req is None:
            return {"code": 404, "msg": f"approval request {approval_id} not found"}

        if decision == "approved":
            agent.approval_manager.approve(approval_id, operator, now)
        else:
            agent.approval_manager.reject(approval_id, operator, "declined via card", now)

        # Run turn to execute task update or escalation
        turn_trace, _ = agent.run_turn([], now)
        await self.service.save_agent_state(agent)

        # Build in-place updated card
        task_id = req.proposed_command.payload.get("task_id", "task")
        task_title = req.proposed_command.payload.get("task_title", task_id)
        target_deadline = req.proposed_command.payload.get("proposed_deadline", "目标排期")

        updated_card = build_approval_updated_card(
            task_id=task_id,
            task_title=task_title,
            proposed_deadline_str=str(target_deadline),
            decision=decision,
            approver_name=operator,
            decided_at_str=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

        return {
            "code": 0,
            "msg": "success",
            "card": updated_card,
            "turn_termination_reason": turn_trace.termination_reason.value,
        }

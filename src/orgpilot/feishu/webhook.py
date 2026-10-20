"""Feishu Webhook event parser and dispatcher for URL challenge, messages, and card actions."""

import asyncio
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from orgpilot.domain.enums import WorkflowStatus
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    OrgEvent,
    TaskCreatedEvent,
    TaskCreatedPayload,
)
from orgpilot.feishu.cards import build_approval_updated_card
from orgpilot.intent.replies import WELCOME_TEXT

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService
    from orgpilot.intent.router import MessageRouter

logger = logging.getLogger(__name__)


class FeishuWebhookHandler:
    """Handles Feishu OpenAPI webhook callbacks and card interactions."""

    def __init__(
        self,
        service: Any,
        project_id: str = "feishu-default",
        verification_token: str | None = None,
        router: "MessageRouter | None" = None,
        demo_mode: bool = False,
    ) -> None:
        self.service: GatewayService = service
        self.project_id = project_id
        self.verification_token = verification_token
        self._router = router
        self.demo_mode = demo_mode

    @property
    def router(self) -> "MessageRouter":
        """The layered message router; lazily built with heuristic intents."""
        if self._router is None:
            from orgpilot.intent.router import MessageRouter

            self._router = getattr(self.service, "intent_router", None)
        if self._router is None:
            from orgpilot.intent.router import MessageRouter

            self._router = MessageRouter(service=self.service)
        return self._router

    async def handle_event(self, body: dict[str, Any]) -> dict[str, Any]:
        """Dispatches Feishu webhook payload according to event type."""
        self._verify_callback(body)
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

        # 4. Handle direct unnested message payload
        if "message" in body:
            return await self._handle_message_received(body)

        return {"code": 0, "msg": "event ignored"}

    def _verify_callback(self, body: dict[str, Any]) -> None:
        if not self.verification_token:
            return
        supplied = body.get("token") or body.get("header", {}).get("token") or ""
        if not hmac.compare_digest(str(supplied), self.verification_token):
            raise PermissionError("Invalid Feishu verification token")

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
        # Feishu timestamps are UTC epoch millis; relative phrases like "明天下午5点"
        # must resolve against the team's business-timezone date, not UTC (B5).
        occurred_at = self.service.to_business_timezone(occurred_at)

        # Demo mode only (ADR-0009 §8): the first sender to an EMPTY project becomes
        # its PM with three demo tasks. The welcome text is sent only here (first
        # contact) and from /帮助 (ADR-0008 §4). Real teams import team.yaml.
        bootstrapped = False
        if self.demo_mode and actor_id:
            bootstrapped = await self._bootstrap_demo_project(actor_id, occurred_at)

        chat_type = message.get("chat_type", "p2p")

        if bootstrapped:
            # First-contact welcome (ADR-0008 §4: welcome only at bootstrap / /帮助)
            adapter = self.service.adapter_factory(self.project_id)
            client = getattr(adapter, "client", None)
            if client and hasattr(client, "send_message"):
                try:
                    res = client.send_message(
                        receive_id=actor_id,
                        msg_type="text",
                        content={"text": WELCOME_TEXT},
                    )
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

        # Layers 0-2: idempotency, group refusal, membership, probes, commands,
        # dialogue state, classification, guardrails, and handlers all live in
        # the MessageRouter now (ADR-0008 §3).
        message_id = message.get("message_id") or (
            f"noid-{int(occurred_at.timestamp() * 1000)}-{abs(hash(raw_text)) % 10_000_000}"
        )
        outcome = await self.router.handle_message(
            project_id=self.project_id,
            sender_id=actor_id,
            message_id=message_id,
            text=raw_text,
            occurred_at=occurred_at,
            chat_type=chat_type,
        )
        return self._outcome_response(outcome)

    async def _bootstrap_demo_project(self, actor_id: str, now_ts: datetime) -> bool:
        """Seeds the demo PM and task chain into a project that has no members yet.

        Keyed on "no members" rather than "no tasks": a project whose members were
        imported before its tasks would otherwise get a second member.registered
        for an existing member, an event that fails every later replay. The batch
        goes through the service's validate-then-persist path for the same reason.
        """
        agent = await self.service.get_or_replay_agent(self.project_id)
        if agent.projector.state.members:
            return False

        def demo_task(
            event_id: str,
            task_id: str,
            title: str,
            status: WorkflowStatus,
            days: int,
            dependencies: tuple[str, ...] = (),
        ) -> TaskCreatedEvent:
            return TaskCreatedEvent(
                project_id=self.project_id,
                event_id=event_id,
                event_type="task.created",
                source=EventSource.TASK,
                source_ref="auto-bootstrap",
                occurred_at=now_ts,
                received_at=now_ts,
                payload=TaskCreatedPayload(
                    task_id=task_id,
                    title=title,
                    owner_id=actor_id,
                    workflow_status=status,
                    deadline=now_ts + timedelta(days=days),
                    dependencies=dependencies,
                ),
            )

        bootstrap_events: list[OrgEvent] = [
            MemberRegisteredEvent(
                project_id=self.project_id,
                event_id=f"evt-boot-mem-{actor_id}",
                event_type="member.registered",
                source=EventSource.HUMAN,
                source_ref="auto-bootstrap",
                occurred_at=now_ts,
                received_at=now_ts,
                payload=MemberRegisteredPayload(
                    member_id=actor_id,
                    display_name="项目负责人(您)",
                    role="pm",
                ),
            ),
            demo_task("evt-boot-task-pay", "task-payment", "支付SDK接入", WorkflowStatus.DOING, 2),
            demo_task(
                "evt-boot-task-checkout",
                "task-checkout",
                "收银台前端结账",
                WorkflowStatus.TODO,
                3,
                ("task-payment",),
            ),
            demo_task(
                "evt-boot-task-qa",
                "task-qa",
                "支付全链路压测与验收",
                WorkflowStatus.TODO,
                4,
                ("task-checkout",),
            ),
        ]
        _, _, _, error = await self.service.commit_extracted_events(
            self.project_id,
            bootstrap_events,
            actor_id=actor_id,
            occurred_at=now_ts,
            auto_run_turn=False,
        )
        if error is not None:
            logger.warning("Demo bootstrap rejected for %r: %s", self.project_id, error)
            return False
        return True

    def _outcome_response(self, outcome: Any) -> dict[str, Any]:
        data = dict(outcome.data)
        data.pop("intent_summary", None)
        data.setdefault("reply_category", outcome.reply_category.value)
        data.setdefault("asks_clarification", outcome.asks_clarification)
        return {"code": 0, "msg": outcome.msg, "data": data}

    async def _handle_card_action(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Processes a card action button click and returns in-place updated card JSON."""
        action_val = event_data.get("action", {}).get("value", {})
        decision = action_val.get("action")
        approval_id = action_val.get("approval_id")

        operator = event_data.get("operator", {}).get("open_id")
        now = datetime.now(UTC)

        if decision in {"sync_confirm", "sync_cancel"}:
            return await self.router.handle_sync_card_action(
                project_id=self.project_id,
                action_value=action_val,
                operator_id=operator or "",
                now=now,
            )

        if not approval_id:
            return {"code": 400, "msg": "missing approval_id in button value"}
        if decision not in {"approved", "rejected"}:
            return {"code": 400, "msg": "invalid approval decision"}
        if not operator:
            return {"code": 400, "msg": "missing operator open_id"}

        agent = await self.service.get_or_replay_agent(self.project_id)
        req = agent.approval_manager.get_request(approval_id)
        if req is None:
            return {"code": 404, "msg": f"approval request {approval_id} not found"}
        if req.approver_id != operator:
            return {"code": 403, "msg": f"operator {operator} is not the designated approver"}

        if decision == "approved":
            agent.approval_manager.approve(approval_id, operator, now)
        else:
            agent.approval_manager.reject(approval_id, operator, "declined via card", now)

        # Run turn to execute task update or escalation
        turn_trace, _ = await self.service.run_agent_turn(agent, [], now)

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

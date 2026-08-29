"""Feishu Webhook event parser and dispatcher for URL challenge, messages, and card actions."""

import asyncio
import hmac
import json
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

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService


class FeishuWebhookHandler:
    """Handles Feishu OpenAPI webhook callbacks and card interactions."""

    def __init__(
        self,
        service: Any,
        project_id: str = "feishu-default",
        verification_token: str | None = None,
        demo_bootstrap: bool = False,
    ) -> None:
        self.service: GatewayService = service
        self.project_id = project_id
        self.verification_token = verification_token
        self.demo_bootstrap = demo_bootstrap

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

    async def _should_bootstrap_demo(self, actor_id: str | None) -> bool:
        """Returns True when the opt-in demo bootstrap should inject the starter task chain."""
        if not self.demo_bootstrap or not actor_id:
            return False
        agent_init = await self.service.get_or_replay_agent(self.project_id)
        return not agent_init.projector.state.tasks

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

        # Auto-bootstrap demo tasks for first-time solo tester (opt-in via ORGPILOT_DEMO_BOOTSTRAP)
        if await self._should_bootstrap_demo(actor_id):
            now_ts = occurred_at
            agent_init = await self.service.get_or_replay_agent(self.project_id)
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
                TaskCreatedEvent(
                    project_id=self.project_id,
                    event_id="evt-boot-task-pay",
                    event_type="task.created",
                    source=EventSource.TASK,
                    source_ref="auto-bootstrap",
                    occurred_at=now_ts,
                    received_at=now_ts,
                    payload=TaskCreatedPayload(
                        task_id="task-payment",
                        title="支付SDK接入",
                        owner_id=actor_id,
                        workflow_status=WorkflowStatus.DOING,
                        deadline=now_ts + timedelta(days=2),
                    ),
                ),
                TaskCreatedEvent(
                    project_id=self.project_id,
                    event_id="evt-boot-task-checkout",
                    event_type="task.created",
                    source=EventSource.TASK,
                    source_ref="auto-bootstrap",
                    occurred_at=now_ts,
                    received_at=now_ts,
                    payload=TaskCreatedPayload(
                        task_id="task-checkout",
                        title="收银台前端结账",
                        owner_id=actor_id,
                        workflow_status=WorkflowStatus.TODO,
                        deadline=now_ts + timedelta(days=3),
                        dependencies=("task-payment",),
                    ),
                ),
                TaskCreatedEvent(
                    project_id=self.project_id,
                    event_id="evt-boot-task-qa",
                    event_type="task.created",
                    source=EventSource.TASK,
                    source_ref="auto-bootstrap",
                    occurred_at=now_ts,
                    received_at=now_ts,
                    payload=TaskCreatedPayload(
                        task_id="task-qa",
                        title="支付全链路压测与验收",
                        owner_id=actor_id,
                        workflow_status=WorkflowStatus.TODO,
                        deadline=now_ts + timedelta(days=4),
                        dependencies=("task-checkout",),
                    ),
                ),
            ]
            for evt in bootstrap_events:
                if (
                    isinstance(evt, MemberRegisteredEvent)
                    and evt.payload.member_id in agent_init.projector.state.members
                ):
                    continue
                await self.service.event_store.append(evt)

        chat_type = message.get("chat_type", "p2p")
        chat_id = message.get("chat_id")
        reply_target = chat_id if (chat_type == "group" and chat_id) else actor_id

        # 1. Check for PM proactive progress sync intent
        is_sync_intent = any(
            k in raw_text
            for k in [
                "跟进进度",
                "同步进度",
                "项目进度",
                "进度汇总",
                "检查进度",
                "当前进度",
                "我要知道当前的项目进度",
                "看看进度",
            ]
        )
        if is_sync_intent:
            session = await self.service.start_progress_sync(
                self.project_id, initiated_by=reply_target
            )
            adapter = self.service.adapter_factory(self.project_id)
            client = getattr(adapter, "client", None)
            if client and hasattr(client, "send_message"):
                n_members = len(session.member_probes)
                confirm_msg = (
                    "🚀 **收到指令！已启动全员进度探针与协同工作流**\n\n"
                    f"• **探测范围**：已向 {n_members} 位任务负责人发起 1 对 1 私聊探针\n"
                    "• **智能追问**：若成员回复存在时间或阻塞模糊，系统将自主多轮追问收敛\n"
                    "• **拓扑决策**：全员信息就绪后，将自动推演关键路径并呈送【决策简报】！"
                )
                try:
                    res = client.send_message(
                        receive_id=reply_target, msg_type="text", content={"text": confirm_msg}
                    )
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

            return {
                "code": 0,
                "msg": "sync_session_started",
                "data": {
                    "session_id": session.session_id,
                    "probed_members_count": len(session.member_probes),
                },
            }

        # 2. Check if this is a reply to an active sync probe
        coordinator = await self.service.get_sync_coordinator(self.project_id)
        active_session = coordinator.get_active_session(self.project_id)
        if active_session and actor_id in active_session.member_probes:
            converged, clarify_q, _ = await self.service.handle_sync_member_reply(
                project_id=self.project_id,
                member_id=actor_id,
                message=raw_text,
                occurred_at=occurred_at,
            )
            if not converged and clarify_q:
                return {
                    "code": 0,
                    "msg": "clarification_sent",
                    "data": {"question": clarify_q},
                }

        # 3. Standard message ingestion and single-turn coordination
        is_act, ext_events, agent, reason, round_num, _intent = await self.service.ingest_message(
            project_id=self.project_id,
            message=raw_text,
            actor_id=actor_id,
            occurred_at=occurred_at,
            source_ref=message.get("message_id"),
            auto_run_turn=True,
        )

        if not is_act and actor_id:
            adapter = self.service.adapter_factory(self.project_id)
            client = getattr(adapter, "client", None)
            if client and hasattr(client, "send_message"):
                if self.demo_bootstrap:
                    greeting_text = (
                        "👋 你好！我是 OrgPilot 组织风险与排期协调智能体。\n\n"
                        "🎯 已为您自动初始化单人体验项目，"
                        "您当前身兼「负责人」与「审批人 (PM)」角色：\n"
                        "• 关联任务链：[支付SDK接入] ➔ [收银台前端结账]"
                        " ➔ [支付全链路压测与验收]\n\n"
                        "💬 您现在可以直接向我发送以下测试消息进行体验：\n"
                        "1️⃣ **发起全员主动协同**：「我要知道当前的项目进度」\n"
                        "   （机器人将主动给相关负责人发私聊，多轮追问后为您汇总决策简报）\n"
                        "2️⃣ **汇报阻塞与改期**：「支付 SDK 报错，排查需要到明天下午 5 点」\n"
                        "   （机器人将识别风险并向您发送交互式改期审批卡片）\n"
                        "3️⃣ **汇报进度恢复**：「支付 SDK 阻塞已解决，按原计划推进」\n\n"
                        "📊 浏览器访问 http://localhost:8000/ 可实时查看任务拓扑大盘与关键路径！"
                    )
                else:
                    greeting_text = (
                        "👋 你好！我是 OrgPilot 组织协调智能体。\n\n"
                        "💬 直接告诉我任务进展或阻塞，"
                        "例如：「支付 SDK 报错，排查需要到明天下午 5 点」，"
                        "我会更新任务账本、评估下游影响并发起协调（高影响操作会先请求审批）。\n"
                        "发送「我要知道当前的项目进度」可启动全员进度同步并生成决策简报。"
                    )
                try:
                    res = client.send_message(
                        receive_id=reply_target,
                        msg_type="text",
                        content={"text": greeting_text},
                    )
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

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
        decision = action_val.get("action")
        approval_id = action_val.get("approval_id")

        operator = event_data.get("operator", {}).get("open_id")
        now = datetime.now(UTC)

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

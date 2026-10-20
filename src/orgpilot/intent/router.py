"""MessageRouter: layer 0-2 orchestration for inbound chat messages (ADR-0008).

Processing order per message: message-level idempotency → group chat refusal →
membership check → sync-probe replies → explicit commands → dialogue state +
intent classification (layer 1; transient errors retried once) → guardrails
and handlers (layer 2). The router owns the single reply send per message and
records every outcome in the persistent message log, which also feeds the
"recent turns" conversation context.

In-flight conversation state survives a restart (ADR-0013): the first turn that
touches a project restores its dialogue state from SQL, and every turn ends by
checkpointing the dialogue state and the project's sync sessions in one write,
BEFORE the processed-message row that marks the message as handled.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from orgpilot.config import OrgPilotSettings
from orgpilot.intent.client import (
    HeuristicIntentClient,
    IntentClient,
    IntentContext,
    PendingConfirmationView,
    PendingQuestionView,
    TaskSnapshot,
    classify_with_timeout,
)
from orgpilot.intent.dialogue import (
    CLARIFICATION_SOURCE_PROBE,
    DialogueStateStore,
    PendingClaimDraft,
    PendingClarification,
    PendingSyncConfirmation,
)
from orgpilot.intent.handlers import IntentHandlers
from orgpilot.intent.llm_client import TRANSIENT_HTTP_STATUSES
from orgpilot.intent.models import IntentResult
from orgpilot.intent.replies import (
    FALLBACK_TEXT,
    HELP_TEXT,
    ReplyKind,
    RouterOutcome,
    sync_started_text,
)
from orgpilot.storage.message_store import SqlMessageStore

if TYPE_CHECKING:
    from orgpilot.gateway.service import GatewayService

logger = logging.getLogger(__name__)

UNREGISTERED_HINT = (
    "你好！你还未加入本项目的成员名单，暂时无法处理你的消息。"
    "请联系项目负责人把你的飞书邮箱加入团队名单（team.yaml）并导入后再试。"
)


def default_intent_client(settings: OrgPilotSettings) -> IntentClient:
    """heuristic keeps the mock demo working; llm selects the 3b DeepSeek client."""
    if settings.intent_client == "llm":
        from orgpilot.intent.llm_client import LLMIntentClient

        return LLMIntentClient.from_settings(settings)
    return HeuristicIntentClient()


class Messenger:
    """Thin async wrapper over a Feishu client; silently no-ops without one."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def send_text(self, receive_id: str, text: str) -> None:
        if self._client is None or not text:
            return
        result = self._client.send_message(
            receive_id=receive_id, msg_type="text", content={"text": text}
        )
        if asyncio.iscoroutine(result):
            await result

    async def send_card(self, receive_id: str, card: dict[str, Any]) -> None:
        if self._client is None:
            return
        if hasattr(self._client, "send_card"):
            await self._client.send_card(receive_id=receive_id, card=card)
            return
        # Fallback for clients without interactive-card support
        fallback_note = "📋 收到一张确认卡片（当前通道不支持卡片）"
        await self.send_text(receive_id=receive_id, text=fallback_note)


def _is_transient_error(exc: BaseException) -> bool:
    """Timeouts, connection failures, and HTTP 429/5xx are worth one retry.

    Everything else (invalid output, protocol errors, budget/infrastructure
    failures) is either already retried inside the LLM client or pointless to
    repeat, so the router degrades immediately (3b.1).
    """
    if isinstance(exc, (TimeoutError, httpx.TransportError)):
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in TRANSIENT_HTTP_STATUSES


class MessageRouter:
    """Routes one inbound message through the layered intent pipeline."""

    def __init__(
        self,
        service: "GatewayService",
        intent_client: IntentClient | None = None,
        dialogue_store: DialogueStateStore | None = None,
        message_store: SqlMessageStore | None = None,
        settings: OrgPilotSettings | None = None,
        recent_turns_limit: int | None = None,
    ) -> None:
        self.service = service
        self.settings = settings or OrgPilotSettings()
        # A real LLM intent client arrives via ORGPILOT_INTENT_CLIENT=llm (3b);
        # the heuristic wrapper stays the default so mock demos are unchanged.
        self.intent_client = intent_client or default_intent_client(self.settings)
        self.dialogue = dialogue_store or DialogueStateStore(
            max_topic_reasks=self.settings.max_topic_reasks,
            clock=getattr(service, "clock", None),
        )
        self.messages = message_store or SqlMessageStore(service.db)
        self.recent_turns_limit = recent_turns_limit or self.settings.intent_recent_turns
        self.handlers = IntentHandlers(
            service, self.dialogue, self.settings.intent_confidence_threshold
        )
        self.handlers.bind_session_registrar(self.register_probe_clarifications)
        self._restored_projects: set[str] = set()

    # ------------------------------------------------------------- persistence

    async def restore(self, project_id: str) -> None:
        """Loads the project's persisted dialogue state once per router (ADR-0013).

        Entries registered in memory before this runs are newer and are kept.
        """
        if project_id in self._restored_projects:
            return
        snapshot = await self.service.conversation_store.load_dialogue(project_id)
        if snapshot is not None:
            self.dialogue.restore_project(project_id, snapshot)
        self._restored_projects.add(project_id)

    async def checkpoint(self, project_id: str) -> None:
        """Persists the project's dialogue state and sync sessions (ADR-0013).

        Turns call this themselves; a caller that changes dialogue state through
        the synchronous ``register_*`` methods outside a turn calls it after.
        """
        await self.service.save_conversation_state(project_id, self.dialogue)

    # ------------------------------------------------------------- public API

    def messenger_for(self, project_id: str) -> Messenger:
        adapter = self.service.adapter_factory(project_id)
        client = getattr(adapter, "client", None)
        return Messenger(client)

    def register_pending_clarification(
        self,
        project_id: str,
        sender_id: str,
        question: str,
        task_ids: list[str] | None = None,
        *,
        source: str = "clarify",
        claims: dict[str, PendingClaimDraft] | None = None,
        missing: dict[str, list[str]] | None = None,
        asked_rounds: int = 0,
        occurred_at: datetime | None = None,
    ) -> PendingClarification:
        """Public entry for constructing pending-question state (tests, SUT, probes)."""
        return self.dialogue.register_pending_clarification(
            project_id,
            sender_id,
            question,
            task_ids=task_ids,
            source=source,
            claims=claims,
            missing=missing,
            asked_rounds=asked_rounds,
            created_at=occurred_at,
        )

    def register_probe_clarifications(
        self,
        project_id: str,
        session: Any,
        occurred_at: datetime | None = None,
    ) -> None:
        """Registers a probe PendingClarification for every probed member (3a.1).

        The clarification targets ALL tasks the member owns, so completion means
        every owned task carries a complete claim (or the probe was exhausted).
        """
        for member_id, probe in session.member_probes.items():
            self.dialogue.register_pending_clarification(
                project_id,
                member_id,
                question="项目负责人正在同步项目进度，请逐个汇报您负责任务的最新进展。",
                task_ids=list(probe.assigned_tasks),
                source=CLARIFICATION_SOURCE_PROBE,
                created_at=occurred_at,
            )

    async def start_sync(
        self,
        project_id: str,
        initiated_by: str,
        occurred_at: datetime | None = None,
    ) -> Any:
        """Public sync entry: starts the session and registers probe clarifications."""
        await self.restore(project_id)
        session = await self.service.start_progress_sync(
            project_id,
            initiated_by=initiated_by,
            occurred_at=occurred_at,
        )
        self.register_probe_clarifications(project_id, session, occurred_at=occurred_at)
        # The session and its probe questions are persisted together (ADR-0013).
        await self.checkpoint(project_id)
        return session

    def register_pending_sync_confirmation(
        self,
        project_id: str,
        sender_id: str,
        initiated_by: str,
        member_ids: list[str],
        occurred_at: datetime | None = None,
    ) -> PendingSyncConfirmation:
        """Public entry for constructing a pending sync confirmation card state."""
        return self.dialogue.register_pending_sync_confirmation(
            project_id,
            sender_id,
            initiated_by=initiated_by,
            member_ids=member_ids,
            created_at=occurred_at,
        )

    async def handle_message(
        self,
        *,
        project_id: str,
        sender_id: str,
        message_id: str,
        text: str,
        occurred_at: datetime,
        chat_type: str = "p2p",
    ) -> RouterOutcome:
        """Routes one message; returns the outcome and sends the single reply."""
        prior = await self.messages.get_outcome(project_id, message_id)
        if prior is not None:
            logger.info("Duplicate message %s suppressed", message_id)
            try:
                previous_msg = json.loads(prior.response_json).get("msg", "success")
            except ValueError:
                previous_msg = "success"
            return RouterOutcome(
                reply_category=ReplyKind(prior.reply_category),
                reply_text=prior.reply_text,
                msg="duplicate_suppressed",
                duplicate=True,
                suppress_reply_send=True,
                data={"previous_msg": previous_msg, "intent_summary": {}},
            )

        await self.restore(project_id)
        try:
            outcome = await self._route(
                project_id=project_id,
                sender_id=sender_id,
                message_id=message_id,
                text=text,
                occurred_at=occurred_at,
                chat_type=chat_type,
            )
        finally:
            # The database mirrors whatever this process now holds in memory,
            # even when routing failed part-way; the processed-message row below
            # stays the turn's last write.
            await self.checkpoint(project_id)

        intent_summary = outcome.data.get("intent_summary", {})
        await self.messages.record_outcome(
            project_id=project_id,
            message_id=message_id,
            sender_id=sender_id,
            message_text=text,
            intent_summary=intent_summary,
            reply_category=outcome.reply_category.value,
            reply_text=outcome.reply_text,
            response={"code": 0, "msg": outcome.msg, "data": _jsonable(outcome.data)},
            created_at=occurred_at,
        )
        if outcome.reply_text and not outcome.suppress_reply_send:
            await self.messenger_for(project_id).send_text(sender_id, outcome.reply_text)
        return outcome

    # ------------------------------------------------------------- card action

    async def handle_sync_card_action(
        self,
        *,
        project_id: str,
        action_value: dict[str, Any],
        operator_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Handles sync_confirm / sync_cancel card button callbacks in place."""
        await self.restore(project_id)
        try:
            return await self._sync_card_action(
                project_id=project_id,
                action_value=action_value,
                operator_id=operator_id,
                now=now,
            )
        finally:
            await self.checkpoint(project_id)

    async def _sync_card_action(
        self,
        *,
        project_id: str,
        action_value: dict[str, Any],
        operator_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        action = action_value.get("action")
        requested_at = _parse_iso(action_value.get("requested_at"))
        confirmation = self.dialogue.get_pending_sync_confirmation(
            project_id, operator_id, now=now
        )

        agent = await self.service.get_or_replay_agent(project_id)
        member = agent.projector.state.members.get(operator_id)
        authorized = (
            confirmation is not None
            and confirmation.initiated_by == operator_id
            and member is not None
            and member.role == "pm"
        )

        if authorized and action == "sync_confirm":
            coordinator = await self.service.get_sync_coordinator(project_id)
            active = coordinator.get_active_session(project_id, now=now)
            self.dialogue.clear_pending_sync_confirmation(project_id, operator_id)
            if active is not None:
                # One active session at a time (3a.1): confirm nothing, close card.
                return {
                    "code": 0,
                    "msg": "sync_already_active",
                    "card": _closed_card(
                        "已关闭",
                        f"已有进行中的全员同步（{active.progress_summary()}），本次确认未重复发起。",
                    ),
                }
            session = await self.start_sync(
                project_id, initiated_by=operator_id, occurred_at=now
            )
            return {
                "code": 0,
                "msg": "sync_session_started",
                "card": _closed_card(
                    "已发起",
                    f"✅ {operator_id} 已确认，正在向 "
                    f"{len(session.member_probes)} 位负责人发送进度探针。",
                ),
            }
        if authorized and action == "sync_cancel":
            self.dialogue.clear_pending_sync_confirmation(project_id, operator_id)
            return {
                "code": 0,
                "msg": "sync_cancelled",
                "card": _closed_card("已取消", f"⚪ {operator_id} 已取消本次全员同步。"),
            }

        expired = (
            confirmation is None
            and requested_at is not None
            and now - requested_at > self.dialogue.confirmation_ttl
        )
        if expired:
            note = "⌛ 该确认卡片已超过 30 分钟未处理，自动过期。如仍需同步请重新发起。"
            return {"code": 0, "msg": "sync_card_expired", "card": _closed_card("已过期", note)}
        return {
            "code": 0,
            "msg": "sync_card_invalid",
            "card": _closed_card(
                "已失效", "该确认卡片不可用（仅发起人本人可操作，或状态已变化）。"
            ),
        }

    # ------------------------------------------------------------- internals

    async def _route(
        self,
        *,
        project_id: str,
        sender_id: str,
        message_id: str,
        text: str,
        occurred_at: datetime,
        chat_type: str,
    ) -> RouterOutcome:
        # Layer 0: only private chats are processed (ADR-0008 §1)
        if chat_type != "p2p":
            logger.info("Ignoring non-p2p message %s in project %s", message_id, project_id)
            return RouterOutcome(
                msg="group_not_supported",
                suppress_reply_send=True,
                data={"intent_summary": {}},
            )

        agent = await self.service.get_or_replay_agent(project_id)
        member = agent.projector.state.members.get(sender_id)
        if member is None:
            return RouterOutcome(
                msg="sender_not_registered",
                reply_category=ReplyKind.NO_PERMISSION,
                reply_text=UNREGISTERED_HINT,
                data={"actor_id": sender_id, "intent_summary": {}},
            )

        # Layer 0: explicit commands run BEFORE probe capture and never consume
        # probe rounds (3a.1) — a probed member may still ask for help.
        stripped = text.strip()
        if stripped.startswith("/"):
            return await self._handle_command(
                project_id=project_id, member=member, command=stripped, occurred_at=occurred_at
            )

        # Probe replies now flow through the same intent pipeline as normal
        # messages (3a.1): the pending probe clarification provides the answer
        # context, and the handlers merge/complete/notify the coordinator.

        # Session housekeeping: idle sessions must expire even when every
        # participant is silent, so every inbound message ticks the clock. The
        # coordinator's agent is refreshed first so any timeout briefing it
        # synthesizes reflects the latest persisted claims.
        coordinator = await self.service.get_sync_coordinator(project_id)
        coordinator.agent = agent
        coordinator.adapter = agent.adapter
        coordinator.get_active_session(project_id, now=occurred_at)

        # Layer 1: classify (with one retry), layer 2: guardrails + handlers
        context = await self._build_context(
            project_id=project_id,
            sender_id=sender_id,
            member=member,
            text=text,
            occurred_at=occurred_at,
            agent=agent,
        )
        result = await self._classify_with_retry(text, context)
        if result is None:
            return RouterOutcome(
                msg="fallback",
                reply_category=ReplyKind.FALLBACK,
                reply_text=FALLBACK_TEXT,
                data={"intent_summary": {}},
            )
        messenger = self.messenger_for(project_id)
        self.handlers.bind_senders(messenger.send_text, messenger.send_card)
        outcome = await self.handlers.process_result(
            project_id=project_id,
            member=member,
            message=text,
            occurred_at=occurred_at,
            result=result,
            context=context,
            message_id=message_id,
        )
        outcome.data["intent_summary"] = result.to_summary()
        return outcome

    async def _handle_command(
        self,
        *,
        project_id: str,
        member: Any,
        command: str,
        occurred_at: datetime,
    ) -> RouterOutcome:
        head = command.split()[0] if command.split() else command
        if head == "/同步":
            if member.role != "pm":
                return RouterOutcome(
                    reply_category=ReplyKind.NO_PERMISSION,
                    reply_text="只有项目负责人（PM）可以发起全员同步。",
                    data={"intent_summary": {"command": head}},
                )
            coordinator = await self.service.get_sync_coordinator(project_id)
            active = coordinator.get_active_session(project_id, now=occurred_at)
            if active is not None:
                # One active session at a time: a repeat /同步 reports progress
                # instead of scattering a second round of probes (3a.1).
                return RouterOutcome(
                    msg="sync_already_active",
                    reply_category=ReplyKind.NOTED,
                    reply_text=(
                        f"已有进行中的全员同步（{active.progress_summary()}），"
                        "请等待成员回复后自动生成简报。"
                    ),
                    data={
                        "session_id": active.session_id,
                        "intent_summary": {"command": head},
                    },
                )
            session = await self.start_sync(
                project_id, initiated_by=member.member_id, occurred_at=occurred_at
            )
            return RouterOutcome(
                msg="sync_session_started",
                reply_category=ReplyKind.SYNC_STARTED,
                reply_text=sync_started_text(session),
                data={
                    "session_id": session.session_id,
                    "intent_summary": {"command": head},
                },
            )
        # /帮助 and any unknown /xxx return the help text (ADR-0008 layer 0)
        return RouterOutcome(
            reply_category=ReplyKind.HELP,
            reply_text=HELP_TEXT,
            data={"intent_summary": {"command": head}},
        )

    async def _build_context(
        self,
        *,
        project_id: str,
        sender_id: str,
        member: Any,
        text: str,
        occurred_at: datetime,
        agent: Any,
    ) -> IntentContext:
        state = agent.projector.state
        known_tasks = [
            TaskSnapshot(
                task_id=task.task_id,
                title=task.title,
                owner_id=task.owner_id,
                deadline=task.deadline,
                health_status=(
                    task.health_status.value
                    if hasattr(task.health_status, "value")
                    else str(task.health_status)
                ),
            )
            for task in state.tasks.values()
        ]
        pending = self.dialogue.get_pending_clarification(project_id, sender_id, now=occurred_at)
        confirmation = self.dialogue.get_pending_sync_confirmation(
            project_id, sender_id, now=occurred_at
        )
        history_rows = await self.messages.recent_turns(
            project_id, sender_id, limit=self.recent_turns_limit
        )
        history: list[str] = []
        for row in history_rows:
            line = f"{row['sender']}: {row['message']}"
            if row["reply"]:
                line += "\n机器人: " + row["reply"]
            history.append(line)
        return IntentContext(
            project_id=project_id,
            message=text,
            occurred_at=occurred_at,
            sender_id=sender_id,
            sender_role=member.role,
            sender_display_name=member.display_name,
            sender_task_ids=[
                task.task_id for task in state.tasks.values() if task.owner_id == sender_id
            ],
            known_tasks=known_tasks,
            pending_question=(
                PendingQuestionView(
                    question=pending.question,
                    task_ids=list(pending.task_ids),
                    asked_rounds=pending.asked_rounds,
                )
                if pending
                else None
            ),
            pending_confirmation=(
                PendingConfirmationView(initiated_by=confirmation.initiated_by)
                if confirmation
                else None
            ),
            conversation_history=tuple(history),
        )

    async def _classify_with_retry(
        self, message: str, context: IntentContext
    ) -> IntentResult | None:
        """One classify attempt; exactly one retry, for transient errors only.

        The LLM client already retries validation failures internally, so an
        ``LLMIntentError`` reaching this router degrades immediately instead of
        paying for another round trip. Missing replay recordings and exhausted
        spend budgets are infrastructure failures re-raised via
        ``IntentInfrastructureError`` — never retried, never degraded (3b.1).
        """
        from orgpilot.intent.llm_client import IntentInfrastructureError, LLMIntentError

        try:
            result = await classify_with_timeout(
                self.intent_client,
                message,
                context,
                self.settings.intent_timeout_seconds,
            )
        except IntentInfrastructureError:
            raise
        except LLMIntentError as exc:
            logger.warning("Intent classify produced invalid output: %s", exc)
            return None
        except Exception as exc:  # includes asyncio.TimeoutError
            if not _is_transient_error(exc):
                logger.warning("Intent classify failed (non-transient): %s", exc)
                return None
            logger.warning("Intent classify transient failure, retrying once: %s", exc)
            try:
                result = await classify_with_timeout(
                    self.intent_client,
                    message,
                    context,
                    self.settings.intent_timeout_seconds,
                )
            except Exception as retry_exc:
                logger.error("Intent classify failed after retry: %s", retry_exc)
                return None
        if isinstance(result, IntentResult):
            return result
        logger.error("Intent classify returned invalid output %s", type(result))
        return None


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    return dict(data)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _closed_card(status_text: str, note: str) -> dict[str, Any]:
    from orgpilot.feishu.cards import build_sync_closed_card

    return build_sync_closed_card(status_text, note)

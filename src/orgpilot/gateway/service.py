"""Gateway coordination service orchestrating persistence and Agent execution."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.agent.loop import CoordinationAgent
from orgpilot.coordination.directives import DirectiveManager, DirectiveNotice
from orgpilot.coordination.sync_coordinator import ProgressSyncCoordinator
from orgpilot.coordination.tasks import TaskManager, TaskOutcome
from orgpilot.domain.enums import ActionType, ApprovalStatus, CoordinationCaseStatus, MessageIntent
from orgpilot.domain.models import ActionCommand, AgentTurnTrace
from orgpilot.domain.sync_models import SyncSession
from orgpilot.events.log import AppendResult
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    OrgEvent,
    parse_event,
)
from orgpilot.extraction.client import LLMUnavailableError
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import MessageContext
from orgpilot.gateway.outbox import OutboxDispatcher
from orgpilot.storage.database import Database
from orgpilot.storage.event_store import SqlEventStore
from orgpilot.storage.outbox_store import OUTBOX_DEAD, OUTBOX_DELIVERED, SqlOutboxStore
from orgpilot.storage.state_store import SqlStateStore

logger = logging.getLogger("orgpilot.gateway.service")


@dataclass
class MessageIngestResult:
    """Full outcome of ingesting one natural-language message."""

    is_actionable: bool
    events: list[OrgEvent]
    agent: CoordinationAgent
    turn_reason: str | None = None
    round_num: int | None = None
    intent: str | None = None
    directive_kind: str | None = None
    bot_reply: str | None = None
    notices: list[DirectiveNotice] = field(default_factory=list)


class GatewayService:
    """Coordinates event ingestion, natural language extraction, and agent execution with
    SQL persistence.
    """

    def __init__(
        self,
        db: Database,
        extractor: ClaimExtractor | None = None,
        adapter_factory: Callable[[str], CollaborationAdapter] | None = None,
        reference_timezone: str = "Asia/Shanghai",
        directive_reminder_minutes: int = 60,
        directive_escalation_minutes: int = 1440,
        outbox_max_attempts: int = 3,
        outbox_retry_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.event_store = SqlEventStore(db)
        self.state_store = SqlStateStore(db)
        self.outbox_store = SqlOutboxStore(db)
        self.outbox_dispatcher = OutboxDispatcher(
            self.outbox_store,
            max_attempts=outbox_max_attempts,
            retry_seconds=outbox_retry_seconds,
        )
        self.extractor = extractor or ClaimExtractor()
        self.reference_timezone = reference_timezone
        self.directive_reminder_minutes = directive_reminder_minutes
        self.directive_escalation_minutes = directive_escalation_minutes
        self.adapter_factory = adapter_factory or (
            lambda project_id: MockCollaborationAdapter(project_id=project_id)
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sync_coordinators: dict[str, ProgressSyncCoordinator] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}

    def now(self) -> datetime:
        """Returns the service clock, injectable for deterministic boundary tests."""
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _project_lock(self, project_id: str) -> asyncio.Lock:
        """Serializes coordination turns per project so concurrent member
        interactions cannot interleave case transitions or duplicate actions."""
        if project_id not in self._project_locks:
            self._project_locks[project_id] = asyncio.Lock()
        return self._project_locks[project_id]

    async def _refresh_agent_from_store(self, agent: CoordinationAgent) -> None:
        """Catches a held agent up with events, cases, and approvals persisted by
        concurrent turns, so it never decides on stale projected state."""
        events = await self.event_store.get_events(agent.project_id)
        for evt in events:
            if evt.event_id in agent.projector.state.processed_event_ids:
                continue
            if agent.event_log.append(evt) is AppendResult.APPENDED:
                agent.projector.apply(evt)
        for case in await self.state_store.load_cases(agent.project_id):
            if agent.case_ledger.get_case(case.case_id) is None:
                agent.case_ledger._cases[case.case_id] = case
        for request in await self.state_store.load_approvals(agent.project_id):
            if agent.approval_manager.get_request(request.approval_id) is None:
                agent.approval_manager._requests[request.approval_id] = request

    async def get_or_replay_agent(self, project_id: str) -> CoordinationAgent:
        """Constructs an Agent with state restored from the persistent SQL event log and stores."""
        agent = CoordinationAgent(
            project_id=project_id,
            adapter=self.adapter_factory(project_id),
            defer_outbound=True,
        )
        events = await self.event_store.get_events(project_id)

        # A snapshot is a replay cache. The event log remains authoritative and any
        # events not represented by the snapshot are applied after restoration. A
        # snapshot that references a missing event is discarded rather than allowed
        # to introduce state that cannot be reproduced from the event log.
        snapshot = await self.state_store.load_state(project_id)
        persisted_event_ids = {event.event_id for event in events}
        if snapshot is not None and snapshot.processed_event_ids <= persisted_event_ids:
            agent.projector.state = snapshot

        for evt in events:
            agent.event_log.append(evt)
            if evt.event_id not in agent.projector.state.processed_event_ids:
                agent.projector.apply(evt)

        # Restore cases and approvals
        cases = await self.state_store.load_cases(project_id)
        for c in cases:
            agent.case_ledger._cases[c.case_id] = c

        approvals = await self.state_store.load_approvals(project_id)
        for r in approvals:
            agent.approval_manager._requests[r.approval_id] = r

        return agent

    async def run_agent_turn(
        self,
        agent: CoordinationAgent,
        events: list[OrgEvent],
        current_time: datetime,
    ) -> tuple[AgentTurnTrace, list[OrgEvent]]:
        """Runs a locked agent turn; see :meth:`_run_agent_turn_locked`."""
        async with self._project_lock(agent.project_id):
            return await self._run_agent_turn_locked(agent, events, current_time)

    async def _run_agent_turn_locked(
        self,
        agent: CoordinationAgent,
        events: list[OrgEvent],
        current_time: datetime,
    ) -> tuple[AgentTurnTrace, list[OrgEvent]]:
        """Runs the synchronous deterministic kernel off-loop and persists all outputs.

        Callers must already hold the per-project lock; the agent is refreshed
        from the event log before deciding, so replies arriving concurrently from
        multiple members cannot duplicate coordination actions.
        """
        await self._refresh_agent_from_store(agent)
        trace, generated_events = await asyncio.to_thread(
            agent.run_turn,
            events,
            current_time,
        )
        for event in generated_events:
            await self.event_store.append(event)
        await self._settle_turn_outbound(agent, current_time)
        trace = trace.model_copy(
            update={
                "active_case_ids": tuple(
                    case.case_id for case in agent.case_ledger.get_active_cases()
                ),
                "termination_reason": agent.current_termination_reason(),
            }
        )
        await self.save_agent_state(agent)
        return trace, generated_events

    async def _settle_turn_outbound(self, agent: CoordinationAgent, ts: datetime) -> None:
        """Settles the agent turn's transports into the outbox.

        Commands the adapter confirmed inline become durable delivery-ledger rows;
        retryable transport failures remain pending for the outbox sweep, while
        permanent failures become dead letters and update the affected case.
        """
        delivery_events: list[OrgEvent] = []
        now = self.now()
        for command, delivered, _error in agent.pop_turn_outbound():
            if delivered is None:
                delivery_events.extend(
                    await self.outbox_dispatcher.enqueue_and_deliver(
                        agent.project_id, command, agent.adapter
                    )
                )
                status = await self.outbox_store.status_of(
                    agent.project_id, command.idempotency_key
                )
                if command.action_type is ActionType.UPDATE_TASK and status != OUTBOX_DELIVERED:
                    self._mark_task_execution_failed(agent, command, ts)
                elif status == OUTBOX_DEAD:
                    self._mark_command_delivery_dead(agent, command, ts)
            elif delivered:
                delivery_events.extend(
                    await self.outbox_dispatcher.record_pre_delivered(agent.project_id, command)
                )
            else:
                await self.outbox_store.enqueue(
                    agent.project_id,
                    command,
                    now,
                    next_attempt_at=now + timedelta(seconds=self.outbox_dispatcher.retry_seconds),
                )
        await self._persist_delivery_events(agent, delivery_events)

    async def _deliver_deferred_commands(
        self, agent: CoordinationAgent, commands: list[ActionCommand]
    ) -> None:
        delivery_events: list[OrgEvent] = []
        for command in commands:
            delivery_events.extend(
                await self.outbox_dispatcher.enqueue_and_deliver(
                    agent.project_id, command, agent.adapter
                )
            )
        await self._persist_delivery_events(agent, delivery_events)

    @staticmethod
    def _mark_task_execution_failed(
        agent: CoordinationAgent, command: ActionCommand, current_time: datetime
    ) -> None:
        for case in agent.case_ledger.get_all_cases():
            if any(item.command_id == command.command_id for item in case.executed_commands):
                agent.case_ledger.transition(
                    case.case_id,
                    CoordinationCaseStatus.ESCALATED,
                    current_time,
                    terminal_reason="approved task update execution failed",
                    candidate_actions=(),
                )

    @staticmethod
    def _mark_command_delivery_dead(
        agent: CoordinationAgent, command: ActionCommand, current_time: datetime
    ) -> None:
        case_ids = {
            case.case_id
            for case in agent.case_ledger.get_all_cases()
            if any(item.command_id == command.command_id for item in case.executed_commands)
        }
        case_ids.update(
            request.case_id
            for request in agent.approval_manager.get_all_requests()
            if request.proposed_command.command_id == command.command_id
        )
        for case_id in case_ids:
            if agent.case_ledger.get_case(case_id) is not None:
                agent.case_ledger.transition(
                    case_id,
                    CoordinationCaseStatus.ESCALATED,
                    current_time,
                    terminal_reason=f"{command.action_type.value} delivery failed permanently",
                    candidate_actions=(),
                )

    async def _persist_delivery_events(
        self, agent: CoordinationAgent, delivery_events: list[OrgEvent]
    ) -> None:
        """Projects and persists delivery-ledger and adapter-generated events.

        Delivery timestamps are clamped forward to the directive's last known
        event time: replay sorts by occurred_at, so a delivery stamped with the
        transport clock must never sort before the directive it settles (a
        future-dated issue event would otherwise brick replay).
        """
        state = agent.projector.state
        clamped: list[OrgEvent] = []
        for evt in delivery_events:
            directive_id = getattr(evt.payload, "directive_id", None)
            directive = state.directives.get(directive_id) if directive_id else None
            if directive is not None and evt.occurred_at < directive.last_update_at:
                evt = evt.model_copy(
                    update={
                        "occurred_at": directive.last_update_at,
                        "received_at": directive.last_update_at,
                    }
                )
            clamped.append(evt)
        for evt in clamped:
            if agent.event_log.append(evt) is AppendResult.APPENDED:
                agent.projector.apply(evt)
        for evt in clamped:
            await self.event_store.append(evt)
        self._settle_retried_task_updates(agent, clamped)

    @staticmethod
    def _settle_retried_task_updates(agent: CoordinationAgent, events: list[OrgEvent]) -> None:
        """Closes an execution-failed case only after its retried task event exists."""
        update_refs = {
            evt.source_ref
            for evt in events
            if evt.event_type == "task.updated" and evt.source_ref is not None
        }
        if not update_refs:
            return
        for case in agent.case_ledger.get_all_cases():
            if (
                case.status is not CoordinationCaseStatus.ESCALATED
                or case.terminal_reason != "approved task update execution failed"
            ):
                continue
            matching = any(
                f"command:{command.command_id}" in update_refs
                or f"adapter:command:{command.command_id}" in update_refs
                for command in case.executed_commands
            )
            if matching:
                agent.case_ledger.transition(
                    case.case_id,
                    new_status=CoordinationCaseStatus.RESOLVED,
                    current_time=max(evt.occurred_at for evt in events),
                    terminal_reason="approved task update succeeded after retry",
                    candidate_actions=(),
                )

    async def save_agent_state(self, agent: CoordinationAgent) -> None:
        """Persists current state snapshot, active cases, and approvals."""
        await self.state_store.save_state(agent.projector.state)
        await self.state_store.save_cases(agent.project_id, agent.case_ledger.get_all_cases())
        await self.state_store.save_approvals(
            agent.project_id, agent.approval_manager.get_all_requests()
        )

    async def ingest_raw_events(
        self, project_id: str, raw_events: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Parses and appends raw JSON events into the persistent event log."""
        appended_count = 0
        duplicate_count = 0
        for raw in raw_events:
            event = parse_event(raw)
            res = await self.event_store.append(event)
            if res is AppendResult.DUPLICATE:
                duplicate_count += 1
            else:
                appended_count += 1
        return appended_count, duplicate_count

    async def ingest_message(
        self,
        project_id: str,
        message: str,
        actor_id: str,
        occurred_at: datetime | None = None,
        source_ref: str | None = None,
        auto_run_turn: bool = True,
    ) -> MessageIngestResult:
        """Extracts claims from natural language message, persists events, and optionally
        executes a turn.

        Events are projected in memory BEFORE being persisted: if the domain rejects
        anything (and unknown senders are auto-registered, so it normally should not),
        the persistent event log stays replayable instead of being bricked by an
        event that can never be projected.

        Directive intents drive the directive lifecycle (issue/relay, clarify,
        authority decline); replies from directive targets are intercepted for
        acknowledge/complete transitions; unanswered directives auto-remind and
        escalate by age.

        The whole turn is serialized behind the per-project lock.
        """
        async with self._project_lock(project_id):
            return await self._ingest_message_locked(
                project_id=project_id,
                message=message,
                actor_id=actor_id,
                occurred_at=occurred_at,
                source_ref=source_ref,
                auto_run_turn=auto_run_turn,
            )

    async def _ingest_message_locked(
        self,
        project_id: str,
        message: str,
        actor_id: str,
        occurred_at: datetime | None,
        source_ref: str | None,
        auto_run_turn: bool,
    ) -> MessageIngestResult:
        ts = occurred_at or self.now()
        agent = await self.get_or_replay_agent(project_id)

        ingest_events: list[OrgEvent] = []
        if actor_id and actor_id not in agent.projector.state.members:
            # Auto-register unseen senders so their claims are attributable —
            # a coordination agent must be able to meet new team members.
            ingest_events.append(
                MemberRegisteredEvent(
                    project_id=project_id,
                    event_id=f"evt-member-auto-{actor_id}",
                    event_type="member.registered",
                    source=EventSource.MESSAGE,
                    source_ref=source_ref or "auto-registration",
                    occurred_at=ts,
                    received_at=ts,
                    payload=MemberRegisteredPayload(
                        member_id=actor_id,
                        display_name=actor_id,
                        role="member",
                    ),
                )
            )

        tasks_dict = {t.task_id: t.title for t in agent.projector.state.tasks.values()}
        members_dict = {m.member_id: m.role for m in agent.projector.state.members.values()}

        context = MessageContext(
            project_id=project_id,
            actor_id=actor_id,
            occurred_at=ts,
            source_ref=source_ref,
            known_tasks=tasks_dict,
            known_members=members_dict,
            reference_timezone=self.reference_timezone,
        )

        # Extraction may block on the LLM provider; run it off the event loop so
        # a slow upstream cannot freeze the gateway (P1: ReadTimeout 500s).
        try:
            extraction_result, extracted_events = await asyncio.to_thread(
                self.extractor.extract_from_message, message, context
            )
        except LLMUnavailableError as exc:
            logger.error("LLM extraction unavailable: %s", exc)
            return MessageIngestResult(
                is_actionable=False,
                events=[],
                agent=agent,
                intent=None,
                directive_kind="llm_unavailable",
                bot_reply=(
                    "⚠️ 智能抽取服务暂时不可用（已自动重试），本条消息未处理。"
                    "系统其他功能不受影响，请稍后重试。"
                ),
            )
        ingest_events.extend(extracted_events)

        directive_kind: str | None = None
        directive_reply: str | None = None
        directive_notices: list[DirectiveNotice] = []
        directive_outbound: list[ActionCommand] = []
        manager = self._directive_manager_for(agent)

        # A pending directive clarification consumes the issuer's next reply:
        # the answer restores the original draft instead of starting a new turn.
        pending_outcome = manager.resolve_pending_clarification(
            actor_id, message, agent.projector.state, ts
        )
        if pending_outcome is not None:
            directive_kind = pending_outcome.kind
            directive_reply = pending_outcome.bot_reply
            directive_notices.extend(pending_outcome.notices)
            directive_outbound.extend(pending_outcome.outbound)
            ingest_events.extend(pending_outcome.events)
        elif extraction_result.intent in (
            MessageIntent.TASK_CREATE,
            MessageIntent.TASK_REASSIGN,
            MessageIntent.DEADLINE_CHANGE,
        ):
            task_manager = self._task_manager_for(agent)
            if extraction_result.intent is MessageIntent.TASK_CREATE:
                task_outcome = task_manager.handle_task_create_intent(
                    message=message,
                    actor_id=actor_id,
                    result=extraction_result,
                    state=agent.projector.state,
                    occurred_at=ts,
                )
            elif extraction_result.intent is MessageIntent.TASK_REASSIGN:
                task_outcome = task_manager.handle_task_reassign_intent(
                    message=message,
                    actor_id=actor_id,
                    result=extraction_result,
                    state=agent.projector.state,
                    occurred_at=ts,
                )
            else:
                task_outcome = task_manager.handle_deadline_change_intent(
                    message=message,
                    actor_id=actor_id,
                    result=extraction_result,
                    state=agent.projector.state,
                    occurred_at=ts,
                )
            directive_kind = task_outcome.kind
            directive_reply = task_outcome.bot_reply
            directive_notices = list(task_outcome.notices)
            directive_outbound = list(task_outcome.outbound)
            if task_outcome.events:
                ingest_events.extend(task_outcome.events)
            if task_outcome.approval is not None:
                # The gated proposal lives in the approval store, not the event
                # log — persist it now or it dies with this agent instance.
                await self.save_agent_state(agent)
        elif extraction_result.intent is MessageIntent.DIRECTIVE:
            outcome = manager.handle_directive_intent(
                message=message,
                actor_id=actor_id,
                hints=extraction_result.hints,
                state=agent.projector.state,
                occurred_at=ts,
            )
            directive_kind = outcome.kind
            directive_reply = outcome.bot_reply
            directive_notices = list(outcome.notices)
            directive_outbound = list(outcome.outbound)
            if outcome.events:
                ingest_events.extend(outcome.events)
        else:
            # Timeout sweep first, then reply interception for open directives.
            sweep = manager.sweep_timeouts(agent.projector.state, ts)
            if sweep.events:
                ingest_events.extend(sweep.events)
                directive_notices.extend(sweep.notices)
                directive_outbound.extend(sweep.outbound)
                directive_kind = "swept"
            reply_outcome = manager.handle_member_reply(
                actor_id, message, agent.projector.state, ts
            )
            if reply_outcome is not None and reply_outcome.kind != "none":
                directive_kind = reply_outcome.kind
                directive_reply = reply_outcome.bot_reply
                directive_notices.extend(reply_outcome.notices)
                ingest_events.extend(reply_outcome.events)
            elif reply_outcome is not None:
                directive_reply = reply_outcome.bot_reply

        # Project in memory first; a domain rejection here leaves the SQL log untouched.
        for evt in ingest_events:
            if agent.event_log.append(evt) is AppendResult.APPENDED:
                agent.projector.apply(evt)
        for evt in ingest_events:
            await self.event_store.append(evt)

        # Relay messages only after their events are durably persisted. The outbox
        # makes the relay crash-recoverable (enqueue before send) and retries
        # transport failures with backoff; delivery settles the directive ledger.
        delivery_events: list[OrgEvent] = []
        for command in directive_outbound:
            delivery_events.extend(
                await self.outbox_dispatcher.enqueue_and_deliver(project_id, command, agent.adapter)
            )
        await self._persist_delivery_events(agent, delivery_events)

        turn_reason: str | None = None
        round_num: int | None = None

        if auto_run_turn and extracted_events:
            turn_trace, _ = await self._run_agent_turn_locked(agent, [], ts)
            turn_reason = turn_trace.termination_reason.value
            round_num = turn_trace.round_number

        return MessageIngestResult(
            is_actionable=extraction_result.is_actionable,
            events=extracted_events,
            agent=agent,
            turn_reason=turn_reason,
            round_num=round_num,
            intent=extraction_result.intent.value if extraction_result.intent else None,
            directive_kind=directive_kind,
            bot_reply=directive_reply,
            notices=directive_notices,
        )

    def _directive_manager_for(self, agent: CoordinationAgent) -> DirectiveManager:
        """Builds a stateless directive manager bound to the agent's live adapter."""
        return DirectiveManager(
            adapter=agent.adapter,
            reminder_after_minutes=self.directive_reminder_minutes,
            escalation_after_minutes=self.directive_escalation_minutes,
            reference_timezone=self.reference_timezone,
        )

    def _task_manager_for(self, agent: CoordinationAgent) -> TaskManager:
        """Builds a stateless task-op manager bound to the agent's adapter and approvals."""
        return TaskManager(
            adapter=agent.adapter,
            approval_manager=agent.approval_manager,
            reference_timezone=self.reference_timezone,
        )

    async def settle_task_approvals(self, project_id: str, operator_id: str) -> TaskOutcome | None:
        """Settles approved/rejected task-operation approvals into kernel events.

        Case-scoped approvals (reschedules) are consumed by the agent loop; task
        proposals settle here: approved emits task.created/task.updated (which
        the DAG picks up automatically) and notifies the affected members.
        """
        async with self._project_lock(project_id):
            agent = await self.get_or_replay_agent(project_id)
            manager = self._task_manager_for(agent)
            now = self.now()
            aggregated: TaskOutcome | None = None
            dirty = False
            for request in agent.approval_manager.get_all_requests():
                proposal_kind = request.proposed_command.payload.get("proposal_kind")
                if proposal_kind not in ("task_create", "task_reassign", "deadline_change"):
                    continue
                if request.consumed:
                    continue
                if request.status not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
                    continue
                outcome = manager.settle_approval(agent.projector.state, request, operator_id, now)
                if outcome is None:
                    continue
                dirty = True
                for evt in outcome.events:
                    if agent.event_log.append(evt) is AppendResult.APPENDED:
                        agent.projector.apply(evt)
                    await self.event_store.append(evt)
                if aggregated is None:
                    aggregated = outcome
                else:
                    aggregated.notices.extend(outcome.notices)
                    aggregated.outbound.extend(outcome.outbound)
                if aggregated.bot_reply and outcome.bot_reply and aggregated is not outcome:
                    aggregated.bot_reply = f"{aggregated.bot_reply}\n{outcome.bot_reply}"
                elif aggregated.bot_reply is None:
                    aggregated.bot_reply = outcome.bot_reply
            if not dirty:
                return None
            delivery_events: list[OrgEvent] = []
            assert aggregated is not None
            for command in aggregated.outbound:
                delivery_events.extend(
                    await self.outbox_dispatcher.enqueue_and_deliver(
                        project_id, command, agent.adapter
                    )
                )
            await self._persist_delivery_events(agent, delivery_events)
            await self.save_agent_state(agent)
            return aggregated

    async def remind_directives(self, project_id: str, operator_id: str) -> MessageIngestResult:
        """Manually nudges every unacknowledged directive and persists the reminders."""
        async with self._project_lock(project_id):
            agent = await self.get_or_replay_agent(project_id)
            manager = self._directive_manager_for(agent)
            outcome = manager.remind_open_directives(agent.projector.state, operator_id, self.now())
            if outcome.kind == "none":
                return MessageIngestResult(
                    is_actionable=False,
                    events=[],
                    agent=agent,
                    intent=None,
                    directive_kind="none",
                    bot_reply=outcome.bot_reply,
                )

            for evt in outcome.events:
                if agent.event_log.append(evt) is AppendResult.APPENDED:
                    agent.projector.apply(evt)
                await self.event_store.append(evt)
            await self.save_agent_state(agent)
            reminder_delivery_events: list[OrgEvent] = []
            for command in outcome.outbound:
                reminder_delivery_events.extend(
                    await self.outbox_dispatcher.enqueue_and_deliver(
                        project_id, command, agent.adapter
                    )
                )
            await self._persist_delivery_events(agent, reminder_delivery_events)
            return MessageIngestResult(
                is_actionable=False,
                events=outcome.events,
                agent=agent,
                intent=None,
                directive_kind="reminded",
                bot_reply=outcome.bot_reply,
                notices=list(outcome.notices),
            )

    async def sweep_outbox(self) -> int:
        """Delivers due pending outbox commands for every project.

        Called by the background sweep and once at startup, which resumes commands
        already persisted as pending before a process interruption.
        """
        now = self.now()
        swept = 0
        for project_id in await self.outbox_store.due_projects(now):
            try:
                async with self._project_lock(project_id):
                    agent = await self.get_or_replay_agent(project_id)
                    due_commands = await self.outbox_store.due_commands(project_id, now)
                    delivery_events = await self.outbox_dispatcher.execute_due(
                        project_id, agent.adapter, now=now
                    )
                    await self._persist_delivery_events(agent, delivery_events)
                    for command in due_commands:
                        if (
                            await self.outbox_store.status_of(project_id, command.idempotency_key)
                            == OUTBOX_DEAD
                        ):
                            self._mark_command_delivery_dead(agent, command, now)
                    await self.save_agent_state(agent)
                swept += 1
            except Exception:
                logger.exception("Outbox sweep failed for project %s", project_id)
        return swept

    async def sweep_directive_timeouts(self) -> int:
        """Runs reminder/escalation policy without waiting for another chat message."""
        now = self.now()
        swept = 0
        for project_id in await self.event_store.list_project_ids():
            try:
                async with self._project_lock(project_id):
                    agent = await self.get_or_replay_agent(project_id)
                    manager = self._directive_manager_for(agent)
                    outcome = manager.sweep_timeouts(agent.projector.state, now)
                    if not outcome.events:
                        continue
                    for event in outcome.events:
                        if agent.event_log.append(event) is AppendResult.APPENDED:
                            agent.projector.apply(event)
                        await self.event_store.append(event)
                    await self._deliver_deferred_commands(agent, outcome.outbound)
                    await self.save_agent_state(agent)
                    swept += len(outcome.events)
            except Exception:
                logger.exception("Directive timeout sweep failed for project %s", project_id)
        return swept

    async def outbox_overview(self, project_id: str) -> dict[str, Any]:
        """Recent outbox rows plus undelivered counts for observability endpoints."""
        return {
            "rows": await self.outbox_store.list_rows(project_id),
            "pending_count": await self.outbox_store.pending_count(project_id),
        }

    async def get_sync_coordinator(self, project_id: str) -> ProgressSyncCoordinator:
        """Retrieves or creates a ProgressSyncCoordinator bound to the current project agent.

        Sessions persisted by an earlier gateway run are restored so a restart does
        not orphan probes already sent to team members.
        """
        if project_id not in self._sync_coordinators:
            agent = await self.get_or_replay_agent(project_id)
            coordinator = ProgressSyncCoordinator(
                agent=agent,
                adapter=agent.adapter,
                extractor=self.extractor,
                reference_timezone=self.reference_timezone,
                defer_outbound=True,
                clock=self.now,
            )
            coordinator.restore_sessions(await self.state_store.load_sync_sessions(project_id))
            self._sync_coordinators[project_id] = coordinator
        return self._sync_coordinators[project_id]

    async def start_progress_sync(
        self,
        project_id: str,
        initiated_by: str,
        custom_intro: str | None = None,
    ) -> SyncSession:
        """Starts a distributed progress sync session with active task owners."""
        async with self._project_lock(project_id):
            coordinator = await self.get_sync_coordinator(project_id)
            agent = await self.get_or_replay_agent(project_id)
            coordinator.agent = agent
            coordinator.adapter = agent.adapter
            session = coordinator.start_sync_session(project_id, initiated_by, custom_intro)
            await self.state_store.save_sync_sessions(project_id, coordinator.all_sessions())
            await self._deliver_deferred_commands(agent, coordinator.pop_outbound_commands())
            await self.save_agent_state(agent)
            return session

    async def handle_sync_member_reply(
        self,
        project_id: str,
        member_id: str,
        message: str,
        occurred_at: datetime | None = None,
    ) -> tuple[bool, str | None, SyncSession | None]:
        """Handles reply from probed member, driving clarification or DAG briefing delivery.

        Runs behind the per-project turn lock and rebuilds the agent from the event
        log first, so replies that race with normal message ingestion are decided
        against the latest persisted state, and the mutated session is checkpointed
        to survive a restart mid-collection.
        """
        async with self._project_lock(project_id):
            coordinator = await self.get_sync_coordinator(project_id)
            active_session = coordinator.get_active_session(project_id)
            if not active_session:
                return True, None, None

            agent = await self.get_or_replay_agent(project_id)
            coordinator.agent = agent
            coordinator.adapter = agent.adapter

            converged, clarification_q = coordinator.handle_member_reply(
                session_id=active_session.session_id,
                member_id=member_id,
                message=message,
                occurred_at=occurred_at,
            )

            # Save any new events generated during the turn. Re-appending the full
            # in-memory log is idempotent: the SQL store deduplicates by event id.
            for evt in coordinator.agent.event_log.all():
                await self.event_store.append(evt)

            await self._settle_turn_outbound(agent, occurred_at or self.now())
            await self._deliver_deferred_commands(agent, coordinator.pop_outbound_commands())
            await self.save_agent_state(agent)
            await self.state_store.save_sync_sessions(project_id, coordinator.all_sessions())
        return converged, clarification_q, active_session

    async def force_complete_sync(self, project_id: str) -> SyncSession | None:
        """Force-closes the live sync session, marking unresponsive probes and
        synthesizing the executive briefing from collected replies."""
        async with self._project_lock(project_id):
            coordinator = await self.get_sync_coordinator(project_id)
            active_session = coordinator.get_active_session(project_id)
            if not active_session:
                return None

            agent = await self.get_or_replay_agent(project_id)
            coordinator.agent = agent
            coordinator.adapter = agent.adapter

            coordinator.force_complete_session(active_session.session_id)

            for evt in coordinator.agent.event_log.all():
                await self.event_store.append(evt)
            await self._settle_turn_outbound(agent, self.now())
            await self._deliver_deferred_commands(agent, coordinator.pop_outbound_commands())
            await self.save_agent_state(agent)
            await self.state_store.save_sync_sessions(project_id, coordinator.all_sessions())
            return active_session

"""Gateway coordination service orchestrating persistence and Agent execution."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.agent.loop import CoordinationAgent
from orgpilot.coordination.sync_coordinator import ProgressSyncCoordinator
from orgpilot.domain.models import AgentTurnTrace
from orgpilot.domain.sync_models import SyncSession
from orgpilot.events.log import AppendResult
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
    OrgEvent,
    parse_event,
)
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import MessageContext
from orgpilot.storage.database import Database
from orgpilot.storage.event_store import SqlEventStore
from orgpilot.storage.state_store import SqlStateStore


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
    ) -> None:
        self.db = db
        self.event_store = SqlEventStore(db)
        self.state_store = SqlStateStore(db)
        self.extractor = extractor or ClaimExtractor()
        self.reference_timezone = reference_timezone
        self.adapter_factory = adapter_factory or (
            lambda project_id: MockCollaborationAdapter(project_id=project_id)
        )
        self._sync_coordinators: dict[str, ProgressSyncCoordinator] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}

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
        """Runs the synchronous deterministic kernel off-loop and persists all outputs.

        Turns for the same project are serialized behind a per-project lock, and the
        agent is refreshed from the event log before deciding, so replies arriving
        concurrently from multiple members cannot duplicate coordination actions.
        """
        async with self._project_lock(agent.project_id):
            await self._refresh_agent_from_store(agent)
            trace, generated_events = await asyncio.to_thread(
                agent.run_turn,
                events,
                current_time,
            )
            for event in generated_events:
                await self.event_store.append(event)
            await self.save_agent_state(agent)
        return trace, generated_events

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
    ) -> tuple[bool, list[OrgEvent], CoordinationAgent, str | None, int | None]:
        """Extracts claims from natural language message, persists events, and optionally
        executes a turn.

        Events are projected in memory BEFORE being persisted: if the domain rejects
        anything (and unknown senders are auto-registered, so it normally should not),
        the persistent event log stays replayable instead of being bricked by an
        event that can never be projected.
        """
        ts = occurred_at or datetime.now(UTC)
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

        extraction_result, extracted_events = self.extractor.extract_from_message(message, context)
        ingest_events.extend(extracted_events)

        # Project in memory first; a domain rejection here leaves the SQL log untouched.
        for evt in ingest_events:
            if agent.event_log.append(evt) is AppendResult.APPENDED:
                agent.projector.apply(evt)
        for evt in ingest_events:
            await self.event_store.append(evt)

        turn_reason: str | None = None
        round_num: int | None = None

        if auto_run_turn and extracted_events:
            turn_trace, _ = await self.run_agent_turn(agent, [], ts)
            turn_reason = turn_trace.termination_reason.value
            round_num = turn_trace.round_number

        return (
            extraction_result.is_actionable,
            extracted_events,
            agent,
            turn_reason,
            round_num,
        )

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
        coordinator = await self.get_sync_coordinator(project_id)
        # Ensure latest state
        agent = await self.get_or_replay_agent(project_id)
        coordinator.agent = agent
        coordinator.adapter = agent.adapter
        session = coordinator.start_sync_session(project_id, initiated_by, custom_intro)
        await self.state_store.save_sync_sessions(project_id, coordinator.all_sessions())
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

            await self.state_store.save_state(coordinator.agent.projector.state)
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
            await self.state_store.save_state(coordinator.agent.projector.state)
            await self.state_store.save_sync_sessions(project_id, coordinator.all_sessions())
            return active_session

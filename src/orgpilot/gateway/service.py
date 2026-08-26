"""Gateway coordination service orchestrating persistence and Agent execution."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.agent.loop import CoordinationAgent
from orgpilot.domain.models import AgentTurnTrace
from orgpilot.events.log import AppendResult
from orgpilot.events.models import OrgEvent, parse_event
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
    ) -> None:
        self.db = db
        self.event_store = SqlEventStore(db)
        self.state_store = SqlStateStore(db)
        self.extractor = extractor or ClaimExtractor()
        self.adapter_factory = adapter_factory or (
            lambda project_id: MockCollaborationAdapter(project_id=project_id)
        )

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
        """Runs the synchronous deterministic kernel off-loop and persists all outputs."""
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
        """
        ts = occurred_at or datetime.now(UTC)
        agent = await self.get_or_replay_agent(project_id)

        tasks_dict = {t.task_id: t.title for t in agent.projector.state.tasks.values()}
        members_dict = {m.member_id: m.role for m in agent.projector.state.members.values()}

        context = MessageContext(
            project_id=project_id,
            actor_id=actor_id,
            occurred_at=ts,
            source_ref=source_ref,
            known_tasks=tasks_dict,
            known_members=members_dict,
        )

        extraction_result, extracted_events = self.extractor.extract_from_message(message, context)

        # Persist extracted events
        for evt in extracted_events:
            await self.event_store.append(evt)

        turn_reason: str | None = None
        round_num: int | None = None

        if auto_run_turn and extracted_events:
            turn_trace, _ = await self.run_agent_turn(agent, extracted_events, ts)
            turn_reason = turn_trace.termination_reason.value
            round_num = turn_trace.round_number

        return (
            extraction_result.is_actionable,
            extracted_events,
            agent,
            turn_reason,
            round_num,
        )

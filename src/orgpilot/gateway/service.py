"""Gateway coordination service orchestrating database persistence and Agent loop execution."""

from datetime import datetime, timezone

from orgpilot.agent.loop import CoordinationAgent
from orgpilot.events.log import AppendResult
from orgpilot.events.models import OrgEvent, parse_event
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import MessageContext
from orgpilot.storage.database import Database
from orgpilot.storage.event_store import SqlEventStore
from orgpilot.storage.state_store import SqlStateStore


class GatewayService:
    """Coordinates event ingestion, natural language extraction, and agent execution with SQL persistence."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.event_store = SqlEventStore(db)
        self.state_store = SqlStateStore(db)
        self.extractor = ClaimExtractor()

    async def get_or_replay_agent(self, project_id: str) -> CoordinationAgent:
        """Constructs an Agent with state restored from the persistent SQL event log and stores."""
        agent = CoordinationAgent(project_id=project_id)
        events = await self.event_store.get_events(project_id)

        # Apply existing events to restore projector state
        for evt in events:
            agent.event_log.append(evt)
            agent.projector.apply(evt)

        # Load persisted cases and approvals
        saved_cases = await self.state_store.load_cases(project_id)
        for case in saved_cases:
            agent.case_ledger._cases[case.case_id] = case

        saved_approvals = await self.state_store.load_approvals(project_id)
        for req in saved_approvals:
            agent.approval_manager._requests[req.approval_id] = req

        return agent

    async def save_agent_state(self, agent: CoordinationAgent) -> None:
        """Persists agent state, cases, and approvals to SQL."""
        await self.state_store.save_state(agent.projector.state)
        await self.state_store.save_cases(
            agent.project_id, agent.case_ledger.get_all_cases()
        )
        await self.state_store.save_approvals(
            agent.project_id, agent.approval_manager.get_all_requests()
        )

    async def ingest_raw_events(
        self, project_id: str, raw_event_dicts: list[dict]
    ) -> tuple[int, int]:
        """Parses and persists raw event dictionaries."""
        appended = 0
        duplicates = 0
        for item in raw_event_dicts:
            evt = parse_event(item)
            res = await self.event_store.append(evt)
            if res is AppendResult.APPENDED:
                appended += 1
            else:
                duplicates += 1
        return appended, duplicates

    async def ingest_message(
        self,
        project_id: str,
        message: str,
        actor_id: str,
        occurred_at: datetime | None = None,
        auto_run_turn: bool = True,
    ) -> tuple[bool, list[OrgEvent], CoordinationAgent, str | None, int | None]:
        """Extracts claims from natural language message, persists events, and optionally executes a turn."""
        ts = occurred_at or datetime.now(timezone.utc)
        agent = await self.get_or_replay_agent(project_id)

        tasks_dict = {
            t.task_id: t.title for t in agent.projector.state.tasks.values()
        }
        members_dict = {
            m.member_id: m.role for m in agent.projector.state.members.values()
        }

        context = MessageContext(
            project_id=project_id,
            actor_id=actor_id,
            occurred_at=ts,
            known_tasks=tasks_dict,
            known_members=members_dict,
        )

        extraction_result, extracted_events = self.extractor.extract_from_message(
            message, context
        )

        # Persist extracted events
        for evt in extracted_events:
            await self.event_store.append(evt)

        turn_reason: str | None = None
        round_num: int | None = None

        if auto_run_turn and extracted_events:
            turn_trace, _ = agent.run_turn(extracted_events, ts)
            turn_reason = turn_trace.termination_reason.value
            round_num = turn_trace.round_number
            await self.save_agent_state(agent)

        return (
            extraction_result.is_actionable,
            extracted_events,
            agent,
            turn_reason,
            round_num,
        )

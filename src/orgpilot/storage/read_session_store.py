"""Store for bounded project read sessions and their budget ledger (ADR-0015 §7.5).

Reservation semantics frozen here:

- ``reserve`` persists worst-case spend **before** execution and rejects asks
  that would exceed any cumulative dimension — the model cannot grant itself
  budget.
- ``settle`` converts one reservation into actual usage; ``actual=None``
  (unknown result) keeps the whole reservation as *unknown* consumption and
  never records it as zero.
- A redelivered message reuses its session; a finished session cannot be
  finished twice; ``mark_interrupted`` keeps used and reserved budget, so an
  interruption is never reset into a fresh budget.
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.knowledge.errors import (
    KnowledgeContractError,
    ReadSessionConflict,
)
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetLimits,
    BudgetReservation,
    BudgetUsage,
    ProjectContextPacket,
    ProjectReadSession,
    ProjectReadSessionSpec,
    ReadSessionStatus,
    ReadStepKind,
    ReadStepRecord,
    StopReason,
)
from orgpilot.storage.database import Database, coowned_session
from orgpilot.storage.models import (
    ProjectContextPacketRecord,
    ProjectReadSessionRecord,
    ProjectReadStepRecord,
)
from orgpilot.storage.project_source_store import _required, _to_utc, dump_json


class SqlReadSessionStore:
    """Bounded read sessions, reservations, settlement, steps, and packets."""

    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock: Clock = clock or SystemClock()

    async def create_session(
        self, spec: ProjectReadSessionSpec, session: AsyncSession | None = None
    ) -> bool:
        """Creates the session row; returns False when the message already has one."""
        existing = await self.get_by_message(spec.project_id, spec.message_key, session)
        if existing is not None:
            return False
        now = _to_utc(self.clock.now())
        async with coowned_session(self.db, session) as owned:
            owned.add(
                ProjectReadSessionRecord(
                    session_id=spec.session_id,
                    project_id=spec.project_id,
                    message_key=spec.message_key,
                    question=spec.question,
                    question_hash=spec.question_hash,
                    requester_id=spec.requester_id,
                    binding_version=spec.binding_version,
                    access_epoch=spec.access_epoch,
                    generation_id=spec.generation_id,
                    org_state_refs_json=dump_json(list(spec.org_state_refs)),
                    budget_limits_json=dump_json(spec.budget_limits.model_dump(mode="json")),
                    used_budget_json=dump_json(BudgetUsage().model_dump(mode="json")),
                    reserved_budget_json=dump_json([]),
                    status=ReadSessionStatus.RUNNING.value,
                    created_at=now,
                    updated_at=now,
                )
            )
        return True

    async def get_by_message(
        self, project_id: str, message_key: str, session: AsyncSession | None = None
    ) -> ProjectReadSession | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(ProjectReadSessionRecord).where(
                        ProjectReadSessionRecord.project_id == project_id,
                        ProjectReadSessionRecord.message_key == message_key,
                    )
                )
            ).scalar_one_or_none()
        return self._session_from(record) if record is not None else None

    async def get_session(
        self, session_id: str, session: AsyncSession | None = None
    ) -> ProjectReadSession | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(ProjectReadSessionRecord).where(
                        ProjectReadSessionRecord.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
        return self._session_from(record) if record is not None else None

    async def reserve(
        self,
        session_id: str,
        reservation: BudgetReservation,
        session: AsyncSession | None = None,
    ) -> bool:
        """Persists a reservation before execution; rejects over-budget asks."""
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(ProjectReadSessionRecord).where(
                        ProjectReadSessionRecord.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"read session {session_id} not found")
            if record.status != ReadSessionStatus.RUNNING.value:
                raise ReadSessionConflict(f"session {session_id} is not running")
            reserved = self._reservations(record)
            if any(r.reservation_id == reservation.reservation_id for r in reserved):
                raise ReadSessionConflict("duplicate reservation identity")
            limits = BudgetLimits.model_validate(json.loads(record.budget_limits_json))
            used = BudgetUsage.model_validate(json.loads(record.used_budget_json))
            projected = used
            for item in reserved:
                projected = projected.plus(item.usage(unknown=True))
            projected = projected.plus(reservation.usage(unknown=True))
            self._check_within_limits(limits, projected, reservation.reservation_id)
            reserved.append(reservation)
            record.reserved_budget_json = dump_json(
                [item.model_dump(mode="json") for item in reserved]
            )
            record.updated_at = _to_utc(self.clock.now())
        return True

    async def settle(
        self,
        session_id: str,
        reservation_id: str,
        actual: BudgetUsage | None,
        session: AsyncSession | None = None,
    ) -> BudgetUsage:
        """Settles one reservation and returns what was actually charged.

        ``actual=None`` (result unknown, model never answered) keeps the whole
        reservation as unknown consumption instead of zeroing it.
        """
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(ProjectReadSessionRecord).where(
                        ProjectReadSessionRecord.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"read session {session_id} not found")
            reserved = self._reservations(record)
            match = next((item for item in reserved if item.reservation_id == reservation_id), None)
            if match is None:
                raise KnowledgeContractError(
                    f"reservation {reservation_id} not found on session {session_id}"
                )
            remaining = [item for item in reserved if item.reservation_id != reservation_id]
            settled = match.usage(unknown=True) if actual is None else actual
            used = BudgetUsage.model_validate(json.loads(record.used_budget_json)).plus(settled)
            record.used_budget_json = dump_json(used.model_dump(mode="json"))
            record.reserved_budget_json = dump_json(
                [item.model_dump(mode="json") for item in remaining]
            )
            record.updated_at = _to_utc(self.clock.now())
        return settled

    async def release(self, session_id: str, reservation_id: str) -> None:
        """Release a hold that was never attempted; unknown attempts use settle(None)."""
        async with self.db.session() as session:
            record = (
                await session.execute(
                    select(ProjectReadSessionRecord).where(
                        ProjectReadSessionRecord.session_id == session_id,
                    )
                )
            ).scalar_one()
            record.reserved_budget_json = dump_json(
                [
                    item.model_dump(mode="json")
                    for item in self._reservations(record)
                    if item.reservation_id != reservation_id
                ]
            )

    async def add_usage(self, session_id: str, usage: BudgetUsage) -> BudgetUsage:
        """Persist non-request counters, without overwriting settled model consumption."""
        async with self.db.session() as session:
            record = (
                await session.execute(
                    select(ProjectReadSessionRecord).where(
                        ProjectReadSessionRecord.session_id == session_id,
                    )
                )
            ).scalar_one()
            used = BudgetUsage.model_validate(json.loads(record.used_budget_json)).plus(usage)
            record.used_budget_json = dump_json(used.model_dump(mode="json"))
            record.updated_at = _to_utc(self.clock.now())
        return used

    async def append_step(self, step: ReadStepRecord, session: AsyncSession | None = None) -> None:
        """Appends one auditable step row (no hidden model reasoning stored)."""
        async with coowned_session(self.db, session) as owned:
            owned.add(
                ProjectReadStepRecord(
                    session_id=step.session_id,
                    seq=step.seq,
                    kind=step.kind.value,
                    tool_name=step.tool_name,
                    params_summary_json=dump_json(step.params_summary),
                    result_summary_json=dump_json(step.result_summary),
                    reservation_json=(
                        dump_json(step.reservation.model_dump(mode="json"))
                        if step.reservation is not None
                        else None
                    ),
                    usage_json=(
                        dump_json(step.usage.model_dump(mode="json"))
                        if step.usage is not None
                        else None
                    ),
                    outcome=step.outcome,
                    occurred_at=_to_utc(step.occurred_at),
                )
            )

    async def list_steps(
        self, session_id: str, session: AsyncSession | None = None
    ) -> list[ReadStepRecord]:
        async with coowned_session(self.db, session) as owned:
            records = list(
                (
                    await owned.execute(
                        select(ProjectReadStepRecord)
                        .where(ProjectReadStepRecord.session_id == session_id)
                        .order_by(ProjectReadStepRecord.seq.asc())
                    )
                ).scalars()
            )
        steps: list[ReadStepRecord] = []
        for record in records:
            steps.append(
                ReadStepRecord(
                    session_id=record.session_id,
                    seq=record.seq,
                    kind=ReadStepKind(record.kind),
                    tool_name=record.tool_name,
                    params_summary=dict(json.loads(record.params_summary_json)),
                    result_summary=dict(json.loads(record.result_summary_json)),
                    reservation=(
                        BudgetReservation.model_validate(json.loads(record.reservation_json))
                        if record.reservation_json
                        else None
                    ),
                    usage=(
                        BudgetUsage.model_validate(json.loads(record.usage_json))
                        if record.usage_json
                        else None
                    ),
                    outcome=record.outcome,
                    occurred_at=_required(record.occurred_at),
                )
            )
        return steps

    async def finish_session(
        self,
        session_id: str,
        stop_reason: StopReason,
        answer_status: AnswerStatus,
        answer: dict[str, Any] | None = None,
        final_budget: BudgetUsage | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Closes a running session exactly once, freezing its final budget.

        ``final_budget`` is the loop accountant's authoritative snapshot — it
        covers tool calls and evidence that never passed model reservations.
        """
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(ProjectReadSessionRecord).where(
                        ProjectReadSessionRecord.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"read session {session_id} not found")
            if record.status != ReadSessionStatus.RUNNING.value:
                raise ReadSessionConflict(f"session {session_id} already {record.status}")
            record.status = ReadSessionStatus.COMPLETED.value
            record.stop_reason = stop_reason.value
            record.answer_status = answer_status.value
            record.answer_json = dump_json(answer) if answer is not None else None
            if final_budget is not None:
                stored = BudgetUsage.model_validate(json.loads(record.used_budget_json))
                # Finishing can add controller counters; it must never refund a
                # settled request or an unknown worst-case reservation.
                merged = {
                    name: max(getattr(stored, name), getattr(final_budget, name))
                    for name in BudgetUsage.model_fields
                }
                record.used_budget_json = dump_json(BudgetUsage(**merged).model_dump(mode="json"))
            record.updated_at = _to_utc(self.clock.now())

    async def mark_interrupted(self, session_id: str) -> None:
        """Marks a session interrupted, keeping used and reserved budget (§7.5)."""
        async with self.db.session() as owned:
            record = (
                await owned.execute(
                    select(ProjectReadSessionRecord).where(
                        ProjectReadSessionRecord.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"read session {session_id} not found")
            if record.status != ReadSessionStatus.RUNNING.value:
                return
            record.status = ReadSessionStatus.INTERRUPTED.value
            record.stop_reason = StopReason.INTERRUPTED.value
            record.updated_at = _to_utc(self.clock.now())

    async def save_packet(
        self, packet: ProjectContextPacket, session: AsyncSession | None = None
    ) -> None:
        async with coowned_session(self.db, session) as owned:
            owned.add(
                ProjectContextPacketRecord(
                    packet_id=packet.packet_id,
                    session_id=packet.read_session_id,
                    round=packet.round,
                    generation_id=packet.generation_id,
                    packet_json=dump_json(packet.model_dump(mode="json")),
                    created_at=_to_utc(packet.created_at),
                )
            )

    async def list_packets(self, session_id: str) -> list[ProjectContextPacket]:
        async with self.db.session() as owned:
            records = list(
                (
                    await owned.execute(
                        select(ProjectContextPacketRecord)
                        .where(ProjectContextPacketRecord.session_id == session_id)
                        .order_by(ProjectContextPacketRecord.round.asc())
                    )
                ).scalars()
            )
        return [
            ProjectContextPacket.model_validate(json.loads(record.packet_json))
            for record in records
        ]

    @staticmethod
    def _reservations(record: ProjectReadSessionRecord) -> list[BudgetReservation]:
        return [
            BudgetReservation.model_validate(item)
            for item in json.loads(record.reserved_budget_json)
        ]

    @staticmethod
    def _check_within_limits(
        limits: BudgetLimits, projected: BudgetUsage, reservation_id: str
    ) -> None:
        """Rejects a reservation that would exceed any cumulative dimension."""
        violations: list[str] = []
        if projected.tool_calls > limits.max_tool_calls:
            violations.append(f"tool_calls {projected.tool_calls}>{limits.max_tool_calls}")
        if limits.max_spend_usd is not None and projected.cost_usd > limits.max_spend_usd:
            violations.append(f"cost_usd {projected.cost_usd}>{limits.max_spend_usd}")
        if projected.model_requests > limits.max_model_requests:
            violations.append(
                f"model_requests {projected.model_requests}>{limits.max_model_requests}"
            )
        if projected.total_tokens() > limits.max_total_tokens:
            violations.append(f"total_tokens {projected.total_tokens()}>{limits.max_total_tokens}")
        if projected.evidence_fragments > limits.max_evidence_fragments:
            violations.append(
                f"evidence_fragments {projected.evidence_fragments}>{limits.max_evidence_fragments}"
            )
        if projected.evidence_chars > limits.max_evidence_chars:
            violations.append(
                f"evidence_chars {projected.evidence_chars}>{limits.max_evidence_chars}"
            )
        if violations:
            raise KnowledgeContractError(
                f"reservation {reservation_id} exceeds budget: " + "; ".join(violations)
            )

    @staticmethod
    def _session_from(record: ProjectReadSessionRecord) -> ProjectReadSession:
        return ProjectReadSession(
            spec=ProjectReadSessionSpec(
                session_id=record.session_id,
                project_id=record.project_id,
                message_key=record.message_key,
                question=record.question,
                question_hash=record.question_hash,
                requester_id=record.requester_id,
                binding_version=record.binding_version,
                access_epoch=record.access_epoch,
                generation_id=record.generation_id,
                org_state_refs=tuple(json.loads(record.org_state_refs_json)),
                budget_limits=BudgetLimits.model_validate(json.loads(record.budget_limits_json)),
                created_at=_required(record.created_at),
            ),
            status=ReadSessionStatus(record.status),
            used_budget=BudgetUsage.model_validate(json.loads(record.used_budget_json)),
            reserved_budget=tuple(SqlReadSessionStore._reservations(record)),
            stop_reason=StopReason(record.stop_reason) if record.stop_reason else None,
            answer_status=(AnswerStatus(record.answer_status) if record.answer_status else None),
            answer_json=record.answer_json,
            updated_at=_required(record.updated_at),
        )

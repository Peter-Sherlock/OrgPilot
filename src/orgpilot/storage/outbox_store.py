"""Durable outbound command store (transactional outbox) backed by SQL."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from orgpilot.domain.models import ActionCommand
from orgpilot.storage.database import Database
from orgpilot.storage.models import OutboxRecord

OUTBOX_PENDING = "pending"
OUTBOX_DELIVERED = "delivered"
OUTBOX_DEAD = "dead"


class SqlOutboxStore:
    """Async manager for the persistent outbox: enqueue, claim due rows, settle outcomes.

    ``enqueue`` is idempotent per (project_id, idempotency_key): a crash between
    "event persisted" and "command sent" leaves a pending row that a later sweep
    delivers exactly once.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def enqueue(
        self,
        project_id: str,
        command: ActionCommand,
        now: datetime,
        next_attempt_at: datetime | None = None,
    ) -> bool:
        """Persists an outbound command as pending. Returns False if already enqueued."""
        async with self.db.session() as session:
            stmt = select(OutboxRecord).where(
                OutboxRecord.project_id == project_id,
                OutboxRecord.idempotency_key == command.idempotency_key,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                return False
            session.add(
                OutboxRecord(
                    project_id=project_id,
                    command_id=command.command_id,
                    action_type=command.action_type.value,
                    idempotency_key=command.idempotency_key,
                    status=OUTBOX_PENDING,
                    attempts=0,
                    command_json=command.model_dump_json(),
                    next_attempt_at=next_attempt_at or now,
                    created_at=now,
                    updated_at=now,
                )
            )
            return True

    async def record_completed(
        self, project_id: str, command: ActionCommand, now: datetime
    ) -> bool:
        """Persists an already-executed command as delivered (agent-loop fast path)."""
        async with self.db.session() as session:
            stmt = select(OutboxRecord).where(
                OutboxRecord.project_id == project_id,
                OutboxRecord.idempotency_key == command.idempotency_key,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                if existing.status == OUTBOX_PENDING:
                    existing.status = OUTBOX_DELIVERED
                    existing.updated_at = now
                    return True
                return False
            session.add(
                OutboxRecord(
                    project_id=project_id,
                    command_id=command.command_id,
                    action_type=command.action_type.value,
                    idempotency_key=command.idempotency_key,
                    status=OUTBOX_DELIVERED,
                    attempts=1,
                    command_json=command.model_dump_json(),
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            return True

    async def due_commands(self, project_id: str, now: datetime) -> list[ActionCommand]:
        """Returns pending commands whose next attempt is due, oldest first."""
        async with self.db.session() as session:
            stmt = (
                select(OutboxRecord)
                .where(
                    OutboxRecord.project_id == project_id,
                    OutboxRecord.status == OUTBOX_PENDING,
                    OutboxRecord.next_attempt_at <= now,
                )
                .order_by(OutboxRecord.id)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [ActionCommand.model_validate_json(row.command_json) for row in rows]

    async def settle(
        self,
        project_id: str,
        idempotency_key: str,
        *,
        delivered: bool,
        attempts: int,
        error: str | None,
        retry_at: datetime,
        now: datetime,
        dead: bool = False,
    ) -> None:
        """Records a delivery attempt outcome."""
        status = OUTBOX_DELIVERED if delivered else (OUTBOX_DEAD if dead else OUTBOX_PENDING)
        async with self.db.session() as session:
            stmt = select(OutboxRecord).where(
                OutboxRecord.project_id == project_id,
                OutboxRecord.idempotency_key == idempotency_key,
            )
            rec = (await session.execute(stmt)).scalar_one_or_none()
            if rec is None:
                return
            rec.status = status
            rec.attempts = attempts
            rec.last_error = error
            rec.next_attempt_at = retry_at
            rec.updated_at = now

    async def due_projects(self, now: datetime) -> list[str]:
        """Returns project ids that have due pending commands, for the sweep loop."""
        async with self.db.session() as session:
            stmt = select(OutboxRecord.project_id).where(
                OutboxRecord.status == OUTBOX_PENDING,
                OutboxRecord.next_attempt_at <= now,
            )
            rows = (await session.execute(stmt)).scalars().all()
            return sorted(set(rows))

    async def attempts_of(self, project_id: str, idempotency_key: str) -> int:
        """Returns recorded attempts for a row, or 0 when it does not exist yet."""
        async with self.db.session() as session:
            stmt = select(OutboxRecord).where(
                OutboxRecord.project_id == project_id,
                OutboxRecord.idempotency_key == idempotency_key,
            )
            rec = (await session.execute(stmt)).scalar_one_or_none()
            return int(rec.attempts) if rec is not None else 0

    async def list_rows(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Returns recent outbox rows for observability endpoints."""
        async with self.db.session() as session:
            stmt = (
                select(OutboxRecord)
                .where(OutboxRecord.project_id == project_id)
                .order_by(OutboxRecord.id.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "command_id": row.command_id,
                    "action_type": row.action_type,
                    "idempotency_key": row.idempotency_key,
                    "status": row.status,
                    "attempts": row.attempts,
                    "last_error": row.last_error,
                    "next_attempt_at": row.next_attempt_at.isoformat(),
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]

    async def pending_count(self, project_id: str) -> int:
        """Counts undelivered rows (pending retries plus dead letters)."""
        async with self.db.session() as session:
            stmt = select(OutboxRecord).where(
                OutboxRecord.project_id == project_id,
                OutboxRecord.status.in_((OUTBOX_PENDING, OUTBOX_DEAD)),
            )
            rows = (await session.execute(stmt)).scalars().all()
            return len(list(rows))

    @staticmethod
    def decode_command(row: OutboxRecord) -> ActionCommand:
        """Parses a stored command payload."""
        return ActionCommand.model_validate(json.loads(row.command_json))

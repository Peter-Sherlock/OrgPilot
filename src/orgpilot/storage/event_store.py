"""Persistent async Event Store backed by SQLAlchemy with idempotency and conflict rejection."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from orgpilot.domain.errors import DuplicateEventConflict
from orgpilot.events.log import AppendResult
from orgpilot.events.models import OrgEvent, parse_event
from orgpilot.storage.database import Database
from orgpilot.storage.models import EventRecord


class SqlEventStore:
    """Async append-only SQL event store with exact deduplication and conflict protection."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _compute_hash(payload_data: dict) -> str:
        serialized = json.dumps(payload_data, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def append(self, event: OrgEvent) -> AppendResult:
        """Appends an OrgEvent to the persistent log or detects duplicates/conflicts."""
        payload_dict = event.payload.model_dump(mode="json")
        payload_hash = self._compute_hash(payload_dict)

        async with self.db.session() as session:
            stmt = select(EventRecord).where(
                EventRecord.project_id == event.project_id,
                EventRecord.event_id == event.event_id,
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing is not None:
                if existing.payload_hash == payload_hash:
                    return AppendResult.DUPLICATE
                raise DuplicateEventConflict(
                    f"event {event.event_id} already exists with different payload"
                )

            record = EventRecord(
                project_id=event.project_id,
                event_id=event.event_id,
                event_type=event.event_type,
                source=event.source.value if hasattr(event.source, "value") else str(event.source),
                source_ref=event.source_ref,
                actor_id=event.actor_id,
                occurred_at=event.occurred_at.astimezone(UTC),
                received_at=event.received_at.astimezone(UTC),
                payload_json=json.dumps(payload_dict, default=str),
                payload_hash=payload_hash,
            )
            session.add(record)
            return AppendResult.APPENDED

    async def get_events(self, project_id: str, since: datetime | None = None) -> list[OrgEvent]:
        """Retrieves ordered immutable events for a project."""
        async with self.db.session() as session:
            stmt = select(EventRecord).where(EventRecord.project_id == project_id)
            if since is not None:
                stmt = stmt.where(EventRecord.occurred_at >= since)
            stmt = stmt.order_by(EventRecord.occurred_at.asc(), EventRecord.id.asc())

            result = await session.execute(stmt)
            records = result.scalars().all()

            events: list[OrgEvent] = []
            for rec in records:
                occ = (
                    rec.occurred_at
                    if rec.occurred_at.tzinfo is not None
                    else rec.occurred_at.replace(tzinfo=UTC)
                )
                rec_at = (
                    rec.received_at
                    if rec.received_at.tzinfo is not None
                    else rec.received_at.replace(tzinfo=UTC)
                )
                raw_event_dict = {
                    "schema_version": 1,
                    "project_id": rec.project_id,
                    "event_id": rec.event_id,
                    "event_type": rec.event_type,
                    "source": rec.source,
                    "source_ref": rec.source_ref,
                    "actor_id": rec.actor_id,
                    "occurred_at": occ.isoformat(),
                    "received_at": rec_at.isoformat(),
                    "payload": json.loads(rec.payload_json),
                }
                events.append(parse_event(raw_event_dict))
            return events

    async def count(self, project_id: str | None = None) -> int:
        """Returns total number of events recorded."""
        async with self.db.session() as session:
            stmt = select(func.count(EventRecord.id))
            if project_id is not None:
                stmt = stmt.where(EventRecord.project_id == project_id)
            result = await session.execute(stmt)
            return result.scalar_one()

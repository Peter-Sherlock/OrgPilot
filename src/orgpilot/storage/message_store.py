"""Persistent processed-message log: message-level idempotency + recent turns.

Keyed by (project_id, message_id): when Feishu redelivers a message the router
returns the stored outcome without re-classifying or re-sending any reply. The
same table doubles as the source for the "recent N conversation turns" context
handed to the intent client (the stored reply_text covers what the bot said).
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from orgpilot.storage.database import Database
from orgpilot.storage.models import Base


class ProcessedMessageRecord(Base):
    """One processed inbound message with its outcome, for idempotent replays."""

    __tablename__ = "processed_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent_summary: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reply_category: Mapped[str] = mapped_column(String(32), nullable=False)
    reply_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlMessageStore:
    """Async store for processed-message outcomes."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def record_outcome(
        self,
        *,
        project_id: str,
        message_id: str,
        sender_id: str,
        message_text: str,
        intent_summary: dict[str, Any],
        reply_category: str,
        reply_text: str,
        response: dict[str, Any],
        created_at: datetime | None = None,
    ) -> None:
        """Persists one outcome; a redelivered message_id keeps the first outcome."""
        existing = await self.get_outcome(project_id, message_id)
        if existing is not None:
            return
        async with self.db.session() as session:
            session.add(
                ProcessedMessageRecord(
                    project_id=project_id,
                    message_id=message_id,
                    sender_id=sender_id,
                    message_text=message_text,
                    intent_summary=json.dumps(intent_summary, ensure_ascii=False, default=str),
                    reply_category=reply_category,
                    reply_text=reply_text,
                    response_json=json.dumps(response, ensure_ascii=False, default=str),
                    created_at=created_at or datetime.now(UTC),
                )
            )

    async def get_outcome(
        self, project_id: str, message_id: str
    ) -> ProcessedMessageRecord | None:
        async with self.db.session() as session:
            stmt = select(ProcessedMessageRecord).where(
                ProcessedMessageRecord.project_id == project_id,
                ProcessedMessageRecord.message_id == message_id,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def recent_turns(
        self, project_id: str, sender_id: str, limit: int = 6
    ) -> list[dict[str, str]]:
        """Up to ``limit`` most recent turns for this sender, oldest first.

        Each turn carries what the human said and what the bot replied, so the
        intent context can include the bot's own questions.
        """
        async with self.db.session() as session:
            stmt = (
                select(ProcessedMessageRecord)
                .where(
                    ProcessedMessageRecord.project_id == project_id,
                    ProcessedMessageRecord.sender_id == sender_id,
                )
                .order_by(
                    ProcessedMessageRecord.created_at.desc(), ProcessedMessageRecord.id.desc()
                )
                .limit(limit)
            )
            rows = list((await session.execute(stmt)).scalars())
        rows.reverse()
        return [
            {
                "sender": row.sender_id,
                "message": row.message_text,
                "reply": row.reply_text,
            }
            for row in rows
        ]

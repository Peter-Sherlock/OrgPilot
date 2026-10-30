"""Persistent in-flight conversation state: dialogue snapshots and sync sessions.

ADR-0013: the router's pending clarifications / sync confirmations and the
coordinator's progress sync sessions are checkpointed at the end of every turn
and restored on first access after a restart. Both go through :meth:`save` so
one turn's dialogue and session changes land in ONE transaction: a restart can
never see a cleared probe question next to a session still waiting for it.

Every persisted datetime must be timezone-aware (the same rule as event
timestamps); pydantic's ISO 8601 round trip then restores the same instant and
the same UTC offset, so expiry and timeout decisions after a restart are
identical to the ones the old process would have made.
"""

import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from orgpilot.domain.sync_models import SyncSession
from orgpilot.intent.dialogue import DialogueSnapshot
from orgpilot.storage.database import Database
from orgpilot.storage.models import DialogueSnapshotRecord, SyncSessionRecord

logger = logging.getLogger(__name__)


def _naive_datetime_paths(value: Any, path: str) -> Iterator[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            yield path
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _naive_datetime_paths(getattr(value, name), f"{path}.{name}")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _naive_datetime_paths(item, f"{path}[{key!r}]")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _naive_datetime_paths(item, f"{path}[{index}]")


def _require_timezone_aware(model: BaseModel, label: str) -> None:
    naive = next(_naive_datetime_paths(model, label), None)
    if naive is not None:
        raise ValueError(f"persisted conversation timestamps must include a timezone: {naive}")


class SqlConversationStore:
    """Async store for dialogue snapshots (one row per project) and sync sessions.

    Writes are skipped when the serialized state equals what this store last
    wrote or read, so a turn that changed nothing costs no write. That cache
    assumes one writer per database, the same single-instance assumption as the
    rest of the gateway.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._persisted: dict[tuple[str, str], str] = {}

    async def load_dialogue(self, project_id: str) -> DialogueSnapshot | None:
        """The project's dialogue snapshot; an unreadable one counts as absent.

        A snapshot written by an older model version must not block every later
        message of the project: it is logged and dropped, which degrades to the
        pre-ADR-0013 behaviour (the pending state is lost), and the next save
        overwrites it.
        """
        async with self.db.session() as session:
            rec = await session.get(DialogueSnapshotRecord, project_id)
        if rec is None:
            return None
        try:
            snapshot = DialogueSnapshot.model_validate_json(rec.state_json)
        except ValidationError as exc:
            logger.warning("Dropping unreadable dialogue snapshot of %r: %s", project_id, exc)
            return None
        self._persisted[("dialogue", project_id)] = rec.state_json
        return snapshot

    async def load_sync_sessions(self, project_id: str) -> list[SyncSession]:
        """All readable sessions of a project in start order (see load_dialogue)."""
        async with self.db.session() as session:
            stmt = (
                select(SyncSessionRecord)
                .where(SyncSessionRecord.project_id == project_id)
                .order_by(SyncSessionRecord.id)
            )
            records = (await session.execute(stmt)).scalars().all()
        sessions: list[SyncSession] = []
        for rec in records:
            try:
                sessions.append(SyncSession.model_validate_json(rec.data_json))
            except ValidationError as exc:
                logger.warning("Dropping unreadable sync session %r: %s", rec.session_id, exc)
                continue
            self._persisted[("sync_session", rec.session_id)] = rec.data_json
        return sessions

    async def save(
        self,
        project_id: str,
        *,
        dialogue: DialogueSnapshot | None = None,
        sync_sessions: Iterable[SyncSession] | None = None,
    ) -> None:
        """Writes what changed since the last save or load, in one transaction."""
        dialogue_json: str | None = None
        if dialogue is not None:
            _require_timezone_aware(dialogue, f"dialogue[{project_id!r}]")
            dialogue_json = dialogue.model_dump_json()
            if self._persisted.get(("dialogue", project_id)) == dialogue_json:
                dialogue_json = None
        changed_sessions: list[tuple[SyncSession, str]] = []
        for sync_session in sync_sessions or ():
            _require_timezone_aware(sync_session, f"sync_session[{sync_session.session_id!r}]")
            data_json = sync_session.model_dump_json()
            if self._persisted.get(("sync_session", sync_session.session_id)) != data_json:
                changed_sessions.append((sync_session, data_json))
        if dialogue_json is None and not changed_sessions:
            return

        now = datetime.now(UTC)
        async with self.db.session() as session:
            if dialogue_json is not None:
                rec = await session.get(DialogueSnapshotRecord, project_id)
                if rec is None:
                    session.add(
                        DialogueSnapshotRecord(
                            project_id=project_id, state_json=dialogue_json, updated_at=now
                        )
                    )
                else:
                    rec.state_json = dialogue_json
                    rec.updated_at = now
            # Sessions are inserted in start order, so the autoincrement id keeps it.
            for sync_session, data_json in changed_sessions:
                stmt = select(SyncSessionRecord).where(
                    SyncSessionRecord.session_id == sync_session.session_id
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    session.add(
                        SyncSessionRecord(
                            session_id=sync_session.session_id,
                            project_id=sync_session.project_id,
                            status=sync_session.status.value,
                            data_json=data_json,
                            updated_at=now,
                        )
                    )
                else:
                    row.status = sync_session.status.value
                    row.data_json = data_json
                    row.updated_at = now

        if dialogue_json is not None:
            self._persisted[("dialogue", project_id)] = dialogue_json
        for sync_session, data_json in changed_sessions:
            self._persisted[("sync_session", sync_session.session_id)] = data_json

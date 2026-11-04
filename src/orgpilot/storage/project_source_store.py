"""Persistent stores for project source bindings, objects, revisions, sync logs.

Every write accepts an optional caller ``session`` so several stores can
publish atomically in one transaction (ADR-0015 C1); with no session, the
store opens and commits a short one of its own. These stores are independent
from the business stores — no method here appends OrgEvents, touches cases or
approvals, or advances any coordination turn (ADR-0015 §6).

Deduplication contracts frozen here (ADR-0015 §4.2):

- Re-observing the latest revision's exact content is idempotent
  (``RevisionOutcome.DUPLICATE``); only observable content change increments
  ``change_sequence``, and A→B→A keeps three revisions. Read time never
  deduplicates, so polling ``observed_at`` is never mistaken for a revision.
- A webhook delivery id with the same payload is a duplicate; with a different
  payload it is a conflict that never overwrites the original record.
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.knowledge.errors import (
    BindingConflict,
    DuplicateDeliveryConflict,
    KnowledgeContractError,
    RevisionConflict,
)
from orgpilot.knowledge.models import (
    InboxStatus,
    IssueSelector,
    ProjectSourceBinding,
    RevisionOutcome,
    SourceAvailability,
    SourceKind,
    SourceObject,
    SourceObjectIdentity,
    SourceRevision,
    SyncCheckpoint,
    SyncInboxEntry,
    SyncRun,
    SyncRunReason,
    SyncRunState,
    binding_uid,
)
from orgpilot.storage.database import Database, coowned_session
from orgpilot.storage.models import (
    KnowledgeGenerationRecord,
    ProjectSourceBindingRecord,
    SourceObjectRecord,
    SourceRevisionRecord,
    SyncInboxRecord,
    SyncRunRecord,
)


def dump_json(data: Any) -> str:
    """Canonical JSON dump for knowledge metadata columns."""
    return json.dumps(data, ensure_ascii=False, default=str)


def load_json(data: str, default: Any) -> Any:
    if not data:
        return default
    return json.loads(data)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips may drop tzinfo; storage is always timezone-aware."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _to_utc(value: datetime | None) -> datetime | None:
    """Normalizes instants to UTC before storing, like SqlEventStore does.

    aiosqlite persists the naive wall time, so writing ``10:00+08:00`` and
    tagging it UTC on read back would shift the instant by the offset. Writing
    UTC wall time keeps the round-trip lossless.
    """
    if value is None:
        return None
    return value.astimezone(UTC)


def _required(value: datetime | None) -> datetime:
    result = _aware(value)
    if result is None:
        raise KnowledgeContractError("required datetime was null")
    return result


class SqlProjectSourceStore:
    """Bindings, source objects, and immutable revisions (ADR-0015 §4)."""

    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock: Clock = clock or SystemClock()

    async def _lock_project(self, project_id: str, session: AsyncSession) -> None:
        await session.execute(
            select(ProjectSourceBindingRecord)
            .where(
                ProjectSourceBindingRecord.project_id == project_id,
            )
            .with_for_update()
        )

    # -- bindings ---------------------------------------------------------------

    async def create_binding(
        self, binding: ProjectSourceBinding, session: AsyncSession | None = None
    ) -> bool:
        """Persists one binding version row; returns False if identical exists.

        A conflicting row (same version, different content) raises instead of
        being silently overwritten.
        """
        existing = await self.get_binding(binding.project_id, binding.binding_version, session)
        if existing is not None:
            if existing != binding:
                raise BindingConflict(
                    f"binding {binding.project_id}@v{binding.binding_version} exists "
                    "with different content"
                )
            return False
        record = ProjectSourceBindingRecord(
            binding_uid=binding_uid(binding.project_id, binding.binding_version),
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            repository_id=binding.repository_id,
            installation_id=binding.installation_id,
            branch=binding.branch,
            path_allowlist_json=dump_json(list(binding.path_allowlist)),
            issue_selector_json=dump_json(binding.issue_selector.model_dump(mode="json")),
            explicit_pr_ids_json=dump_json(list(binding.explicit_pr_ids)),
            maintainers_json=dump_json(list(binding.maintainers)),
            sharing_grants_json=dump_json(list(binding.sharing_grants)),
            authority_rules_json=dump_json(binding.authority_rules),
            access_epoch=binding.access_epoch,
            enabled=binding.enabled,
            created_at=_to_utc(binding.created_at),
        )
        async with coowned_session(self.db, session) as owned:
            owned.add(record)
        return True

    async def get_binding(
        self, project_id: str, binding_version: int, session: AsyncSession | None = None
    ) -> ProjectSourceBinding | None:
        async with coowned_session(self.db, session) as owned:
            stmt = select(ProjectSourceBindingRecord).where(
                ProjectSourceBindingRecord.project_id == project_id,
                ProjectSourceBindingRecord.binding_version == binding_version,
            )
            record = (await owned.execute(stmt)).scalar_one_or_none()
        return self._binding_from(record) if record is not None else None

    async def get_active_binding(
        self, project_id: str, session: AsyncSession | None = None
    ) -> ProjectSourceBinding | None:
        """The newest enabled binding version for a project, if any."""
        async with coowned_session(self.db, session) as owned:
            stmt = (
                select(ProjectSourceBindingRecord)
                .where(
                    ProjectSourceBindingRecord.project_id == project_id,
                    ProjectSourceBindingRecord.enabled.is_(True),
                )
                .order_by(ProjectSourceBindingRecord.binding_version.desc())
                .limit(1)
            )
            record = (await owned.execute(stmt)).scalar_one_or_none()
        return self._binding_from(record) if record is not None else None

    async def set_binding_enabled(
        self,
        project_id: str,
        binding_version: int,
        enabled: bool,
        session: AsyncSession | None = None,
    ) -> None:
        async with coowned_session(self.db, session) as owned:
            await self._lock_project(project_id, owned)
            stmt = select(ProjectSourceBindingRecord).where(
                ProjectSourceBindingRecord.project_id == project_id,
                ProjectSourceBindingRecord.binding_version == binding_version,
            )
            record = (await owned.execute(stmt)).scalar_one_or_none()
            if record is None:
                raise BindingConflict(f"binding {project_id}@v{binding_version} not found")
            record.enabled = enabled

    async def find_active_binding_for_repository(
        self, repository_id: str, session: AsyncSession | None = None
    ) -> ProjectSourceBinding | None:
        """Resolves a webhook's repository to the newest enabled binding."""
        async with coowned_session(self.db, session) as owned:
            stmt = (
                select(ProjectSourceBindingRecord)
                .where(
                    ProjectSourceBindingRecord.repository_id == str(repository_id),
                    ProjectSourceBindingRecord.enabled.is_(True),
                )
                .order_by(ProjectSourceBindingRecord.binding_version.desc())
                .limit(1)
            )
            record = (await owned.execute(stmt)).scalar_one_or_none()
        return self._binding_from(record) if record is not None else None

    @staticmethod
    def _binding_from(record: ProjectSourceBindingRecord) -> ProjectSourceBinding:
        return ProjectSourceBinding(
            project_id=record.project_id,
            repository_id=record.repository_id,
            binding_version=record.binding_version,
            installation_id=record.installation_id,
            branch=record.branch,
            path_allowlist=tuple(load_json(record.path_allowlist_json, [])),
            issue_selector=IssueSelector.model_validate(load_json(record.issue_selector_json, {})),
            explicit_pr_ids=tuple(load_json(record.explicit_pr_ids_json, [])),
            maintainers=tuple(load_json(record.maintainers_json, [])),
            sharing_grants=tuple(load_json(record.sharing_grants_json, [])),
            authority_rules=dict(load_json(record.authority_rules_json, {})),
            access_epoch=record.access_epoch,
            enabled=record.enabled,
            created_at=_required(record.created_at),
        )

    # -- source objects -----------------------------------------------------------

    async def upsert_source_object(
        self, obj: SourceObject, session: AsyncSession | None = None
    ) -> bool:
        """Registers or refreshes one source object; returns True when created.

        Availability is owned by explicit ``set_source_availability`` calls, so
        an observation refresh never silently resurrects a revoked source.
        """
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(SourceObjectRecord).where(SourceObjectRecord.source_id == obj.source_id)
                )
            ).scalar_one_or_none()
            created = record is None
            now = _to_utc(self.clock.now())
            if record is None:
                record = SourceObjectRecord(
                    source_id=obj.source_id,
                    project_id=obj.project_id,
                    binding_uid=binding_uid(obj.project_id, obj.binding_version),
                    binding_version=obj.binding_version,
                    kind=obj.kind.value,
                    external_id=obj.external_id,
                    parent_source_id=obj.parent_source_id,
                    canonical_url=obj.canonical_url,
                    title=obj.title,
                    availability=obj.availability.value,
                    unavailable_reason=obj.unavailable_reason,
                    metadata_json=dump_json(obj.metadata),
                    created_at=now,
                    updated_at=now,
                )
                owned.add(record)
            record.canonical_url = obj.canonical_url
            record.title = obj.title
            record.source_updated_at = _to_utc(obj.source_updated_at)
            record.metadata_json = dump_json(obj.metadata)
            record.updated_at = now
            if obj.latest_revision_id is not None:
                record.latest_revision_id = obj.latest_revision_id
                record.latest_change_sequence = obj.latest_change_sequence
            if obj.last_observed_at is not None:
                record.last_observed_at = _to_utc(obj.last_observed_at)
            if obj.last_verified_at is not None:
                record.last_verified_at = _to_utc(obj.last_verified_at)
        return created

    async def get_source_object(
        self, source_id: str, session: AsyncSession | None = None
    ) -> SourceObject | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(SourceObjectRecord).where(SourceObjectRecord.source_id == source_id)
                )
            ).scalar_one_or_none()
        return self._object_from(record) if record is not None else None

    async def find_source_object(
        self,
        project_id: str,
        binding_version: int,
        kind: SourceKind,
        external_id: str,
        parent_source_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> SourceObject | None:
        identity = SourceObjectIdentity(
            project_id=project_id,
            binding_version=binding_version,
            kind=kind,
            external_id=external_id,
            parent_source_id=parent_source_id,
        )
        return await self.get_source_object(identity.source_id(), session)

    async def list_source_objects(
        self,
        project_id: str,
        binding_version: int,
        kind: SourceKind | None = None,
        session: AsyncSession | None = None,
    ) -> list[SourceObject]:
        async with coowned_session(self.db, session) as owned:
            stmt = select(SourceObjectRecord).where(
                SourceObjectRecord.project_id == project_id,
                SourceObjectRecord.binding_version == binding_version,
            )
            if kind is not None:
                stmt = stmt.where(SourceObjectRecord.kind == kind.value)
            records = list((await owned.execute(stmt)).scalars())
        return [self._object_from(record) for record in records]

    async def set_source_availability(
        self,
        source_id: str,
        availability: SourceAvailability,
        reason: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Marks a source unavailable/revoked/deleted; citing must stop (§5.4).

        A 403/404 is recorded as ``unavailable`` with its reason; only a
        confirmed deletion or revocation uses ``deleted``/``revoked``.
        """
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(SourceObjectRecord).where(SourceObjectRecord.source_id == source_id)
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"source object {source_id} not found")
            await self._lock_project(record.project_id, owned)
            record.availability = availability.value
            record.unavailable_reason = reason
            record.updated_at = _to_utc(self.clock.now())
            await self._invalidate_generations(record.project_id, owned)

    async def _invalidate_generations(self, project_id: str, session: AsyncSession) -> None:
        generations = (
            await session.execute(
                select(KnowledgeGenerationRecord).where(
                    KnowledgeGenerationRecord.project_id == project_id,
                    KnowledgeGenerationRecord.status == "ready",
                    KnowledgeGenerationRecord.valid.is_(True),
                )
            )
        ).scalars()
        for generation in generations:
            generation.valid = False
            generation.invalidated_reason = "source material changed; rebuild required"

    @staticmethod
    def _object_from(record: SourceObjectRecord) -> SourceObject:
        return SourceObject(
            source_id=record.source_id,
            project_id=record.project_id,
            binding_version=record.binding_version,
            kind=SourceKind(record.kind),
            external_id=record.external_id,
            parent_source_id=record.parent_source_id,
            canonical_url=record.canonical_url,
            title=record.title,
            availability=SourceAvailability(record.availability),
            unavailable_reason=record.unavailable_reason,
            latest_revision_id=record.latest_revision_id,
            latest_change_sequence=record.latest_change_sequence,
            source_updated_at=_aware(record.source_updated_at),
            last_observed_at=_aware(record.last_observed_at),
            last_verified_at=_aware(record.last_verified_at),
            metadata=dict(load_json(record.metadata_json, {})),
        )

    # -- revisions -----------------------------------------------------------------

    async def record_revision(
        self, revision: SourceRevision, session: AsyncSession | None = None
    ) -> RevisionOutcome:
        """Appends one immutable revision, or dedups a same-content re-observation.

        Content identity is (source_id, content_hash) against the **latest**
        revision only: reading time never deduplicates, unchanged content never
        re-extracts, and A→B→A records three revisions.
        """
        async with coowned_session(self.db, session) as owned:
            await self._lock_project(revision.project_id, owned)
            latest = (
                await owned.execute(
                    select(SourceRevisionRecord)
                    .where(SourceRevisionRecord.source_id == revision.source_id)
                    .order_by(SourceRevisionRecord.change_sequence.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if latest is not None and latest.content_hash == revision.content_hash:
                obj = (
                    await owned.execute(
                        select(SourceObjectRecord).where(
                            SourceObjectRecord.source_id == revision.source_id
                        )
                    )
                ).scalar_one_or_none()
                if obj is not None:
                    obj.last_observed_at = _to_utc(revision.observed_at)
                    obj.updated_at = _to_utc(self.clock.now())
                return RevisionOutcome.DUPLICATE

            if latest is not None and revision.change_sequence <= latest.change_sequence:
                raise RevisionConflict(
                    f"revision {revision.revision_id} reuses change_sequence "
                    f"{revision.change_sequence}; latest is {latest.change_sequence}"
                )

            owned.add(
                SourceRevisionRecord(
                    revision_id=revision.revision_id,
                    source_id=revision.source_id,
                    project_id=revision.project_id,
                    binding_version=revision.binding_version,
                    access_epoch=revision.access_epoch,
                    kind=revision.kind.value,
                    source_version=revision.source_version,
                    change_sequence=revision.change_sequence,
                    content_hash=revision.content_hash,
                    raw_content=revision.raw_content,
                    metadata_json=dump_json(revision.metadata),
                    source_updated_at=_to_utc(revision.source_updated_at),
                    observed_at=_to_utc(revision.observed_at),
                )
            )
            obj = (
                await owned.execute(
                    select(SourceObjectRecord).where(
                        SourceObjectRecord.source_id == revision.source_id
                    )
                )
            ).scalar_one_or_none()
            if obj is not None:
                obj.latest_revision_id = revision.revision_id
                obj.latest_change_sequence = revision.change_sequence
                obj.last_observed_at = _to_utc(revision.observed_at)
                obj.source_updated_at = _to_utc(revision.source_updated_at) or obj.source_updated_at
                obj.updated_at = _to_utc(self.clock.now())
            await self._invalidate_generations(revision.project_id, owned)
            return RevisionOutcome.APPENDED

    async def get_revision(
        self, revision_id: str, session: AsyncSession | None = None
    ) -> SourceRevision | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(SourceRevisionRecord).where(
                        SourceRevisionRecord.revision_id == revision_id
                    )
                )
            ).scalar_one_or_none()
        return self._revision_from(record) if record is not None else None

    async def latest_revision(
        self, source_id: str, session: AsyncSession | None = None
    ) -> SourceRevision | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(SourceRevisionRecord)
                    .where(SourceRevisionRecord.source_id == source_id)
                    .order_by(SourceRevisionRecord.change_sequence.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return self._revision_from(record) if record is not None else None

    async def list_revisions(
        self, source_id: str, session: AsyncSession | None = None
    ) -> list[SourceRevision]:
        async with coowned_session(self.db, session) as owned:
            records = list(
                (
                    await owned.execute(
                        select(SourceRevisionRecord)
                        .where(SourceRevisionRecord.source_id == source_id)
                        .order_by(SourceRevisionRecord.change_sequence.asc())
                    )
                ).scalars()
            )
        return [self._revision_from(record) for record in records]

    @staticmethod
    def _revision_from(record: SourceRevisionRecord) -> SourceRevision:
        return SourceRevision(
            revision_id=record.revision_id,
            source_id=record.source_id,
            project_id=record.project_id,
            binding_version=record.binding_version,
            access_epoch=record.access_epoch,
            kind=SourceKind(record.kind),
            source_version=record.source_version,
            change_sequence=record.change_sequence,
            content_hash=record.content_hash,
            raw_content=record.raw_content,
            metadata=dict(load_json(record.metadata_json, {})),
            source_updated_at=_aware(record.source_updated_at),
            observed_at=_required(record.observed_at),
        )


class SqlSyncInboxStore:
    """Webhook delivery inbox with payload-conflict rejection (ADR-0015 §5.3)."""

    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock: Clock = clock or SystemClock()

    async def ingest(self, entry: SyncInboxEntry, session: AsyncSession | None = None) -> bool:
        """Persists one delivery before acknowledgement.

        Returns False for an identical redelivery; the same delivery id with a
        different payload raises ``DuplicateDeliveryConflict`` and never
        overwrites the original record.
        """
        async with coowned_session(self.db, session) as owned:
            stmt = select(SyncInboxRecord).where(
                SyncInboxRecord.repository_id == entry.repository_id,
                SyncInboxRecord.delivery_id == entry.delivery_id,
            )
            record = (await owned.execute(stmt)).scalar_one_or_none()
            if record is not None:
                if record.payload_hash != entry.payload_hash:
                    raise DuplicateDeliveryConflict(
                        f"delivery {entry.delivery_id} for repository "
                        f"{entry.repository_id} redelivered with different payload"
                    )
                return False
            owned.add(
                SyncInboxRecord(
                    repository_id=entry.repository_id,
                    delivery_id=entry.delivery_id,
                    event_type=entry.event_type,
                    action=entry.action,
                    payload_hash=entry.payload_hash,
                    payload_json=dump_json(entry.payload),
                    received_at=_to_utc(entry.received_at),
                    status=entry.status.value,
                    attempts=entry.attempts,
                    last_error=entry.last_error,
                    retry_at=_to_utc(entry.retry_at),
                )
            )
            return True

    async def get_entry(
        self, repository_id: str, delivery_id: str, session: AsyncSession | None = None
    ) -> SyncInboxEntry | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(SyncInboxRecord).where(
                        SyncInboxRecord.repository_id == repository_id,
                        SyncInboxRecord.delivery_id == delivery_id,
                    )
                )
            ).scalar_one_or_none()
        return self._entry_from(record) if record is not None else None

    async def set_status(
        self,
        repository_id: str,
        delivery_id: str,
        status: InboxStatus,
        *,
        attempts_delta: int = 0,
        last_error: str | None = None,
        retry_at: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(SyncInboxRecord).where(
                        SyncInboxRecord.repository_id == repository_id,
                        SyncInboxRecord.delivery_id == delivery_id,
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"inbox entry {repository_id}/{delivery_id} not found")
            record.status = status.value
            record.attempts += attempts_delta
            if last_error is not None:
                record.last_error = last_error
            record.retry_at = _to_utc(retry_at)
            if status in (InboxStatus.DONE, InboxStatus.FAILED):
                record.processed_at = _to_utc(self.clock.now())

    async def list_due(
        self, repository_id: str | None = None, now: datetime | None = None, limit: int = 50
    ) -> list[SyncInboxEntry]:
        """Pending/retry entries whose retry_at (if any) has passed."""
        checked = now or _to_utc(self.clock.now())
        async with self.db.session() as owned:
            stmt = select(SyncInboxRecord).where(
                SyncInboxRecord.status.in_(
                    [InboxStatus.PENDING.value, InboxStatus.RETRY_WAIT.value]
                )
            )
            if repository_id is not None:
                stmt = stmt.where(SyncInboxRecord.repository_id == repository_id)
            records = list((await owned.execute(stmt)).scalars())
        entries = [self._entry_from(record) for record in records]
        due = [entry for entry in entries if entry.retry_at is None or entry.retry_at <= checked]
        return due[:limit]

    @staticmethod
    def _entry_from(record: SyncInboxRecord) -> SyncInboxEntry:
        return SyncInboxEntry(
            repository_id=record.repository_id,
            delivery_id=record.delivery_id,
            event_type=record.event_type,
            action=record.action,
            payload_hash=record.payload_hash,
            payload=dict(load_json(record.payload_json, {})),
            received_at=_required(record.received_at),
            status=InboxStatus(record.status),
            attempts=record.attempts,
            last_error=record.last_error,
            retry_at=_aware(record.retry_at),
        )


class SqlSyncRunStore:
    """Sync runs with persisted checkpoints (ADR-0015 §4.1, §5.4)."""

    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock: Clock = clock or SystemClock()

    async def start_run(
        self,
        run_id: str,
        project_id: str,
        binding_version: int,
        access_epoch: int,
        reason: SyncRunReason,
        candidate_manifest: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        now = _to_utc(self.clock.now())
        async with coowned_session(self.db, session) as owned:
            owned.add(
                SyncRunRecord(
                    run_id=run_id,
                    project_id=project_id,
                    binding_version=binding_version,
                    access_epoch=access_epoch,
                    reason=reason.value,
                    state=SyncRunState.RUNNING.value,
                    candidate_manifest_json=dump_json(candidate_manifest or {}),
                    created_at=now,
                    updated_at=now,
                )
            )

    async def save_checkpoint(
        self,
        run_id: str,
        per_source_cursor: dict[str, str],
        coverage: dict[str, str],
        *,
        last_success_at: datetime | None = None,
        retry_at: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Persists the checkpoint together with run progress in one write."""
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(select(SyncRunRecord).where(SyncRunRecord.run_id == run_id))
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"sync run {run_id} not found")
            record.per_source_cursor_json = dump_json(per_source_cursor)
            record.coverage_json = dump_json(coverage)
            record.last_success_at = _to_utc(last_success_at)
            record.retry_at = _to_utc(retry_at)
            record.updated_at = _to_utc(self.clock.now())

    async def set_run_state(
        self,
        run_id: str,
        state: SyncRunState,
        *,
        error: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(select(SyncRunRecord).where(SyncRunRecord.run_id == run_id))
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"sync run {run_id} not found")
            record.state = state.value
            if error is not None:
                record.error = error
            record.updated_at = _to_utc(self.clock.now())

    async def get_run(self, run_id: str, session: AsyncSession | None = None) -> SyncRun | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(select(SyncRunRecord).where(SyncRunRecord.run_id == run_id))
            ).scalar_one_or_none()
        return self._run_from(record) if record is not None else None

    async def latest_run(
        self, project_id: str, session: AsyncSession | None = None
    ) -> SyncRun | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(SyncRunRecord)
                    .where(SyncRunRecord.project_id == project_id)
                    .order_by(SyncRunRecord.created_at.desc(), SyncRunRecord.run_id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return self._run_from(record) if record is not None else None

    @staticmethod
    def _run_from(record: SyncRunRecord) -> SyncRun:
        checkpoint = SyncCheckpoint(
            run_id=record.run_id,
            per_source_cursor=dict(load_json(record.per_source_cursor_json, {})),
            coverage=dict(load_json(record.coverage_json, {})),
            last_success_at=_aware(record.last_success_at),
            retry_at=_aware(record.retry_at),
        )
        return SyncRun(
            run_id=record.run_id,
            project_id=record.project_id,
            binding_version=record.binding_version,
            access_epoch=record.access_epoch,
            reason=SyncRunReason(record.reason),
            state=SyncRunState(record.state),
            candidate_manifest=dict(load_json(record.candidate_manifest_json, {})),
            checkpoint=checkpoint,
            error=record.error,
            created_at=_required(record.created_at),
            updated_at=_required(record.updated_at),
        )

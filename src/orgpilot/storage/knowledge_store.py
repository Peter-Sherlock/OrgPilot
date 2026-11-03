"""Store for assertions, relations, and knowledge generations (ADR-0015 §4.1).

Publishing logic lives with the C4 derivation pipeline; this store freezes the
atomic row access: staging content is invisible until ``publish_generation``
flips both the status and the project pointer in one transaction, and a
published generation is invalidated by a flag instead of content rewriting.
"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.knowledge.errors import KnowledgeContractError
from orgpilot.knowledge.models import (
    GenerationStatus,
    KnowledgeAssertion,
    KnowledgeGeneration,
    KnowledgeRelation,
)
from orgpilot.storage.database import Database, coowned_session
from orgpilot.storage.models import (
    KnowledgeAssertionRecord,
    KnowledgeGenerationPointerRecord,
    KnowledgeGenerationRecord,
    KnowledgeRelationRecord,
)
from orgpilot.storage.project_source_store import _aware, _required, _to_utc, dump_json


class SqlKnowledgeStore:
    """Derived-knowledge rows: assertions, relations, generations, pointer."""

    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self.db = db
        self.clock: Clock = clock or SystemClock()

    async def add_assertion(
        self, assertion: KnowledgeAssertion, session: AsyncSession | None = None
    ) -> None:
        async with coowned_session(self.db, session) as owned:
            owned.add(
                KnowledgeAssertionRecord(
                    assertion_id=assertion.assertion_id,
                    project_id=assertion.project_id,
                    subject_ref=assertion.subject_ref,
                    predicate=assertion.predicate,
                    value=assertion.value,
                    assertion_kind=assertion.assertion_kind.value,
                    evidence_refs_json=dump_json(
                        [ref.model_dump(mode="json") for ref in assertion.evidence_refs]
                    ),
                    extractor_version=assertion.extractor_version,
                    validation_status=assertion.validation_status.value,
                    created_at=_to_utc(self.clock.now()),
                )
            )

    async def list_assertions(
        self, project_id: str, session: AsyncSession | None = None
    ) -> list[KnowledgeAssertion]:
        from orgpilot.knowledge.models import (
            AssertionKind,
            AssertionValidation,
            EvidenceRef,
        )

        async with coowned_session(self.db, session) as owned:
            records = list(
                (
                    await owned.execute(
                        select(KnowledgeAssertionRecord).where(
                            KnowledgeAssertionRecord.project_id == project_id
                        )
                    )
                ).scalars()
            )
        assertions: list[KnowledgeAssertion] = []
        for record in records:
            assertions.append(
                KnowledgeAssertion(
                    assertion_id=record.assertion_id,
                    project_id=record.project_id,
                    subject_ref=record.subject_ref,
                    predicate=record.predicate,
                    value=record.value,
                    assertion_kind=AssertionKind(record.assertion_kind),
                    evidence_refs=tuple(
                        EvidenceRef.model_validate(item)
                        for item in json.loads(record.evidence_refs_json)
                    ),
                    extractor_version=record.extractor_version,
                    validation_status=AssertionValidation(record.validation_status),
                )
            )
        return assertions

    async def add_relation(
        self, relation: KnowledgeRelation, session: AsyncSession | None = None
    ) -> None:
        async with coowned_session(self.db, session) as owned:
            owned.add(
                KnowledgeRelationRecord(
                    relation_id=relation.relation_id,
                    project_id=relation.project_id,
                    from_ref=relation.from_ref,
                    relation_type=relation.relation_type.value,
                    to_ref=relation.to_ref,
                    evidence_refs_json=dump_json(
                        [ref.model_dump(mode="json") for ref in relation.evidence_refs]
                    ),
                    confirmation_status=relation.confirmation_status.value,
                    created_at=_to_utc(self.clock.now()),
                )
            )

    async def list_relations(
        self, project_id: str, session: AsyncSession | None = None
    ) -> list[KnowledgeRelation]:
        from orgpilot.knowledge.models import (
            ConfirmationStatus,
            EvidenceRef,
            RelationType,
        )

        async with coowned_session(self.db, session) as owned:
            records = list(
                (
                    await owned.execute(
                        select(KnowledgeRelationRecord).where(
                            KnowledgeRelationRecord.project_id == project_id
                        )
                    )
                ).scalars()
            )
        relations: list[KnowledgeRelation] = []
        for record in records:
            relations.append(
                KnowledgeRelation(
                    relation_id=record.relation_id,
                    project_id=record.project_id,
                    from_ref=record.from_ref,
                    relation_type=RelationType(record.relation_type),
                    to_ref=record.to_ref,
                    evidence_refs=tuple(
                        EvidenceRef.model_validate(item)
                        for item in json.loads(record.evidence_refs_json)
                    ),
                    confirmation_status=ConfirmationStatus(record.confirmation_status),
                )
            )
        return relations

    async def create_generation(
        self, generation: KnowledgeGeneration, session: AsyncSession | None = None
    ) -> None:
        async with coowned_session(self.db, session) as owned:
            owned.add(
                KnowledgeGenerationRecord(
                    generation_id=generation.generation_id,
                    project_id=generation.project_id,
                    binding_version=generation.binding_version,
                    access_epoch=generation.access_epoch,
                    status=generation.status.value,
                    revision_manifest_json=dump_json(list(generation.revision_manifest)),
                    assertion_refs_json=dump_json(list(generation.assertion_refs)),
                    relation_refs_json=dump_json(list(generation.relation_refs)),
                    summary=generation.summary,
                    derivation_version=generation.derivation_version,
                    coverage_json=dump_json(generation.coverage),
                    valid=generation.valid,
                    created_at=_to_utc(generation.created_at),
                )
            )

    async def publish_generation(
        self, generation_id: str, session: AsyncSession | None = None
    ) -> None:
        """Marks a staging generation ready and flips the project pointer in the
        same transaction; staging content is never visible before this."""
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(KnowledgeGenerationRecord).where(
                        KnowledgeGenerationRecord.generation_id == generation_id
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"generation {generation_id} not found")
            if record.status != GenerationStatus.STAGING.value:
                raise KnowledgeContractError(
                    f"generation {generation_id} is {record.status}, not staging"
                )
            record.status = GenerationStatus.READY.value
            record.published_at = _to_utc(self.clock.now())
            pointer = (
                await owned.execute(
                    select(KnowledgeGenerationPointerRecord).where(
                        KnowledgeGenerationPointerRecord.project_id == record.project_id
                    )
                )
            ).scalar_one_or_none()
            now = _to_utc(self.clock.now())
            if pointer is None:
                owned.add(
                    KnowledgeGenerationPointerRecord(
                        project_id=record.project_id,
                        current_generation_id=generation_id,
                        switched_at=now,
                    )
                )
            else:
                pointer.current_generation_id = generation_id
                pointer.switched_at = now

    async def fail_generation(self, generation_id: str, reason: str) -> None:
        """A failed build never publishes; the error stays visible for audit."""
        async with self.db.session() as owned:
            record = (
                await owned.execute(
                    select(KnowledgeGenerationRecord).where(
                        KnowledgeGenerationRecord.generation_id == generation_id
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"generation {generation_id} not found")
            record.status = GenerationStatus.FAILED.value
            record.invalidated_reason = reason

    async def invalidate_generation(self, generation_id: str, reason: str) -> None:
        """Marks a published generation invalid without rewriting its content."""
        async with self.db.session() as owned:
            record = (
                await owned.execute(
                    select(KnowledgeGenerationRecord).where(
                        KnowledgeGenerationRecord.generation_id == generation_id
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise KnowledgeContractError(f"generation {generation_id} not found")
            record.valid = False
            record.invalidated_reason = reason

    async def get_generation(
        self, generation_id: str, session: AsyncSession | None = None
    ) -> KnowledgeGeneration | None:
        async with coowned_session(self.db, session) as owned:
            record = (
                await owned.execute(
                    select(KnowledgeGenerationRecord).where(
                        KnowledgeGenerationRecord.generation_id == generation_id
                    )
                )
            ).scalar_one_or_none()
        return self._generation_from(record) if record is not None else None

    async def current_generation_id(
        self, project_id: str, session: AsyncSession | None = None
    ) -> str | None:
        async with coowned_session(self.db, session) as owned:
            pointer = (
                await owned.execute(
                    select(KnowledgeGenerationPointerRecord).where(
                        KnowledgeGenerationPointerRecord.project_id == project_id
                    )
                )
            ).scalar_one_or_none()
        return pointer.current_generation_id if pointer is not None else None

    @staticmethod
    def _generation_from(record: KnowledgeGenerationRecord) -> KnowledgeGeneration:
        return KnowledgeGeneration(
            generation_id=record.generation_id,
            project_id=record.project_id,
            binding_version=record.binding_version,
            access_epoch=record.access_epoch,
            status=GenerationStatus(record.status),
            revision_manifest=tuple(json.loads(record.revision_manifest_json)),
            assertion_refs=tuple(json.loads(record.assertion_refs_json)),
            relation_refs=tuple(json.loads(record.relation_refs_json)),
            summary=record.summary,
            derivation_version=record.derivation_version,
            coverage=dict(json.loads(record.coverage_json)),
            valid=record.valid,
            invalidated_reason=record.invalidated_reason,
            created_at=_required(record.created_at),
            published_at=_aware(record.published_at),
        )

"""Deterministic derivation: segments, assertions, summaries, generations (C4).

The first-version extractor is rule-based and deterministic; it tags every
assertion with ``extractor_version`` and exact evidence, so a later model
extractor can replace it behind the same contract without pretending the old
outputs were model-derived (ADR-0015 §7.1).

Frozen behaviors:

- Staging → ready: a generation is created ``staging``, validated (every
  evidence revision exists, its source is available, binding and access epoch
  unchanged), and only then published, flipping the project pointer in one
  transaction. Failures never publish a half generation.
- Unchanged sources are not re-extracted: when a base generation exists,
  sources whose latest revision is already in its manifest reuse their
  assertions verbatim (same ids); only changed sources extract again.
- A published generation whose manifest no longer matches the sources'
  latest revisions is *invalidated by flag* — content stays auditable, and
  the context layer refuses invalid generations.
"""

import re
from dataclasses import dataclass

from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.knowledge.errors import KnowledgeContractError
from orgpilot.knowledge.models import (
    AssertionKind,
    AssertionValidation,
    CoverageStatus,
    EvidenceRef,
    GenerationStatus,
    KnowledgeAssertion,
    KnowledgeGeneration,
    ProjectSourceBinding,
    SourceAvailability,
    SourceKind,
    SourceObject,
    SourceRevision,
    new_assertion_id,
    new_generation_id,
)
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore

EXTRACTOR_VERSION = "rule-v1"
SNIPPET_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 2000

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TERM_LINE = re.compile(r"^[-*]\s*(.+?)[：:]\s*(.+)$")
_GOAL_SECTION_NAMES = ("项目目标", "项目说明", "概述", "目标")
_TERM_SECTION_NAMES = ("术语", "词汇", "glossary")
_DELIVERY_SECTION_NAMES = ("交付", "验收", "交付条件")


@dataclass
class Segment:
    """One Markdown paragraph range with its nearest heading."""

    source_id: str
    revision_id: str
    title: str
    start_line: int
    end_line: int
    text: str

    @property
    def locator(self) -> str:
        return f"{self.title}#L{self.start_line}-L{self.end_line}"


def segment_markdown(source_id: str, revision_id: str, content: str) -> list[Segment]:
    """Splits Markdown into heading-scoped paragraphs with line numbers."""
    segments: list[Segment] = []
    current_title = ""
    buffer: list[str] = []
    start_line = 1
    seen_content = False

    def flush(end_line: int) -> None:
        text = "\n".join(buffer).strip("\n")
        if text.strip():
            segments.append(
                Segment(
                    source_id=source_id,
                    revision_id=revision_id,
                    title=current_title,
                    start_line=start_line,
                    end_line=end_line,
                    text=text,
                )
            )

    for index, line in enumerate(content.splitlines(), start=1):
        match = _HEADING.match(line)
        if match:
            flush(index - 1)
            current_title = match.group(2).strip()
            buffer = []
            start_line = index + 1
            seen_content = False
            continue
        if not buffer and not seen_content:
            start_line = index
        if line.strip():
            seen_content = True
        buffer.append(line)
    flush(len(content.splitlines()))
    return segments


def _snippet(text: str) -> str:
    cleaned = " ".join(text.split())
    return cleaned[:SNIPPET_MAX_CHARS]


_GOAL_LINE = re.compile(r"项目目标[：:]\s*(.+)")


def _goal_line(text: str) -> str | None:
    """An explicit ``项目目标：`` statement line, if the segment has one."""
    for line in text.splitlines():
        match = _GOAL_LINE.search(line)
        if match:
            return match.group(0)
    return None


def _section_matches(title: str, names: tuple[str, ...]) -> bool:
    lowered = title.lower()
    return any(name in title or name.lower() in lowered for name in names)


def extract_document_assertions(
    obj: SourceObject, revision: SourceRevision
) -> list[KnowledgeAssertion]:
    """Rule extraction of goals / terms / delivery conditions from one doc."""
    if not revision.raw_content:
        return []
    segments = segment_markdown(obj.source_id, revision.revision_id, revision.raw_content)
    assertions: list[KnowledgeAssertion] = []
    goal_taken = False
    for segment in segments:
        evidence = EvidenceRef(
            revision_id=revision.revision_id,
            source_id=obj.source_id,
            kind=SourceKind.DOCUMENT,
            title=obj.title,
            paragraph=segment.locator,
            line_start=segment.start_line,
            line_end=segment.end_line,
            snippet=_snippet(segment.text),
            source_version=revision.source_version,
        )
        goal_line = _goal_line(segment.text)
        is_goal_section = _section_matches(segment.title, _GOAL_SECTION_NAMES)
        first_paragraph = segment.title == "" and not assertions
        if not goal_taken and (goal_line or is_goal_section or first_paragraph):
            value = _snippet(goal_line) if goal_line else _snippet(segment.text)
            assertions.append(
                KnowledgeAssertion(
                    assertion_id=new_assertion_id(),
                    project_id=obj.project_id,
                    subject_ref=obj.source_id,
                    predicate="project_goal",
                    value=value,
                    assertion_kind=AssertionKind.SOURCE_STATEMENT,
                    evidence_refs=(evidence,),
                    extractor_version=EXTRACTOR_VERSION,
                    validation_status=AssertionValidation.VERIFIED,
                )
            )
            goal_taken = True
            continue
        if _section_matches(segment.title, _TERM_SECTION_NAMES):
            for line in segment.text.splitlines():
                term = _TERM_LINE.match(line.strip())
                if term:
                    assertions.append(
                        KnowledgeAssertion(
                            assertion_id=new_assertion_id(),
                            project_id=obj.project_id,
                            subject_ref=obj.source_id,
                            predicate="term",
                            value=f"{term.group(1)}：{term.group(2)}",
                            assertion_kind=AssertionKind.SOURCE_STATEMENT,
                            evidence_refs=(
                                EvidenceRef(
                                    revision_id=revision.revision_id,
                                    source_id=obj.source_id,
                                    kind=SourceKind.DOCUMENT,
                                    title=obj.title,
                                    paragraph=segment.locator,
                                    line_start=segment.start_line,
                                    line_end=segment.end_line,
                                    snippet=_snippet(line),
                                    source_version=revision.source_version,
                                ),
                            ),
                            extractor_version=EXTRACTOR_VERSION,
                            validation_status=AssertionValidation.VERIFIED,
                        )
                    )
        elif _section_matches(segment.title, _DELIVERY_SECTION_NAMES):
            for line in segment.text.splitlines():
                body = line.strip().lstrip("-* ").strip()
                if body:
                    assertions.append(
                        KnowledgeAssertion(
                            assertion_id=new_assertion_id(),
                            project_id=obj.project_id,
                            predicate="delivery_condition",
                            subject_ref=obj.source_id,
                            value=_snippet(body),
                            assertion_kind=AssertionKind.SOURCE_STATEMENT,
                            evidence_refs=(
                                EvidenceRef(
                                    revision_id=revision.revision_id,
                                    source_id=obj.source_id,
                                    kind=SourceKind.DOCUMENT,
                                    title=obj.title,
                                    paragraph=segment.locator,
                                    line_start=segment.start_line,
                                    line_end=segment.end_line,
                                    snippet=_snippet(line),
                                    source_version=revision.source_version,
                                ),
                            ),
                            extractor_version=EXTRACTOR_VERSION,
                            validation_status=AssertionValidation.VERIFIED,
                        )
                    )
    return assertions


def extract_platform_assertions(
    obj: SourceObject, revision: SourceRevision
) -> list[KnowledgeAssertion]:
    """Raw platform fields become first-class assertions with exact evidence."""
    assertions: list[KnowledgeAssertion] = []
    for predicate, key in (
        ("state", "state"),
        ("milestone", "milestone"),
    ):
        value = revision.metadata.get(key)
        if value is not None and value != "":
            assertions.append(
                KnowledgeAssertion(
                    assertion_id=new_assertion_id(),
                    project_id=obj.project_id,
                    subject_ref=obj.source_id,
                    predicate=f"{obj.kind.value}_{predicate}",
                    value=str(value),
                    assertion_kind=AssertionKind.PLATFORM_FIELD,
                    evidence_refs=(
                        EvidenceRef(
                            revision_id=revision.revision_id,
                            source_id=obj.source_id,
                            kind=obj.kind,
                            title=obj.title,
                            snippet=_snippet(f"{key}: {value}"),
                            source_version=revision.source_version,
                        ),
                    ),
                    extractor_version=EXTRACTOR_VERSION,
                    validation_status=AssertionValidation.VERIFIED,
                )
            )
    assignees = revision.metadata.get("assignees")
    if assignees:
        assertions.append(
            KnowledgeAssertion(
                assertion_id=new_assertion_id(),
                project_id=obj.project_id,
                subject_ref=obj.source_id,
                predicate=f"{obj.kind.value}_assignees",
                value=", ".join(assignees),
                assertion_kind=AssertionKind.PLATFORM_FIELD,
                evidence_refs=(
                    EvidenceRef(
                        revision_id=revision.revision_id,
                        source_id=obj.source_id,
                        kind=obj.kind,
                        title=obj.title,
                        snippet=_snippet(f"assignees: {', '.join(assignees)}"),
                        source_version=revision.source_version,
                    ),
                ),
                extractor_version=EXTRACTOR_VERSION,
                validation_status=AssertionValidation.VERIFIED,
            )
        )
    return assertions


class DerivationPipeline:
    """Builds and publishes knowledge generations for one binding version."""

    def __init__(
        self,
        sources: SqlProjectSourceStore,
        knowledge: SqlKnowledgeStore,
        clock: Clock | None = None,
    ) -> None:
        self.sources = sources
        self.knowledge = knowledge
        self.clock: Clock = clock or SystemClock()

    async def build_and_publish(
        self,
        binding: ProjectSourceBinding,
        reason: str = "rebuild",
        base_generation: KnowledgeGeneration | None = None,
        coverage_override: dict[str, str] | None = None,
    ) -> KnowledgeGeneration:
        """Derives a staging generation, validates it, and publishes it.

        Any inconsistency discovered at publish time (binding/epoch changed,
        source revoked or updated mid-build) fails the build instead of
        publishing stale or unauthorized content (ADR-0015 §5.3).
        """
        generation = KnowledgeGeneration(
            generation_id=new_generation_id(),
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            access_epoch=binding.access_epoch,
            status=GenerationStatus.STAGING,
            created_at=self.clock.now(),
        )
        await self.knowledge.create_generation(generation)
        try:
            manifest, assertion_refs, relation_refs, summary, coverage = await self._derive(
                binding, base_generation
            )
        except Exception as exc:
            await self.knowledge.fail_generation(generation.generation_id, f"derive failed: {exc}")
            raise
        # Publish-time re-validation: the world may have moved while deriving.
        current_binding = await self.sources.get_active_binding(binding.project_id)
        if current_binding is None or not current_binding.enabled:
            await self.knowledge.fail_generation(
                generation.generation_id, "binding disabled or missing at publish time"
            )
            raise DerivationAborted("binding no longer active")
        if (
            current_binding.binding_version != binding.binding_version
            or current_binding.access_epoch != binding.access_epoch
        ):
            await self.knowledge.fail_generation(
                generation.generation_id, "binding version or access epoch changed during build"
            )
            raise DerivationAborted("binding changed during build")
        stale = await self._stale_manifest_sources(binding, manifest)
        if stale:
            await self.knowledge.fail_generation(
                generation.generation_id,
                f"sources changed during build: {', '.join(sorted(stale))}",
            )
            raise DerivationAborted("sources updated during build")
        revoked = await self._revoked_manifest_sources(binding, manifest)
        if revoked:
            await self.knowledge.fail_generation(
                generation.generation_id,
                f"sources revoked during build: {', '.join(sorted(revoked))}",
            )
            raise DerivationAborted("sources revoked during build")

        ready = KnowledgeGeneration(
            generation_id=generation.generation_id,
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            access_epoch=binding.access_epoch,
            status=GenerationStatus.STAGING,
            revision_manifest=manifest,
            assertion_refs=assertion_refs,
            relation_refs=relation_refs,
            summary=summary,
            derivation_version=EXTRACTOR_VERSION,
            coverage=coverage_override if coverage_override is not None else coverage,
            created_at=generation.created_at,
        )
        await self._replace_generation_row(ready)
        try:
            await self.knowledge.publish_generation(generation.generation_id, validate_sources=True)
        except KnowledgeContractError as exc:
            await self.knowledge.fail_generation(generation.generation_id, str(exc))
            raise DerivationAborted("material changed before publication") from exc
        published = await self.knowledge.get_generation(generation.generation_id)
        assert published is not None  # just published above
        return published

    async def _replace_generation_row(self, generation: KnowledgeGeneration) -> None:
        """Rewrites the staging row with its derived content (still staging)."""
        from sqlalchemy import select

        from orgpilot.storage.database import Database  # noqa: F401 - typing aid
        from orgpilot.storage.models import KnowledgeGenerationRecord

        async with self.knowledge.db.session() as session:
            record = (
                await session.execute(
                    select(KnowledgeGenerationRecord).where(
                        KnowledgeGenerationRecord.generation_id == generation.generation_id
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                return
            import json

            record.revision_manifest_json = json.dumps(list(generation.revision_manifest))
            record.assertion_refs_json = json.dumps(list(generation.assertion_refs))
            record.relation_refs_json = json.dumps(list(generation.relation_refs))
            record.summary = generation.summary
            record.derivation_version = generation.derivation_version
            record.coverage_json = json.dumps(generation.coverage)

    async def _derive(
        self,
        binding: ProjectSourceBinding,
        base_generation: KnowledgeGeneration | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str, dict[str, str]]:
        """Extracts assertions for changed sources, reuses unchanged ones."""
        objects = await self.sources.list_source_objects(
            binding.project_id, binding.binding_version
        )
        available = [obj for obj in objects if obj.availability is SourceAvailability.AVAILABLE]
        base_manifest = set(base_generation.revision_manifest) if base_generation else set()
        # Reuse only what the base generation actually published. Rows left by
        # an aborted build pin the same revisions and would otherwise be reused
        # as duplicates.
        base_refs = set(base_generation.assertion_refs) if base_generation else set()
        base_assertions = {
            a.assertion_id: a
            for a in await self.knowledge.list_assertions(binding.project_id)
            if a.assertion_id in base_refs
        }
        manifest: list[str] = []
        assertions: list[KnowledgeAssertion] = []
        for obj in available:
            latest = await self.sources.latest_revision(obj.source_id)
            if latest is None:
                continue
            manifest.append(latest.revision_id)
            if latest.revision_id in base_manifest and base_generation is not None:
                # Unchanged source: reuse assertions whose evidence pins this
                # revision, keeping their original ids (no re-extraction).
                reused = [
                    a
                    for a in base_assertions.values()
                    if a.subject_ref == obj.source_id
                    and any(ref.revision_id == latest.revision_id for ref in a.evidence_refs)
                ]
                assertions.extend(reused)
                continue
            if obj.kind is SourceKind.DOCUMENT:
                assertions.extend(extract_document_assertions(obj, latest))
            elif obj.kind in (SourceKind.ISSUE, SourceKind.PULL_REQUEST):
                assertions.extend(extract_platform_assertions(obj, latest))
        # Fresh assertion rows for this generation (idempotent per generation).
        stored_ids = {
            a.assertion_id for a in await self.knowledge.list_assertions(binding.project_id)
        }
        new_rows = [a for a in assertions if a.assertion_id not in stored_ids]
        from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401

        async with self.knowledge.db.session() as session:
            for assertion in new_rows:
                await self.knowledge.add_assertion(assertion, session)
        relations = await self.knowledge.list_relations(binding.project_id)
        relation_refs = tuple(r.relation_id for r in relations)
        summary = self._render_summary(binding, assertions, available)
        coverage: dict[str, str] = dict(base_generation.coverage) if base_generation else {}
        for kind in SourceKind:
            count = sum(1 for obj in available if obj.kind is kind)
            if count:
                coverage.setdefault(kind.value, CoverageStatus.COMPLETE.value)
            if any(
                obj.kind is kind and obj.availability is not SourceAvailability.AVAILABLE
                for obj in objects
            ):
                coverage[kind.value] = CoverageStatus.PARTIAL.value
        return (
            tuple(manifest),
            tuple(a.assertion_id for a in assertions),
            relation_refs,
            summary,
            coverage,
        )

    async def _stale_manifest_sources(
        self, binding: ProjectSourceBinding, manifest: tuple[str, ...]
    ) -> list[str]:
        """Sources whose latest revision moved away from the manifest."""
        stale: list[str] = []
        manifest_set = set(manifest)
        objects = await self.sources.list_source_objects(
            binding.project_id, binding.binding_version
        )
        for obj in objects:
            if obj.availability is not SourceAvailability.AVAILABLE:
                continue
            latest = await self.sources.latest_revision(obj.source_id)
            if latest is not None and latest.revision_id not in manifest_set:
                stale.append(obj.external_id)
        return stale

    async def _revoked_manifest_sources(
        self, binding: ProjectSourceBinding, manifest: tuple[str, ...]
    ) -> list[str]:
        """Manifest sources that lost availability while the build ran.

        The manifest holds revision ids, so the source set is resolved through
        the revision rows before comparing against stored availability.
        """
        revoked: list[str] = []
        manifest_sources: set[str] = set()
        for revision_id in manifest:
            revision = await self.sources.get_revision(revision_id)
            if revision is not None:
                manifest_sources.add(revision.source_id)
        objects = await self.sources.list_source_objects(
            binding.project_id, binding.binding_version
        )
        for obj in objects:
            in_manifest = obj.source_id in manifest_sources
            if in_manifest and obj.availability is not SourceAvailability.AVAILABLE:
                revoked.append(obj.external_id)
        return revoked

    def _render_summary(
        self,
        binding: ProjectSourceBinding,
        assertions: list[KnowledgeAssertion],
        available: list[SourceObject],
    ) -> str:
        goals = [a for a in assertions if a.predicate == "project_goal"]
        delivery = [a for a in assertions if a.predicate == "delivery_condition"]
        terms = [a for a in assertions if a.predicate == "term"]
        docs = sum(1 for obj in available if obj.kind is SourceKind.DOCUMENT)
        issues = sum(1 for obj in available if obj.kind is SourceKind.ISSUE)
        prs = sum(1 for obj in available if obj.kind is SourceKind.PULL_REQUEST)
        lines = [
            f"项目目标：{goals[0].value if goals else '资料中未登记'}",
            f"交付约定：{len(delivery)} 条（来源见对应交付文档）",
            f"术语：{len(terms)} 条",
            f"接入范围：文档 {docs} 篇、Issue {issues} 个、PR {prs} 个"
            f"（binding v{binding.binding_version}，epoch {binding.access_epoch}）",
        ]
        summary = "\n".join(lines)
        return summary[:SUMMARY_MAX_CHARS]

    async def invalidate_stale_generations(self, project_id: str) -> list[str]:
        """Flags published generations whose manifest lags the sources (§5.3)."""
        invalidated: list[str] = []
        current_id = await self.knowledge.current_generation_id(project_id)
        if current_id is None:
            return invalidated
        generation = await self.knowledge.get_generation(current_id)
        if generation is None or not generation.valid:
            return invalidated
        binding = await self.sources.get_active_binding(project_id)
        if binding is None:
            return invalidated
        stale = await self._stale_manifest_sources(binding, generation.revision_manifest)
        if stale:
            await self.knowledge.invalidate_generation(
                generation.generation_id,
                f"sources updated after publish: {', '.join(sorted(stale))}",
            )
            invalidated.append(generation.generation_id)
        return invalidated


class DerivationAborted(Exception):
    """The build was aborted before publishing (binding/epoch/source changed)."""

"""Current authorization checks shared by the read controller and its tools."""

from orgpilot.knowledge.models import (
    KnowledgeGeneration,
    ProjectSourceBinding,
    SourceAvailability,
    StopReason,
)
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore


async def check_binding(
    sources: SqlProjectSourceStore | None,
    binding: ProjectSourceBinding,
    requester_id: str,
) -> StopReason | None:
    current = await sources.get_active_binding(binding.project_id) if sources else binding
    if current is None or not current.member_can_read(requester_id):
        return StopReason.ACCESS_DENIED
    if (
        current.binding_version != binding.binding_version
        or current.access_epoch != binding.access_epoch
    ):
        return StopReason.CONTEXT_CHANGED
    return None


async def check_generation(
    sources: SqlProjectSourceStore,
    knowledge: SqlKnowledgeStore,
    generation: KnowledgeGeneration,
) -> StopReason | None:
    # Two bulk reads instead of two queries per revision: this check runs
    # before and after every tool call, inside the per-tool time budget.
    revisions = await sources.get_revisions(generation.revision_manifest)
    objects = await sources.get_source_objects(
        revision.source_id for revision in revisions.values()
    )
    for revision_id in generation.revision_manifest:
        revision = revisions.get(revision_id)
        obj = objects.get(revision.source_id) if revision else None
        if obj is None or obj.availability is not SourceAvailability.AVAILABLE:
            return StopReason.ACCESS_DENIED
        if (
            obj.project_id != generation.project_id
            or obj.binding_version != generation.binding_version
            or obj.latest_revision_id != revision_id
        ):
            return StopReason.CONTEXT_CHANGED
    if await knowledge.current_generation_id(generation.project_id) != generation.generation_id:
        return StopReason.CONTEXT_CHANGED
    current = await knowledge.get_generation(generation.generation_id)
    if current is None or not current.valid:
        return StopReason.CONTEXT_CHANGED
    return None

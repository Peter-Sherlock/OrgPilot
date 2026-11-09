"""ProjectContextBuilder: combines material and read-only OrgState (C4).

The packet is a single-request read model (ADR-0015 §4.1): a fixed generation
plus a frozen view of formal task state, with evidence pinned to revisions,
conflicts and gaps visible, and a hard size ceiling. Carrying a packet grants
no write permission on tasks, cases, or approvals (§6), and the builder never
calls the gateway, coordination, or sync paths.
"""

from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.domain.models import OrgState
from orgpilot.knowledge.models import (
    PROJECT_BRIEF_MAX_CHARS,
    FreshnessInfo,
    KnowledgeGeneration,
    ProjectContextPacket,
    ProjectSourceBinding,
    SourceAvailability,
    SourceObject,
    binding_uid,
    new_packet_id,
)
from orgpilot.knowledge.retrieval import MaterialIndex

INITIAL_EVIDENCE_MAX = 4


def last_event(task) -> str:
    """The task's latest event ref, or a placeholder when history is empty."""
    if task.source_event_ids:
        return task.source_event_ids[-1]
    return "unknown"


class ProjectContextBuilder:
    """Assembles the initial context packet for one bounded read session."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or SystemClock()

    def build_packet(
        self,
        *,
        read_session_id: str,
        requester_id: str,
        binding: ProjectSourceBinding,
        generation: KnowledgeGeneration,
        index: MaterialIndex,
        objects: list[SourceObject],
        question: str,
        org_state: OrgState | None = None,
        max_evidence: int = INITIAL_EVIDENCE_MAX,
        max_chars: int = 8000,
    ) -> ProjectContextPacket:
        """Builds round-0 packet; sharing checks happen before this is called.

        The builder refuses to run against an invalid or foreign generation —
        a session must never mix two generations or read after revocation.
        """
        if not generation.valid:
            raise ValueError("generation is invalid; refresh before retrying")
        if binding_uid(binding.project_id, binding.binding_version) != (
            binding_uid(generation.project_id, generation.binding_version)
        ):
            raise ValueError("generation does not match the session binding")
        if not binding.member_can_read(requester_id):
            raise PermissionError("requester is not in the project sharing list")

        hits = index.search(question) if question.strip() else []
        from orgpilot.knowledge.models import EvidenceRef

        evidence: list[EvidenceRef] = []
        excluded: list[str] = []
        chars_left = max_chars
        for entry in hits[:max_evidence]:
            if chars_left <= 0:
                break
            revision = getattr(entry, "revision_id", None)
            if revision is None:
                continue
            snippet = entry.text[: min(PROJECT_BRIEF_MAX_CHARS, chars_left)]
            chars_left -= len(snippet)
            evidence.append(
                EvidenceRef(
                    revision_id=revision,
                    source_id=entry.source_id,
                    kind=entry.kind,
                    title=entry.title,
                    paragraph=entry.paragraph,
                    line_start=entry.line_start,
                    line_end=entry.line_end,
                    snippet=snippet,
                )
            )
        if len(hits) > len(evidence):
            excluded = [
                f"{entry.source_id}:{entry.paragraph or entry.external_id}"
                for entry in hits[len(evidence) :]
            ]

        # Only a count, and only for temporary platform failures: names of
        # revoked or deleted material must not reach the asker (§7.4), and
        # deleted objects are not missing information, they are gone.
        unavailable = sum(
            1 for obj in objects if obj.availability is SourceAvailability.UNAVAILABLE
        )
        missing_info = (
            [f"{unavailable} 份资料在平台上暂时读不到（unavailable），未参与本次回答"]
            if unavailable
            else []
        )

        org_state_refs: tuple[str, ...] = ()
        conflicts: list[str] = []
        if org_state is not None:
            refs = [f"task:{task.task_id}@{last_event(task)}" for task in org_state.tasks.values()]
            org_state_refs = tuple(refs[:20])
            # Explicit issue→task mappings would be checked here; first version
            # surfaces the boundary instead of inventing agreement.
            if binding.issue_selector.milestone is not None and org_state.tasks:
                conflicts.append(
                    "正式任务状态与 GitHub 工作记录分属两套来源；本回答分别标注，不自动合并。"
                )

        freshness = FreshnessInfo(
            generation_id=generation.generation_id,
            binding_version=generation.binding_version,
            access_epoch=generation.access_epoch,
            last_verified_at=self.clock.now(),
            coverage=dict(generation.coverage),
            stale_sources=(),
            sync_note=None,
        )
        return ProjectContextPacket(
            packet_id=new_packet_id(),
            read_session_id=read_session_id,
            round=0,
            generation_id=generation.generation_id,
            requester_id=requester_id,
            binding_version=binding.binding_version,
            access_epoch=binding.access_epoch,
            org_state_refs=org_state_refs,
            brief=generation.summary or "",
            relevant_evidence=tuple(evidence),
            conflicts=tuple(conflicts),
            missing_info=tuple(missing_info),
            freshness=freshness,
            excluded_evidence=tuple(excluded),
            created_at=self.clock.now(),
        )

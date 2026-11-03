"""First-import of GitHub material into source objects and revisions (ADR-0015 C2).

``SourceImporter`` walks the binding's declared scope once — documents at a
pinned commit SHA, issues/PRs/comments/reviews by stable platform ids — and
writes source objects plus immutable revisions through the C1 stores. It never
appends OrgEvents, touches cases or approvals, or runs an agent; that
guarantee is enforced by construction (imports touch knowledge stores only)
and pinned by a dedicated regression test.

Coverage honesty is the core contract (§5.2): pagination truncation, size
caps, object caps, and read failures each make the per-kind coverage
``partial`` or ``failed`` — a partially-read scope is never reported complete.
403/404 record an availability failure, never a deletion.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.github.client import GitHubReadClient, GitHubReadError, is_access_failure
from orgpilot.github.models import CommentPayload, IssuePayload, PullRequestPayload
from orgpilot.knowledge.errors import BindingConflict, KnowledgeContractError
from orgpilot.knowledge.models import (
    ConfirmationStatus,
    CoverageStatus,
    EvidenceRef,
    KnowledgeRelation,
    ProjectSourceBinding,
    RelationType,
    SourceAvailability,
    SourceKind,
    SourceObject,
    SourceObjectIdentity,
    SourceRevision,
    SyncRunReason,
    SyncRunState,
    canonical_payload_hash,
    content_hash,
    new_relation_id,
    new_revision_id,
    normalize_repo_path,
)
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore, SqlSyncRunStore

# ADR-0015 §2.2 suggested initial caps (binding config may tighten later).
DEFAULT_MAX_DOCUMENTS = 50
DEFAULT_MAX_WORK_ITEMS = 200
DEFAULT_MAX_SUB_ITEMS_PER_OBJECT = 100
DEFAULT_MAX_DOCUMENT_BYTES = 256 * 1024

# Platform-explicit association: closing keywords in a PR body reference an
# issue; a bare "#N" is only a mention and never upgraded (§2.1).
_CLOSING_KEYWORDS = re.compile(
    r"\b(?:[Cc]loses|[Ff]ixes|[Rr]esolves|[Cc]losed|[Ff]ixed|[Rr]esolved)\s+#(\d+)"
)
_MENTION = re.compile(r"#(\d+)")


@dataclass(frozen=True)
class ImportLimits:
    """Binding-level scope caps; exceeding one is visible, never silent (§2.2)."""

    max_documents: int = DEFAULT_MAX_DOCUMENTS
    max_work_items: int = DEFAULT_MAX_WORK_ITEMS
    max_sub_items_per_object: int = DEFAULT_MAX_SUB_ITEMS_PER_OBJECT
    max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES


@dataclass
class ImportFailure:
    """One object that could not be read completely, with the reason."""

    kind: str
    external_id: str
    reason: str
    status_code: int | None = None


@dataclass
class ImportTruncation:
    """One cap that hid material from this import run."""

    kind: str
    external_id: str
    cap: str
    observed: int


@dataclass
class ImportConflict:
    """A scope inconsistency worth review, e.g. an explicit PR id that is an issue."""

    kind: str
    external_id: str
    detail: str


@dataclass
class ImportReport:
    """Reviewable scope, version, failure, and conflict manifest (C2 deliverable)."""

    project_id: str
    binding_version: int
    access_epoch: int
    repository_id: str
    repository_full_name: str
    pinned_commit_sha: str
    run_id: str
    counts: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=dict)
    failures: list[ImportFailure] = field(default_factory=list)
    truncations: list[ImportTruncation] = field(default_factory=list)
    conflicts: list[ImportConflict] = field(default_factory=list)
    relations_recorded: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def coverage_summary(self) -> str:
        parts = [f"{kind}={status}" for kind, status in sorted(self.coverage.items())]
        return ", ".join(parts) if parts else "no material in scope"

    @property
    def fully_complete(self) -> bool:
        return bool(self.coverage) and all(
            status == CoverageStatus.COMPLETE.value for status in self.coverage.values()
        )


class SourceImporter:
    """Imports the binding scope into source objects and immutable revisions."""

    def __init__(
        self,
        client: GitHubReadClient,
        sources: SqlProjectSourceStore,
        knowledge: SqlKnowledgeStore,
        runs: SqlSyncRunStore,
        clock: Clock | None = None,
        limits: ImportLimits | None = None,
    ) -> None:
        self.client = client
        self.sources = sources
        self.knowledge = knowledge
        self.runs = runs
        self.clock: Clock = clock or SystemClock()
        self.limits = limits or ImportLimits()
        self._binding: ProjectSourceBinding | None = None

    async def import_binding_scope(
        self, binding: ProjectSourceBinding, run_id: str | None = None
    ) -> ImportReport:
        """Runs one full first-import over the binding scope."""
        self._binding = binding
        if not binding.enabled:
            raise BindingConflict(
                f"binding {binding.project_id}@v{binding.binding_version} is disabled"
            )
        started_at = self.clock.now()
        run_id = run_id or f"run-{uuid.uuid4().hex[:16]}"
        repo = self.client.get_repository()
        if repo.repository_id != binding.repository_id:
            raise KnowledgeContractError(
                f"binding repository {binding.repository_id} != client repository "
                f"{repo.repository_id}; repository identity changes never silently rebind"
            )
        await self.runs.start_run(
            run_id,
            binding.project_id,
            binding_version=binding.binding_version,
            access_epoch=binding.access_epoch,
            reason=SyncRunReason.INITIAL_IMPORT,
            candidate_manifest={"scope": self._scope_manifest(binding)},
        )
        report = ImportReport(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            access_epoch=binding.access_epoch,
            repository_id=repo.repository_id,
            repository_full_name=repo.full_name,
            pinned_commit_sha=self.client.get_branch_head(binding.branch).sha,
            run_id=run_id,
            started_at=started_at,
        )
        await self._import_documents(binding, report)
        issue_scope, pr_scope = await self._collect_work_items(binding, report)
        await self._import_issues(binding, report, issue_scope)
        await self._import_pull_requests(binding, report, pr_scope)
        await self._import_sub_items(binding, report, issue_scope, pr_scope)
        self._finalize_coverage(report)
        await self.runs.save_checkpoint(
            run_id,
            per_source_cursor={"pinned_commit": report.pinned_commit_sha},
            coverage=dict(report.coverage),
            last_success_at=self.clock.now(),
        )
        run_state = _run_state_for(report)
        await self.runs.set_run_state(
            run_id,
            run_state,
            error=(
                None
                if run_state is SyncRunState.COMPLETED and report.fully_complete
                else f"coverage: {report.coverage_summary()}"
            ),
        )
        report.finished_at = self.clock.now()
        return report

    # -- documents ----------------------------------------------------------------

    async def _import_documents(
        self, binding: ProjectSourceBinding, report: ImportReport
    ) -> None:
        if not binding.path_allowlist:
            return
        try:
            tree = self.client.get_tree(report.pinned_commit_sha)
        except GitHubReadError as exc:
            report.failures.append(
                ImportFailure(
                    SourceKind.DOCUMENT.value,
                    "*",
                    f"tree enumeration failed: {exc}",
                    exc.status_code,
                )
            )
            report.coverage[SourceKind.DOCUMENT.value] = CoverageStatus.FAILED.value
            return
        allowed = [entry for entry in tree if binding.allows_path(entry.path)]
        allowed.sort(key=lambda entry: entry.path)
        if len(allowed) > self.limits.max_documents:
            for entry in allowed[self.limits.max_documents :]:
                report.truncations.append(
                    ImportTruncation(
                        SourceKind.DOCUMENT.value, entry.path, "max_documents", len(allowed)
                    )
                )
            allowed = allowed[: self.limits.max_documents]
        imported = 0
        for entry in allowed:
            try:
                content = self.client.get_blob(entry.blob_sha)
            except GitHubReadError as exc:
                report.failures.append(
                    ImportFailure(
                        SourceKind.DOCUMENT.value,
                        entry.path,
                        f"blob read failed: {exc}",
                        exc.status_code,
                    )
                )
                await self._mark_object_unavailable(binding, SourceKind.DOCUMENT, entry.path, exc)
                continue
            if len(content.encode("utf-8")) > self.limits.max_document_bytes:
                report.truncations.append(
                    ImportTruncation(
                        SourceKind.DOCUMENT.value, entry.path, "max_document_bytes", len(content)
                    )
                )
                continue
            await self._record_object_and_revision(
                binding=binding,
                kind=SourceKind.DOCUMENT,
                external_id=normalize_repo_path(entry.path),
                title=entry.path.rsplit("/", 1)[-1],
                source_version=report.pinned_commit_sha,
                content=content,
                metadata={"blob_sha": entry.blob_sha, "size": entry.size},
                source_updated_at=None,
                canonical_url=None,
            )
            imported += 1
        report.counts[SourceKind.DOCUMENT.value] = imported

    # -- work items ---------------------------------------------------------------

    def _scope_manifest(self, binding: ProjectSourceBinding) -> dict[str, Any]:
        return {
            "branch": binding.branch,
            "path_allowlist": list(binding.path_allowlist),
            "issue_milestone": binding.issue_selector.milestone,
            "issue_ids": list(binding.issue_selector.issue_ids),
            "explicit_pr_ids": list(binding.explicit_pr_ids),
        }

    async def _collect_work_items(
        self, binding: ProjectSourceBinding, report: ImportReport
    ) -> tuple[dict[int, IssuePayload], dict[int, int]]:
        """Enumerates issues/PRs across pages, applying the selector and caps.

        Returns (issues in scope, PR numbers in scope → discovered where).
        PR entries returned by the issues API are routed by object type, never
        double-counted as issues (§5.2). Objects beyond ``max_work_items`` are
        not imported; the cap hit is recorded instead of being passed off as a
        complete read (§2.2).
        """
        issue_scope: dict[int, IssuePayload] = {}
        pr_scope: dict[int, int] = dict.fromkeys(binding.explicit_pr_ids, 0)
        page = 1
        seen_total = 0
        matched = len(pr_scope)
        beyond_cap = 0
        while True:
            batch = self.client.list_issues(state="all", page=page, per_page=50)
            if not batch:
                break
            for item in batch:
                seen_total += 1
                selected = binding.issue_selector.covers(
                    item.number, item.milestone
                ) or item.number in binding.explicit_pr_ids
                if not selected:
                    continue
                if matched >= self.limits.max_work_items:
                    beyond_cap += 1
                    continue
                matched += 1
                if item.is_pull_request:
                    pr_scope.setdefault(item.number, 1)
                else:
                    issue_scope[item.number] = item
            if len(batch) < 50:
                break
            page += 1
        if beyond_cap:
            report.truncations.append(
                ImportTruncation(
                    "work_items",
                    binding.repository_id,
                    "max_work_items",
                    seen_total,
                )
            )
        return issue_scope, pr_scope

    async def _import_issues(
        self,
        binding: ProjectSourceBinding,
        report: ImportReport,
        issue_scope: dict[int, IssuePayload],
    ) -> None:
        imported = 0
        for number in sorted(issue_scope):
            issue = issue_scope[number]
            payload = issue.model_dump(mode="json")
            await self._record_object_and_revision(
                binding=binding,
                kind=SourceKind.ISSUE,
                external_id=str(number),
                title=issue.title,
                source_version=issue.updated_at.isoformat(),
                content=canonical_payload_hash(payload),
                metadata={
                    "state": issue.state,
                    "assignees": list(issue.assignees),
                    "labels": list(issue.labels),
                    "milestone": issue.milestone,
                    "body": issue.body or "",
                    "html_url": issue.html_url,
                },
                source_updated_at=issue.updated_at,
                canonical_url=issue.html_url,
                payload_for_hash=payload,
            )
            imported += 1
        report.counts[SourceKind.ISSUE.value] = imported

    async def _import_pull_requests(
        self,
        binding: ProjectSourceBinding,
        report: ImportReport,
        pr_scope: dict[int, int],
    ) -> None:
        imported = 0
        for number in sorted(pr_scope):
            try:
                pr = self.client.get_pull_request(number)
            except GitHubReadError as exc:
                if number in binding.explicit_pr_ids:
                    # An admin-registered PR id that cannot be read as a PR is
                    # a scope conflict for review, not a silent skip.
                    report.conflicts.append(
                        ImportConflict(
                            SourceKind.PULL_REQUEST.value,
                            str(number),
                            f"explicit id #{number} not readable as PR: {exc}",
                        )
                    )
                report.failures.append(
                    ImportFailure(
                        SourceKind.PULL_REQUEST.value,
                        str(number),
                        f"read failed: {exc}",
                        exc.status_code,
                    )
                )
                continue
            payload = pr.model_dump(mode="json")
            await self._record_object_and_revision(
                binding=binding,
                kind=SourceKind.PULL_REQUEST,
                external_id=str(number),
                title=pr.title,
                source_version=pr.updated_at.isoformat(),
                content=canonical_payload_hash(payload),
                metadata={
                    "state": pr.state,
                    "merged": pr.merged,
                    "merge_commit_sha": pr.merge_commit_sha,
                    "head_sha": pr.head_sha,
                    "base_sha": pr.base_sha,
                    "body": pr.body or "",
                    "html_url": pr.html_url,
                },
                source_updated_at=pr.updated_at,
                canonical_url=pr.html_url,
                payload_for_hash=payload,
            )
            await self._record_pr_issue_links(binding, pr)
            imported += 1
        report.counts[SourceKind.PULL_REQUEST.value] = imported

    async def _record_pr_issue_links(
        self, binding: ProjectSourceBinding, pr: PullRequestPayload
    ) -> None:
        """Records platform-explicit links and plain mentions from the PR body.

        Closing keywords establish an ``implements`` platform link; bare ``#N``
        mentions stay ``mentions`` and are never upgraded (§2.1, §3).
        """
        body = pr.body or ""
        pr_ref = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=SourceKind.PULL_REQUEST,
            external_id=str(pr.number),
        ).source_id()
        closing = set(_CLOSING_KEYWORDS.findall(body))
        all_refs = set(_MENTION.findall(body))
        evidence_revision = await self.sources.latest_revision(pr_ref)
        if evidence_revision is None:
            return
        for number in sorted(all_refs):
            issue_identity = SourceObjectIdentity(
                project_id=binding.project_id,
                binding_version=binding.binding_version,
                kind=SourceKind.ISSUE,
                external_id=number,
            )
            issue_obj = await self.sources.get_source_object(issue_identity.source_id())
            if issue_obj is None:
                continue  # out-of-scope targets are mentions of nothing trackable
            relation_type = (
                RelationType.IMPLEMENTS if number in closing else RelationType.MENTIONS
            )
            confirmation = (
                ConfirmationStatus.CONFIRMED
                if relation_type is RelationType.IMPLEMENTS
                else ConfirmationStatus.UNCONFIRMED
            )
            await self.knowledge.add_relation(
                KnowledgeRelation(
                    relation_id=new_relation_id(),
                    project_id=binding.project_id,
                    from_ref=pr_ref,
                    relation_type=relation_type,
                    to_ref=issue_obj.source_id,
                    evidence_refs=(
                        EvidenceRef(
                            revision_id=evidence_revision.revision_id,
                            source_id=pr_ref,
                            kind=SourceKind.PULL_REQUEST,
                            title=pr.title,
                            snippet=f"PR #{pr.number} body references #{number}",
                        ),
                    ),
                    confirmation_status=confirmation,
                )
            )

    async def _import_sub_items(
        self,
        binding: ProjectSourceBinding,
        report: ImportReport,
        issue_scope: dict[int, IssuePayload],
        pr_scope: dict[int, int],
    ) -> None:
        """Imports ordinary comments and PR reviews as child objects (§2.1)."""
        comments_imported = 0
        reviews_imported = 0
        for number in sorted(issue_scope):
            comments_imported += await self._import_comments(
                binding, report, SourceKind.ISSUE, number
            )
        for number in sorted(pr_scope):
            comments_imported += await self._import_comments(
                binding, report, SourceKind.PULL_REQUEST, number
            )
            reviews_imported += await self._import_reviews(binding, report, number)
        report.counts[SourceKind.COMMENT.value] = comments_imported
        report.counts[SourceKind.REVIEW.value] = reviews_imported

    async def _import_comments(
        self,
        binding: ProjectSourceBinding,
        report: ImportReport,
        parent_kind: SourceKind,
        number: int,
    ) -> int:
        parent_ref = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=parent_kind,
            external_id=str(number),
        ).source_id()
        imported = 0
        page = 1
        while True:
            try:
                batch = self.client.list_issue_comments(number, page=page, per_page=50)
            except GitHubReadError as exc:
                report.failures.append(
                    ImportFailure(
                        SourceKind.COMMENT.value,
                        f"{parent_kind.value}#{number}",
                        f"comments page {page} failed: {exc}",
                        exc.status_code,
                    )
                )
                await self._mark_object_unavailable(
                    binding, SourceKind.COMMENT, f"{parent_kind.value}#{number}", exc
                )
                return imported
            for comment in batch:
                if imported >= self.limits.max_sub_items_per_object:
                    report.truncations.append(
                        ImportTruncation(
                            SourceKind.COMMENT.value,
                            f"{parent_kind.value}#{number}",
                            "max_sub_items_per_object",
                            imported + len(batch),
                        )
                    )
                    return imported
                await self._record_comment(binding, parent_ref, parent_kind, number, comment)
                imported += 1
            if len(batch) < 50:
                return imported
            page += 1

    async def _import_reviews(
        self, binding: ProjectSourceBinding, report: ImportReport, pr_number: int
    ) -> int:
        pr_ref = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=SourceKind.PULL_REQUEST,
            external_id=str(pr_number),
        ).source_id()
        imported = 0
        page = 1
        while True:
            try:
                batch = self.client.list_pull_request_reviews(pr_number, page=page, per_page=50)
            except GitHubReadError as exc:
                report.failures.append(
                    ImportFailure(
                        SourceKind.REVIEW.value,
                        f"pull_request#{pr_number}",
                        f"reviews page {page} failed: {exc}",
                        exc.status_code,
                    )
                )
                return imported
            for review in batch:
                if imported >= self.limits.max_sub_items_per_object:
                    report.truncations.append(
                        ImportTruncation(
                            SourceKind.REVIEW.value,
                            f"pull_request#{pr_number}",
                            "max_sub_items_per_object",
                            imported + len(batch),
                        )
                    )
                    return imported
                identity = SourceObjectIdentity(
                    project_id=binding.project_id,
                    binding_version=binding.binding_version,
                    kind=SourceKind.REVIEW,
                    external_id=review.review_id,
                    parent_source_id=pr_ref,
                )
                payload = review.model_dump(mode="json")
                await self._record_object_and_revision(
                    binding=binding,
                    kind=SourceKind.REVIEW,
                    external_id=review.review_id,
                    title=f"review by {review.author or 'unknown'}: {review.state}",
                    parent_source_id=pr_ref,
                    source_version=review.submitted_at.isoformat(),
                    content=canonical_payload_hash(payload),
                    metadata={
                        "state": review.state,
                        "author": review.author,
                        "body": review.body or "",
                        "pr_number": pr_number,
                    },
                    source_updated_at=review.submitted_at,
                    canonical_url=None,
                    payload_for_hash=payload,
                    precomputed_source_id=identity.source_id(),
                )
                imported += 1
            if len(batch) < 50:
                return imported
            page += 1

    async def _record_comment(
        self,
        binding: ProjectSourceBinding,
        parent_ref: str,
        parent_kind: SourceKind,
        parent_number: int,
        comment: CommentPayload,
    ) -> None:
        identity = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=SourceKind.COMMENT,
            external_id=comment.comment_id,
            parent_source_id=parent_ref,
        )
        payload = comment.model_dump(mode="json")
        await self._record_object_and_revision(
            binding=binding,
            kind=SourceKind.COMMENT,
            external_id=comment.comment_id,
            title=f"comment by {comment.author or 'unknown'}",
            parent_source_id=parent_ref,
            source_version=comment.updated_at.isoformat(),
            content=canonical_payload_hash(payload),
            metadata={
                "author": comment.author,
                "body": comment.body or "",
                "parent_kind": parent_kind.value,
                "parent_number": parent_number,
                "html_url": comment.html_url,
            },
            source_updated_at=comment.updated_at,
            canonical_url=comment.html_url,
            payload_for_hash=payload,
            precomputed_source_id=identity.source_id(),
        )

    # -- shared write path ----------------------------------------------------------

    async def _record_object_and_revision(
        self,
        binding: ProjectSourceBinding,
        kind: SourceKind,
        external_id: str,
        title: str | None,
        source_version: str,
        content: str,
        metadata: dict[str, Any],
        source_updated_at: datetime | None,
        canonical_url: str | None,
        payload_for_hash: dict[str, Any] | None = None,
        parent_source_id: str | None = None,
        precomputed_source_id: str | None = None,
    ) -> None:
        """Writes object + revision; platform objects hash their canonical payload,
        documents hash (and keep) the raw Markdown content."""
        identity = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=kind,
            external_id=external_id,
            parent_source_id=parent_source_id,
        )
        source_id = precomputed_source_id or identity.source_id()
        now = self.clock.now()
        existing = await self.sources.get_source_object(source_id)
        obj = SourceObject(
            source_id=source_id,
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=kind,
            external_id=external_id,
            parent_source_id=parent_source_id,
            canonical_url=canonical_url,
            title=title,
            availability=SourceAvailability.AVAILABLE,
            latest_revision_id=existing.latest_revision_id if existing else None,
            latest_change_sequence=existing.latest_change_sequence if existing else 0,
            last_observed_at=now,
            last_verified_at=now,
            source_updated_at=source_updated_at,
            metadata=metadata,
        )
        await self.sources.upsert_source_object(obj)
        revision_content_hash = (
            canonical_payload_hash(payload_for_hash)
            if payload_for_hash is not None
            else content_hash(content)
        )
        revision = SourceRevision(
            revision_id=new_revision_id(),
            source_id=source_id,
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            access_epoch=binding.access_epoch,
            kind=kind,
            source_version=source_version,
            change_sequence=(existing.latest_change_sequence + 1) if existing else 1,
            content_hash=revision_content_hash,
            raw_content=None if payload_for_hash is not None else content,
            metadata=metadata,
            source_updated_at=source_updated_at,
            observed_at=now,
        )
        await self.sources.record_revision(revision)

    async def _mark_object_unavailable(
        self,
        binding: ProjectSourceBinding,
        kind: SourceKind,
        external_id: str,
        exc: GitHubReadError,
    ) -> None:
        """403/404 marks availability down; it never writes ``deleted`` (§5.4)."""
        if not is_access_failure(exc.status_code):
            return
        identity = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=kind,
            external_id=external_id,
        )
        obj = await self.sources.get_source_object(identity.source_id())
        if obj is None:
            return
        await self.sources.set_source_availability(
            identity.source_id(),
            SourceAvailability.UNAVAILABLE,
            f"platform status {exc.status_code}",
        )

    # -- coverage -------------------------------------------------------------------

    def _finalize_coverage(self, report: ImportReport) -> None:
        """Derives honest per-kind coverage from failures and truncations.

        A kind with zero objects and no failure is only marked complete when
        the binding actually declared that kind (an empty scope reads as
        complete-with-zero); any failure makes it partial (or failed with zero
        successes), and any cap hit makes it partial. Nothing pretends.
        """
        for kind in SourceKind:
            name = kind.value
            failures = [f for f in report.failures if f.kind == name]
            # A work-items cap hides mixed issue/PR objects from the listing.
            cap_hits = any(
                t.kind == name or (t.kind == "work_items" and name in ("issue", "pull_request"))
                for t in report.truncations
            )
            count = report.counts.get(name, 0)
            if failures:
                report.coverage[name] = (
                    CoverageStatus.PARTIAL.value if count > 0 else CoverageStatus.FAILED.value
                )
            elif cap_hits:
                report.coverage[name] = CoverageStatus.PARTIAL.value
            elif count > 0 or self._kind_declared(name):
                report.coverage[name] = CoverageStatus.COMPLETE.value
            # else: kind absent from both scope and store — no coverage claim.

    def _kind_declared(self, kind: str) -> bool:
        """Whether the binding declares this kind, even if the scope is empty.

        Precondition: ``import_binding_scope`` stored ``self._binding``.
        """
        binding = self._binding
        if binding is None:
            return False
        if kind == SourceKind.DOCUMENT.value:
            return bool(binding.path_allowlist)
        if kind == SourceKind.ISSUE.value:
            return (
                binding.issue_selector.milestone is not None
                or bool(binding.issue_selector.issue_ids)
            )
        if kind == SourceKind.PULL_REQUEST.value:
            return bool(binding.explicit_pr_ids)
        if kind == SourceKind.COMMENT.value:
            # Comments are read for every imported issue/PR.
            return True
        if kind == SourceKind.REVIEW.value:
            return bool(binding.explicit_pr_ids)
        return False


def _run_state_for(report: ImportReport) -> SyncRunState:
    """A run that enumerated everything completed; all-failed is a failure."""
    if report.coverage and all(
        status == CoverageStatus.FAILED.value for status in report.coverage.values()
    ):
        return SyncRunState.FAILED
    return SyncRunState.COMPLETED

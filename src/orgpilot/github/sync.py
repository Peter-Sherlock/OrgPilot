"""Webhook ingest, incremental refresh, rechecks, and recovery (ADR-0015 C3).

Flow guarantees frozen here:

- Signature verification happens before anything is persisted; the delivery
  is written into ``sync_inbox`` before the HTTP response, and the same
  delivery id with a different payload is rejected, never overwritten.
- One project drains serially. Refreshes read from the platform *outside* any
  transaction, then commit object/revision writes, the sync-run checkpoint,
  and the inbox completion in **one** shared transaction, so a crash leaves
  the entry pending with no half-written revision.
- Out-of-order or stale notifications never roll the current material back:
  a payload older than the stored latest revision is skipped as stale, and
  content-based revision dedup absorbs true no-change refreshes.
- 403/404 mark availability down; deletion is inferred only from a *complete*
  enumeration (manual recheck), never from one missing page or one 404.
- Bounded retries: transient failures park the entry in ``retry_wait`` with a
  persisted ``retry_at``; after ``max_attempts`` the entry is ``failed`` and
  never loops in the worker.
- Periodic recheck is off by default and refreshes material only — it never
  triggers coordination turns, member notifications, or business writes.
"""

import asyncio
import contextlib
import hashlib
import hmac
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.github.client import (
    UNSUPPORTED_CONTENT_STATUS,
    GitHubReadClient,
    GitHubReadError,
    HttpGitHubClient,
    StaticTokenCredentialProvider,
    is_access_failure,
    read_github,
)
from orgpilot.knowledge.importer import ImportLimits, ImportReport, SourceImporter
from orgpilot.knowledge.models import (
    InboxStatus,
    ProjectSourceBinding,
    SourceAvailability,
    SourceKind,
    SourceObject,
    SourceRevision,
    SyncInboxEntry,
    SyncRunReason,
    SyncRunState,
    canonical_payload_hash,
    content_hash,
    new_revision_id,
)
from orgpilot.storage.database import Database
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import (
    SqlProjectSourceStore,
    SqlSyncInboxStore,
    SqlSyncRunStore,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BACKOFF_SECONDS = 60
SUPPORTED_EVENTS = frozenset(
    {"push", "issues", "issue_comment", "pull_request", "pull_request_review", "ping"}
)


def verify_signature(secret: str, payload_body: bytes, signature_header: str | None) -> bool:
    """Constant-time HMAC-SHA256 check of the ``X-Hub-Signature-256`` header."""
    if not secret or not signature_header:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header[len(prefix) :].lower(), expected)


@dataclass
class RefreshOutcome:
    """What one inbox entry's refresh did, plus the writes to commit atomically."""

    result: str
    writes: list[tuple[SourceObject, SourceRevision]] = field(default_factory=list)
    takedowns: list[tuple[str, SourceAvailability, str]] = field(default_factory=list)
    # Sources read successfully again whose content did not change; their
    # earlier unavailable/deleted state is cleared in the same transaction.
    restores: list[str] = field(default_factory=list)
    detail: str | None = None
    unreadable_documents: list[SourceObject] = field(default_factory=list)
    read_errors: list[str] = field(default_factory=list)


# Runs whose checkpoint means "the whole scope was read": the periodic recheck
# interval counts from these, never from a webhook drain.
FULL_READ_REASONS = (
    SyncRunReason.INITIAL_IMPORT,
    SyncRunReason.MANUAL_RECHECK,
    SyncRunReason.PERIODIC_RECHECK,
)


def _needs_restore(existing: SourceObject | None) -> bool:
    return existing is not None and existing.availability in (
        SourceAvailability.UNAVAILABLE,
        SourceAvailability.DELETED,
    )


class GitHubSyncService:
    """Single-writer sync: ingest deliveries, drain them serially, recheck."""

    def __init__(
        self,
        db: Database,
        sources: SqlProjectSourceStore,
        inbox_store: SqlSyncInboxStore,
        runs: SqlSyncRunStore,
        knowledge: SqlKnowledgeStore,
        clock: Clock | None = None,
        client_factory: Callable[[ProjectSourceBinding], GitHubReadClient] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        recheck_interval_seconds: int = 1800,
        import_limits: ImportLimits | None = None,
    ) -> None:
        self.db = db
        self.sources = sources
        self.inbox = inbox_store
        self.runs = runs
        self.knowledge = knowledge
        self.clock: Clock = clock or SystemClock()
        self.client_factory = client_factory
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.recheck_interval_seconds = recheck_interval_seconds
        self.import_limits = import_limits or ImportLimits()
        self._drain_locks: dict[str, asyncio.Lock] = {}
        self._wake = asyncio.Event()
        self._stopping = False
        self._accepting = True
        self._worker: asyncio.Task | None = None
        self._bootstrap_attempted: set[tuple[str, int, int]] = set()

    async def start(self, poll_seconds: float = 1.0) -> None:
        if self._worker is not None and not self._worker.done():
            return
        await self.startup_recovery()
        self._stopping, self._accepting = False, True
        self._worker = asyncio.create_task(self._work_loop(poll_seconds))
        self._wake.set()

    async def stop(self) -> None:
        self._accepting, self._stopping = False, True
        self._wake.set()
        if self._worker is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._worker), timeout=45.0)
            except TimeoutError:
                self._worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worker
            self._worker = None

    async def _work_loop(self, poll_seconds: float) -> None:
        while not self._stopping:
            self._wake.clear()
            for binding in await self.sources.list_active_bindings():
                if self._stopping:
                    break
                if self.client_factory is None:
                    continue
                try:
                    current_id = await self.knowledge.current_generation_id(binding.project_id)
                    current = (
                        await self.knowledge.get_generation(current_id) if current_id else None
                    )
                    binding_key = (
                        binding.project_id,
                        binding.binding_version,
                        binding.access_epoch,
                    )
                    needs_import = current is None or (
                        current.binding_version,
                        current.access_epoch,
                    ) != (binding.binding_version, binding.access_epoch)
                    if needs_import and binding_key not in self._bootstrap_attempted:
                        self._bootstrap_attempted.add(binding_key)
                        await self.manual_recheck(binding.project_id)
                    if await self.inbox.list_due(binding.repository_id, limit=1):
                        await self.drain_repository(binding.repository_id)
                    else:
                        async with self._lock_for(binding.repository_id):
                            await self._publish_current(binding)
                except Exception:
                    logger.exception("GitHub material worker failed for %s", binding.project_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=poll_seconds)

    # ---------------------------------------------------------------- ingest

    async def resolve_binding(self, repository_id: str) -> ProjectSourceBinding | None:
        return await self.sources.find_active_binding_for_repository(repository_id)

    async def ingest_webhook(
        self,
        repository_id: str,
        delivery_id: str,
        event_type: str,
        action: str | None,
        payload: dict[str, Any],
        received_at: datetime | None = None,
    ) -> str:
        """Persists one verified delivery before any acknowledgement (§5.3)."""
        if not self._accepting:
            raise RuntimeError("GitHub sync is shutting down")
        received = received_at or self.clock.now()
        entry = SyncInboxEntry(
            repository_id=str(repository_id),
            delivery_id=delivery_id,
            event_type=event_type,
            action=action,
            payload_hash=canonical_payload_hash(payload),
            payload=payload,
            received_at=received,
        )
        stored = await self.inbox.ingest(entry)
        self._wake.set()
        return "accepted" if stored else "duplicate"

    # ----------------------------------------------------------------- drain

    def _lock_for(self, repository_id: str) -> asyncio.Lock:
        if repository_id not in self._drain_locks:
            self._drain_locks[repository_id] = asyncio.Lock()
        return self._drain_locks[repository_id]

    async def drain_repository(self, repository_id: str, limit: int = 50) -> dict[str, int]:
        """Serially processes due inbox entries for one repository.

        Returns counters; entries that need a platform client but none is
        configured stay pending (ingest-only mode never fakes a refresh).
        """
        async with self._lock_for(str(repository_id)):
            return await self._drain_locked(repository_id, limit)

    async def _drain_locked(self, repository_id: str, limit: int) -> dict[str, int]:
        counts = {
            "processed": 0,
            "refreshed": 0,
            "unchanged": 0,
            "stale_skipped": 0,
            "ignored": 0,
            "retry_wait": 0,
            "failed": 0,
            "pending_no_client": 0,
        }
        binding = await self.resolve_binding(repository_id)
        if binding is None:
            return counts
        client = self.client_factory(binding) if self.client_factory is not None else None
        if client is None:
            counts["pending_no_client"] = len(await self.inbox.list_due(repository_id))
            return counts
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        await self.runs.start_run(
            run_id,
            binding.project_id,
            binding_version=binding.binding_version,
            access_epoch=binding.access_epoch,
            reason=SyncRunReason.WEBHOOK,
            candidate_manifest={"repository_id": str(repository_id)},
        )
        entries = await self.inbox.list_due(repository_id, limit=limit)
        for entry in entries:
            result = await self._process_entry(entry, binding, client, run_id)
            counts["processed"] += 1
            counts[result if result in counts else "refreshed"] += 1
        await self.runs.set_run_state(run_id, SyncRunState.COMPLETED)
        await self._publish_current(binding)
        return counts

    async def _publish_current(self, binding: ProjectSourceBinding, coverage=None) -> None:
        from orgpilot.knowledge.derive import DerivationPipeline

        current_id = await self.knowledge.current_generation_id(binding.project_id)
        base = await self.knowledge.get_generation(current_id) if current_id else None
        if (
            base is not None
            and base.valid
            and (base.binding_version, base.access_epoch)
            == (binding.binding_version, binding.access_epoch)
            and (coverage is None or coverage == base.coverage)
        ):
            return
        if not await self.sources.list_source_objects(
            binding.project_id, binding.binding_version
        ) and (not coverage or any(status != "complete" for status in coverage.values())):
            return
        pipeline = DerivationPipeline(self.sources, self.knowledge, self.clock)
        await pipeline.build_and_publish(
            binding, reason="sync", base_generation=base, coverage_override=coverage
        )

    async def _process_entry(
        self,
        entry: SyncInboxEntry,
        binding: ProjectSourceBinding,
        client: GitHubReadClient,
        run_id: str,
    ) -> str:
        """Claims one entry, refreshes, and commits everything atomically."""
        await self.inbox.set_status(entry.repository_id, entry.delivery_id, InboxStatus.PROCESSING)
        try:
            outcome = await self._apply_event(entry, binding, client)
        except GitHubReadError as exc:
            if is_access_failure(exc.status_code):
                await self.inbox.set_status(
                    entry.repository_id,
                    entry.delivery_id,
                    InboxStatus.DONE,
                    attempts_delta=1,
                    last_error=f"access failure: {exc}",
                )
                return "ignored"
            return await self._park_for_retry(entry, str(exc))
        except Exception as exc:  # noqa: BLE001 - worker must never crash-loop
            return await self._park_for_retry(entry, f"unexpected: {exc}")

        # Network work is done; revision writes + checkpoint + inbox completion
        # now share one transaction (§5.3). A crash here leaves the entry
        # pending with nothing half-written.
        async with self.db.session() as session:
            for obj, revision in outcome.writes:
                await self.sources.upsert_source_object(obj, session)
                await self.sources.record_revision(revision, session)
                # A successful read ends an earlier 403/404 or deletion.
                await self.sources.restore_observed_availability(obj.source_id, session)
            for source_id in outcome.restores:
                await self.sources.restore_observed_availability(source_id, session)
            for obj in outcome.unreadable_documents:
                await self.sources.upsert_source_object(obj, session)
            for source_id, availability, reason in outcome.takedowns:
                record = await self.sources.get_source_object(source_id, session)
                if record is not None:
                    await self.sources.set_source_availability(
                        source_id, availability, reason, session, platform_observation=True
                    )
            if outcome.writes or outcome.takedowns or outcome.restores:
                await self.runs.save_checkpoint(
                    run_id,
                    per_source_cursor={"delivery_id": entry.delivery_id},
                    coverage={"last_event": entry.event_type},
                    last_success_at=self.clock.now(),
                    session=session,
                )
            await self.inbox.set_status(
                entry.repository_id,
                entry.delivery_id,
                InboxStatus.DONE,
                attempts_delta=1,
                last_error="; ".join(outcome.read_errors)[:500] if outcome.read_errors else None,
                session=session,
            )
        return outcome.result

    async def _park_for_retry(self, entry: SyncInboxEntry, error: str) -> str:
        attempts = entry.attempts + 1
        if attempts >= self.max_attempts:
            await self.inbox.set_status(
                entry.repository_id,
                entry.delivery_id,
                InboxStatus.FAILED,
                attempts_delta=1,
                last_error=error,
            )
            return "failed"
        retry_at = self.clock.now() + timedelta(
            seconds=self.retry_backoff_seconds * (2 ** (attempts - 1))
        )
        await self.inbox.set_status(
            entry.repository_id,
            entry.delivery_id,
            InboxStatus.RETRY_WAIT,
            attempts_delta=1,
            last_error=error,
            retry_at=retry_at,
        )
        return "retry_wait"

    # ------------------------------------------------------------- event apply

    async def _apply_event(
        self,
        entry: SyncInboxEntry,
        binding: ProjectSourceBinding,
        client: GitHubReadClient,
    ) -> RefreshOutcome:
        event = entry.event_type
        if event not in SUPPORTED_EVENTS:
            return RefreshOutcome(result="ignored", detail=f"unsupported event {event}")
        if event == "ping":
            return RefreshOutcome(result="ignored", detail="ping")
        if event == "push":
            return await self._apply_push(entry, binding, client)
        if event == "issues":
            number = self._issue_number(entry.payload)
            if number is None:
                return RefreshOutcome(result="ignored", detail="missing issue number")
            return await self._refresh_issue(binding, client, number)
        if event == "issue_comment":
            number = self._issue_number(entry.payload)
            if number is None:
                return RefreshOutcome(result="ignored", detail="missing issue number")
            # GitHub marks comments on pull requests with issue.pull_request.
            on_pull_request = "pull_request" in (entry.payload.get("issue") or {})
            return await self._refresh_comments(binding, client, number, on_pull_request)
        if event == "pull_request":
            number = self._pr_number(entry.payload)
            if number is None:
                return RefreshOutcome(result="ignored", detail="missing PR number")
            return await self._refresh_pull_request(binding, client, number)
        if event == "pull_request_review":
            number = self._pr_number(entry.payload)
            if number is None:
                return RefreshOutcome(result="ignored", detail="missing PR number")
            return await self._refresh_reviews(binding, client, number)
        return RefreshOutcome(result="ignored", detail="unreachable")  # pragma: no cover

    @staticmethod
    def _issue_number(payload: dict[str, Any]) -> int | None:
        issue = payload.get("issue") or {}
        number = issue.get("number")
        return int(number) if number is not None else None

    @staticmethod
    def _pr_number(payload: dict[str, Any]) -> int | None:
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if number is None:
            number = (payload.get("issue") or {}).get("number")
        return int(number) if number is not None else None

    async def _apply_push(
        self,
        entry: SyncInboxEntry,
        binding: ProjectSourceBinding,
        client: GitHubReadClient,
    ) -> RefreshOutcome:
        """A push refreshes documents only for the bound branch (§5.3)."""
        ref = entry.payload.get("ref", "")
        if ref != f"refs/heads/{binding.branch}":
            return RefreshOutcome(result="ignored", detail=f"push to unbound ref {ref}")
        return await self._refresh_documents(binding, client)

    async def _refresh_documents(
        self, binding: ProjectSourceBinding, client: GitHubReadClient
    ) -> RefreshOutcome:
        outcome = RefreshOutcome(result="unchanged")
        head = await read_github(client, "get_branch_head", binding.branch)
        tree = await read_github(client, "get_tree", head.sha)
        allowed = sorted(
            (item for item in tree if binding.allows_document(item.path)),
            key=lambda item: item.path,
        )
        seen_paths: set[str] = set()
        for item in allowed:
            normalized = item.path.replace("\\", "/")
            seen_paths.add(normalized)
            existing = await self.sources.find_source_object(
                binding.project_id, binding.binding_version, SourceKind.DOCUMENT, normalized
            )
            try:
                content = await read_github(client, "get_blob", item.blob_sha)
            except GitHubReadError as exc:
                if exc.status_code != UNSUPPORTED_CONTENT_STATUS:
                    raise
                reason = "blob is not UTF-8 Markdown (415)"
                outcome.read_errors.append(f"{normalized}: {reason}")
                if existing is None:
                    source_id = await self._new_document_identity(binding, normalized)
                    outcome.unreadable_documents.append(
                        SourceObject(
                            source_id=source_id,
                            project_id=binding.project_id,
                            binding_version=binding.binding_version,
                            kind=SourceKind.DOCUMENT,
                            external_id=normalized,
                            title=normalized.rsplit("/", 1)[-1],
                            availability=SourceAvailability.UNAVAILABLE,
                            unavailable_reason=reason,
                        )
                    )
                elif (
                    existing.availability is SourceAvailability.UNAVAILABLE
                    and existing.unavailable_reason == reason
                ):
                    continue
                else:
                    source_id = existing.source_id
                outcome.takedowns.append((source_id, SourceAvailability.UNAVAILABLE, reason))
                continue
            now = self.clock.now()
            source_id = (
                existing.source_id
                if existing
                else await self._new_document_identity(binding, normalized)
            )
            metadata = {"blob_sha": item.blob_sha, "size": item.size}
            # Pre-check dedup: unchanged documents queue no write (the
            # transactional commit re-checks defensively).
            if existing is not None:
                latest = await self.sources.latest_revision(source_id)
                if latest is not None and latest.content_hash == content_hash(content):
                    # Same content, e.g. a deleted file restored as it was.
                    if _needs_restore(existing):
                        outcome.restores.append(source_id)
                    continue
            revision = SourceRevision(
                revision_id=new_revision_id(),
                source_id=source_id,
                project_id=binding.project_id,
                binding_version=binding.binding_version,
                access_epoch=binding.access_epoch,
                kind=SourceKind.DOCUMENT,
                source_version=head.sha,
                change_sequence=(existing.latest_change_sequence + 1) if existing else 1,
                content_hash=content_hash(content),
                raw_content=content,
                metadata=metadata,
                observed_at=now,
            )
            obj = SourceObject(
                source_id=source_id,
                project_id=binding.project_id,
                binding_version=binding.binding_version,
                kind=SourceKind.DOCUMENT,
                external_id=normalized,
                title=normalized.rsplit("/", 1)[-1],
                availability=SourceAvailability.AVAILABLE,
                latest_revision_id=existing.latest_revision_id if existing else None,
                latest_change_sequence=existing.latest_change_sequence if existing else 0,
                last_observed_at=now,
                last_verified_at=now,
                metadata=metadata,
            )
            outcome.writes.append((obj, revision))
        # The tree enumeration completed, even if an individual blob failed.
        # Absence from that tree confirms deletion (§5.4).
        stored_docs = await self.sources.list_source_objects(
            binding.project_id, binding.binding_version, kind=SourceKind.DOCUMENT
        )
        for doc in stored_docs:
            visible_or_unavailable = doc.availability in (
                SourceAvailability.AVAILABLE,
                SourceAvailability.UNAVAILABLE,
            )
            if visible_or_unavailable and doc.external_id not in seen_paths:
                outcome.takedowns.append(
                    (doc.source_id, SourceAvailability.DELETED, "missing from complete tree")
                )
        if outcome.writes or outcome.takedowns or outcome.restores:
            outcome.result = "refreshed"
        return outcome

    async def _new_document_identity(self, binding: ProjectSourceBinding, path: str) -> str:
        from orgpilot.knowledge.models import SourceObjectIdentity

        identity = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=SourceKind.DOCUMENT,
            external_id=path,
        )
        return identity.source_id()

    async def _refresh_issue(
        self, binding: ProjectSourceBinding, client: GitHubReadClient, number: int
    ) -> RefreshOutcome:
        try:
            issue = await read_github(client, "get_issue", number)
        except GitHubReadError as exc:
            return await self._takedown_or_fail(binding, SourceKind.ISSUE, str(number), exc)
        if issue.is_pull_request:
            return await self._refresh_pull_request(binding, client, number)
        # Scope is decided from platform data + the stored binding, never from
        # the notification payload's own claims (§2.2). A stored object stays
        # in scope even if the platform milestone moved away (scope exits go
        # through full rechecks, not single refreshes).
        selected = binding.issue_selector.covers(number, issue.milestone) or (
            number in binding.explicit_pr_ids
        )
        if not selected:
            stored = await self.sources.find_source_object(
                binding.project_id, binding.binding_version, SourceKind.ISSUE, str(number)
            )
            if stored is None:
                return RefreshOutcome(result="ignored", detail=f"issue #{number} outside selector")
        return await self._record_work_item(
            binding,
            SourceKind.ISSUE,
            number,
            issue.updated_at,
            issue.model_dump(mode="json"),
            extra_metadata={
                "state": issue.state,
                "assignees": list(issue.assignees),
                "labels": list(issue.labels),
                "milestone": issue.milestone,
                "body": issue.body or "",
                "html_url": issue.html_url,
            },
            title=issue.title,
            canonical_url=issue.html_url,
        )

    async def _refresh_pull_request(
        self, binding: ProjectSourceBinding, client: GitHubReadClient, number: int
    ) -> RefreshOutcome:
        try:
            pr = await read_github(client, "get_pull_request", number)
        except GitHubReadError as exc:
            return await self._takedown_or_fail(binding, SourceKind.PULL_REQUEST, str(number), exc)
        selected = number in binding.explicit_pr_ids or binding.issue_selector.covers(
            number, pr.milestone
        )
        if not selected:
            stored = await self.sources.find_source_object(
                binding.project_id, binding.binding_version, SourceKind.PULL_REQUEST, str(number)
            )
            if stored is None:
                return RefreshOutcome(result="ignored", detail=f"PR #{number} out of scope")
        return await self._record_work_item(
            binding,
            SourceKind.PULL_REQUEST,
            number,
            pr.updated_at,
            pr.model_dump(mode="json"),
            extra_metadata={
                "state": pr.state,
                "milestone": pr.milestone,
                "merged": pr.merged,
                "merge_commit_sha": pr.merge_commit_sha,
                "head_sha": pr.head_sha,
                "base_sha": pr.base_sha,
                "body": pr.body or "",
                "html_url": pr.html_url,
            },
            title=pr.title,
            canonical_url=pr.html_url,
        )

    async def _record_work_item(
        self,
        binding: ProjectSourceBinding,
        kind: SourceKind,
        number: int,
        updated_at: datetime,
        payload: dict[str, Any],
        *,
        extra_metadata: dict[str, Any],
        title: str | None,
        canonical_url: str | None,
    ) -> RefreshOutcome:
        from orgpilot.knowledge.models import SourceObjectIdentity

        identity = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=kind,
            external_id=str(number),
        )
        existing = await self.sources.get_source_object(identity.source_id())
        latest = await self.sources.latest_revision(identity.source_id())
        # The platform just answered for this object, so an earlier 403/404
        # (or a deletion inferred from a recheck) is over.
        restores = [identity.source_id()] if _needs_restore(existing) else []
        # Stale notification guard: an older payload never rolls the object
        # back (§5.3); equal timestamps fall through to content dedup.
        if (
            latest is not None
            and latest.source_updated_at is not None
            and updated_at < latest.source_updated_at
        ):
            return RefreshOutcome(
                result="refreshed" if restores else "stale_skipped",
                restores=restores,
                detail=f"#{number} payload older",
            )
        now = self.clock.now()
        metadata = dict(extra_metadata)
        payload_hash = canonical_payload_hash(payload)
        # Content-level dedup before queueing: unchanged observations queue no
        # writes; the transactional commit re-checks defensively anyway.
        if latest is not None and latest.content_hash == payload_hash:
            return RefreshOutcome(
                result="refreshed" if restores else "unchanged",
                restores=restores,
                detail=f"#{number} unchanged",
            )
        revision = SourceRevision(
            revision_id=new_revision_id(),
            source_id=identity.source_id(),
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            access_epoch=binding.access_epoch,
            kind=kind,
            source_version=updated_at.isoformat(),
            change_sequence=(existing.latest_change_sequence + 1) if existing else 1,
            content_hash=payload_hash,
            raw_content=None,
            metadata=metadata,
            source_updated_at=updated_at,
            observed_at=now,
        )
        obj = SourceObject(
            source_id=identity.source_id(),
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=kind,
            external_id=str(number),
            title=title,
            canonical_url=canonical_url,
            availability=SourceAvailability.AVAILABLE,
            unavailable_reason=None,
            latest_revision_id=existing.latest_revision_id if existing else None,
            latest_change_sequence=existing.latest_change_sequence if existing else 0,
            source_updated_at=updated_at,
            last_observed_at=now,
            last_verified_at=now,
            metadata=metadata,
        )
        return RefreshOutcome(result="refreshed", writes=[(obj, revision)])

    async def _refresh_comments(
        self,
        binding: ProjectSourceBinding,
        client: GitHubReadClient,
        number: int,
        on_pull_request: bool = False,
    ) -> RefreshOutcome:
        """Sub-object edits refresh independently of the parent's updated_at.

        The parent kind comes from the event (``issue.pull_request``) or an
        explicit PR id; a PR picked by the milestone selector is not in
        ``explicit_pr_ids``, so the stored object decides when the event is
        silent.
        """
        is_pr = on_pull_request or number in binding.explicit_pr_ids
        candidates = (
            (SourceKind.PULL_REQUEST, SourceKind.ISSUE)
            if is_pr
            else (SourceKind.ISSUE, SourceKind.PULL_REQUEST)
        )
        stored_parent = None
        parent_kind = candidates[0]
        for kind in candidates:
            stored_parent = await self.sources.find_source_object(
                binding.project_id, binding.binding_version, kind, str(number)
            )
            if stored_parent is not None:
                parent_kind = kind
                break
        if stored_parent is None:
            return RefreshOutcome(result="ignored", detail="parent not in scope")
        outcome = RefreshOutcome(result="unchanged")
        page = 1
        while True:
            batch = await read_github(client, "list_issue_comments", number, page=page, per_page=50)
            for comment in batch:
                from orgpilot.knowledge.models import SourceObjectIdentity

                identity = SourceObjectIdentity(
                    project_id=binding.project_id,
                    binding_version=binding.binding_version,
                    kind=SourceKind.COMMENT,
                    external_id=comment.comment_id,
                    parent_source_id=stored_parent.source_id,
                )
                existing = await self.sources.get_source_object(identity.source_id())
                payload = comment.model_dump(mode="json")
                payload_hash = canonical_payload_hash(payload)
                if existing is not None:
                    latest = await self.sources.latest_revision(identity.source_id())
                    if latest is not None and latest.content_hash == payload_hash:
                        if _needs_restore(existing):
                            outcome.restores.append(identity.source_id())
                        continue
                now = self.clock.now()
                metadata = {
                    "author": comment.author,
                    "body": comment.body or "",
                    "parent_kind": parent_kind.value,
                    "parent_number": number,
                    "html_url": comment.html_url,
                }
                revision = SourceRevision(
                    revision_id=new_revision_id(),
                    source_id=identity.source_id(),
                    project_id=binding.project_id,
                    binding_version=binding.binding_version,
                    access_epoch=binding.access_epoch,
                    kind=SourceKind.COMMENT,
                    source_version=comment.updated_at.isoformat(),
                    change_sequence=(existing.latest_change_sequence + 1) if existing else 1,
                    content_hash=payload_hash,
                    raw_content=None,
                    metadata=metadata,
                    source_updated_at=comment.updated_at,
                    observed_at=now,
                )
                obj = SourceObject(
                    source_id=identity.source_id(),
                    project_id=binding.project_id,
                    binding_version=binding.binding_version,
                    kind=SourceKind.COMMENT,
                    external_id=comment.comment_id,
                    parent_source_id=stored_parent.source_id,
                    title=f"comment by {comment.author or 'unknown'}",
                    canonical_url=comment.html_url,
                    availability=SourceAvailability.AVAILABLE,
                    latest_revision_id=existing.latest_revision_id if existing else None,
                    latest_change_sequence=existing.latest_change_sequence if existing else 0,
                    source_updated_at=comment.updated_at,
                    last_observed_at=now,
                    last_verified_at=now,
                    metadata=metadata,
                )
                outcome.writes.append((obj, revision))
            if len(batch) < 50:
                break
            page += 1
        if outcome.writes or outcome.restores:
            outcome.result = "refreshed"
        return outcome

    async def _refresh_reviews(
        self, binding: ProjectSourceBinding, client: GitHubReadClient, pr_number: int
    ) -> RefreshOutcome:
        stored_pr = await self.sources.find_source_object(
            binding.project_id, binding.binding_version, SourceKind.PULL_REQUEST, str(pr_number)
        )
        if stored_pr is None:
            return RefreshOutcome(result="ignored", detail="PR not in scope")
        outcome = RefreshOutcome(result="unchanged")
        page = 1
        while True:
            batch = await read_github(
                client, "list_pull_request_reviews", pr_number, page=page, per_page=50
            )
            for review in batch:
                from orgpilot.knowledge.models import SourceObjectIdentity

                identity = SourceObjectIdentity(
                    project_id=binding.project_id,
                    binding_version=binding.binding_version,
                    kind=SourceKind.REVIEW,
                    external_id=review.review_id,
                    parent_source_id=stored_pr.source_id,
                )
                existing = await self.sources.get_source_object(identity.source_id())
                payload = review.model_dump(mode="json")
                payload_hash = canonical_payload_hash(payload)
                if existing is not None:
                    latest = await self.sources.latest_revision(identity.source_id())
                    if latest is not None and latest.content_hash == payload_hash:
                        if _needs_restore(existing):
                            outcome.restores.append(identity.source_id())
                        continue
                now = self.clock.now()
                metadata = {
                    "state": review.state,
                    "author": review.author,
                    "body": review.body or "",
                    "pr_number": pr_number,
                }
                revision = SourceRevision(
                    revision_id=new_revision_id(),
                    source_id=identity.source_id(),
                    project_id=binding.project_id,
                    binding_version=binding.binding_version,
                    access_epoch=binding.access_epoch,
                    kind=SourceKind.REVIEW,
                    source_version=review.submitted_at.isoformat(),
                    change_sequence=(existing.latest_change_sequence + 1) if existing else 1,
                    content_hash=payload_hash,
                    raw_content=None,
                    metadata=metadata,
                    source_updated_at=review.submitted_at,
                    observed_at=now,
                )
                obj = SourceObject(
                    source_id=identity.source_id(),
                    project_id=binding.project_id,
                    binding_version=binding.binding_version,
                    kind=SourceKind.REVIEW,
                    external_id=review.review_id,
                    parent_source_id=stored_pr.source_id,
                    title=f"review by {review.author or 'unknown'}: {review.state}",
                    availability=SourceAvailability.AVAILABLE,
                    latest_revision_id=existing.latest_revision_id if existing else None,
                    latest_change_sequence=existing.latest_change_sequence if existing else 0,
                    source_updated_at=review.submitted_at,
                    last_observed_at=now,
                    last_verified_at=now,
                    metadata=metadata,
                )
                outcome.writes.append((obj, revision))
            if len(batch) < 50:
                break
            page += 1
        if outcome.writes or outcome.restores:
            outcome.result = "refreshed"
        return outcome

    async def _takedown_or_fail(
        self,
        binding: ProjectSourceBinding,
        kind: SourceKind,
        external_id: str,
        exc: GitHubReadError,
    ) -> RefreshOutcome:
        """403/404 only drops availability; deletion waits for a full recheck."""
        from orgpilot.knowledge.models import SourceObjectIdentity

        identity = SourceObjectIdentity(
            project_id=binding.project_id,
            binding_version=binding.binding_version,
            kind=kind,
            external_id=external_id,
        )
        if is_access_failure(exc.status_code):
            return RefreshOutcome(
                result="ignored",
                takedowns=[
                    (
                        identity.source_id(),
                        SourceAvailability.UNAVAILABLE,
                        f"platform status {exc.status_code}",
                    )
                ],
                detail=str(exc),
            )
        raise exc  # transient: the entry parks for retry

    # ------------------------------------------------------------- recheck

    async def manual_recheck(self, project_id: str) -> ImportReport | None:
        """Full-scope recheck; refreshes material only (ADR-0015 §5.4)."""
        binding = await self.sources.get_active_binding(project_id)
        if binding is None:
            return None
        async with self._lock_for(binding.repository_id):
            return await self._recheck_locked(project_id)

    async def _recheck_locked(self, project_id: str) -> ImportReport | None:
        binding = await self.sources.get_active_binding(project_id)
        if binding is None:
            return None
        if self.client_factory is None:
            return None
        client = self.client_factory(binding)
        importer = SourceImporter(
            client=client,
            sources=self.sources,
            knowledge=self.knowledge,
            runs=self.runs,
            clock=self.clock,
            limits=self.import_limits,
        )
        report = await importer.import_binding_scope(binding)
        if report.fully_complete:
            await self._reconcile_deletions(binding, report)
        await self._publish_current(binding, coverage=report.coverage)
        return report

    async def _reconcile_deletions(
        self, binding: ProjectSourceBinding, report: ImportReport
    ) -> int:
        """Takes down objects missing from a *complete* enumeration only."""
        taken_down = 0
        for kind in SourceKind:
            seen = set(report.seen_external_ids.get(kind.value, ()))
            stored_objects = await self.sources.list_source_objects(
                binding.project_id, binding.binding_version, kind=kind
            )
            for obj in stored_objects:
                visible_or_unavailable = obj.availability in (
                    SourceAvailability.AVAILABLE,
                    SourceAvailability.UNAVAILABLE,
                )
                if visible_or_unavailable and obj.external_id not in seen:
                    await self.sources.set_source_availability(
                        obj.source_id,
                        SourceAvailability.DELETED,
                        "missing from complete recheck enumeration",
                        platform_observation=True,
                    )
                    taken_down += 1
        return taken_down

    async def periodic_recheck_if_due(self, project_id: str) -> bool:
        """Runs the recheck when the configured interval has elapsed.

        The interval counts from the last *full* read (import or recheck). A
        webhook drain that wrote nothing has no success time, and a drain that
        wrote one object did not re-read the scope, so neither may postpone or
        suppress the recheck.
        """
        binding = await self.sources.get_active_binding(project_id)
        if binding is None:
            return False
        latest = await self.runs.latest_complete_read(
            project_id, binding.binding_version, binding.access_epoch, FULL_READ_REASONS
        )
        last_success = latest.checkpoint.last_success_at if latest and latest.checkpoint else None
        if last_success and self.clock.now() < last_success + timedelta(
            seconds=self.recheck_interval_seconds
        ):
            return False
        report = await self.manual_recheck(project_id)
        return report is not None

    async def recheck_all_due(self) -> list[str]:
        """Periodic entry point: rechecks every project whose interval elapsed."""
        checked: list[str] = []
        for binding in await self.sources.list_active_bindings():
            if await self.periodic_recheck_if_due(binding.project_id):
                checked.append(binding.project_id)
        return checked

    # ------------------------------------------------------------- recovery

    async def startup_recovery(self) -> dict[str, int]:
        """Resumes after a crash: stale claims re-queue, dead runs abandon.

        Completed work is never reprocessed; only ``processing`` entries left
        behind by an interrupted worker return to ``pending`` (§5.3).
        """
        from sqlalchemy import select

        from orgpilot.storage.models import SyncInboxRecord, SyncRunRecord

        reset_entries = 0
        async with self.db.session() as session:
            records = list(
                (
                    await session.execute(
                        select(SyncInboxRecord).where(
                            SyncInboxRecord.status == InboxStatus.PROCESSING.value
                        )
                    )
                ).scalars()
            )
            for record in records:
                record.status = InboxStatus.PENDING.value
                reset_entries += 1
            runs_running = list(
                (
                    await session.execute(
                        select(SyncRunRecord).where(
                            SyncRunRecord.state == SyncRunState.RUNNING.value
                        )
                    )
                ).scalars()
            )
            for record in runs_running:
                record.state = SyncRunState.ABANDONED.value
        return {"processing_reset": reset_entries, "runs_abandoned": len(runs_running)}


def build_github_sync_service(
    settings: OrgPilotSettings,
    db: Database,
    clock: Clock | None = None,
) -> GitHubSyncService:
    """Wires the sync service from settings; None factory = ingest-only mode."""
    client_factory: Callable[[ProjectSourceBinding], GitHubReadClient] | None = None
    if settings.github_token and settings.github_repo_full_name:
        owner, name = settings.github_repo_full_name.split("/", 1)
        credentials = StaticTokenCredentialProvider(lambda: settings.github_token or "")

        def client_factory(binding: ProjectSourceBinding) -> GitHubReadClient:
            return HttpGitHubClient(owner, name, credentials)

    sources = SqlProjectSourceStore(db, clock)
    return GitHubSyncService(
        db=db,
        sources=sources,
        inbox_store=SqlSyncInboxStore(db, clock),
        runs=SqlSyncRunStore(db, clock),
        knowledge=SqlKnowledgeStore(db, clock),
        clock=clock,
        client_factory=client_factory,
        recheck_interval_seconds=settings.github_recheck_interval_seconds,
    )

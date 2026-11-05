"""Versioned value contracts for project knowledge (ADR-0015 §4).

Every model here is a frozen proposal-grade contract: immutable once created,
no silent extra fields. Namespaces are deliberately disjoint from OrgEvent:
source ids start with ``src-`` and revision ids with ``rev-`` so a material id
can never be mistaken for an ``event_id`` or slipped into
``evidence_claim_ids`` (ADR-0015 §6).

All names here are contracts only; persistence lives in
``orgpilot.storage.project_source_store`` and the record models in
``orgpilot.storage.models``.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

SOURCE_ID_PREFIX = "src-"
REVISION_ID_PREFIX = "rev-"
GENERATION_ID_PREFIX = "gen-"
ASSERTION_ID_PREFIX = "kn-"
RELATION_ID_PREFIX = "rel-"
SESSION_ID_PREFIX = "rds-"
PACKET_ID_PREFIX = "pkt-"

# Question hash salt keeps question_hash a pure fingerprint of the question
# text, never of the asker, so the same question re-asked by another member
# still gets a fresh session (identity is the message key, not the hash).
QUESTION_HASH_SALT = "orgpilot-project-question-v1"

# §7.1: the pre-generated project brief carried in every context packet.
PROJECT_BRIEF_MAX_CHARS = 2000
# §7.4 per-tool return ceilings.
SEARCH_QUERY_MAX_CHARS = 200
SEARCH_MAX_HITS = 5
READ_SOURCE_MAX_CHARS = 1000
RELATED_ITEMS_MAX = 5
READ_TASK_STATE_MAX = 5
TOOL_RESULT_MAX_CHARS = 4096


class KnowledgeFrozen(BaseModel):
    """Immutable validated contract model with no silent extra fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include an explicit timezone")
    return value


def utcnow() -> datetime:
    """Wall-clock helper for non-deterministic callers; stores inject a Clock."""
    return datetime.now(UTC)


def new_source_id() -> str:
    return f"{SOURCE_ID_PREFIX}{uuid.uuid4().hex}"


def new_revision_id() -> str:
    return f"{REVISION_ID_PREFIX}{uuid.uuid4().hex}"


def new_generation_id() -> str:
    return f"{GENERATION_ID_PREFIX}{uuid.uuid4().hex}"


def new_assertion_id() -> str:
    return f"{ASSERTION_ID_PREFIX}{uuid.uuid4().hex}"


def new_relation_id() -> str:
    return f"{RELATION_ID_PREFIX}{uuid.uuid4().hex}"


def new_session_id() -> str:
    return f"{SESSION_ID_PREFIX}{uuid.uuid4().hex}"


def new_packet_id() -> str:
    return f"{PACKET_ID_PREFIX}{uuid.uuid4().hex}"


def binding_uid(project_id: str, binding_version: int) -> str:
    """Stable identity of one binding version: ``<project>@v<n>``."""
    return f"{project_id}@v{binding_version}"


def normalize_repo_path(path: str) -> str:
    """Normalizes a repository path so a Markdown file has one stable identity.

    Backslashes, ``./`` segments, leading slashes and duplicate slashes collapse
    to one POSIX form; renaming a file therefore leaves the old path behind and
    registers the new path as its own source object (ADR-0015 §4.2).
    """
    cleaned = path.replace("\\", "/")
    parts: list[str] = []
    for segment in cleaned.split("/"):
        if segment in ("", "."):
            continue
        parts.append(segment)
    return "/".join(parts)


MARKDOWN_SUFFIXES = (".md", ".markdown")


def is_markdown_path(path: str) -> bool:
    """Documents are Markdown files; the suffix check ignores case."""
    return normalize_repo_path(path).lower().endswith(MARKDOWN_SUFFIXES)


def content_hash(raw: str | bytes) -> str:
    """SHA-256 over the exact observable content; observation time never enters."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over a canonical JSON dump of a platform payload."""
    import json

    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return content_hash(serialized)


def question_hash(question: str) -> str:
    normalized = " ".join(question.split())
    return content_hash(f"{QUESTION_HASH_SALT}:{normalized}")


class SourceKind(StrEnum):
    """Object kinds materialized into source objects (ADR-0015 §2.1)."""

    DOCUMENT = "document"
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    COMMENT = "comment"
    REVIEW = "review"


class SourceAvailability(StrEnum):
    """Why (not) a source may currently back an answer (ADR-0015 §4.1)."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    OUT_OF_SCOPE = "out_of_scope"
    DELETED = "deleted"
    REVOKED = "revoked"


class CoverageStatus(StrEnum):
    """Per-kind import/recheck coverage; partial is never reported complete."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class GenerationStatus(StrEnum):
    """Staging → ready lifecycle; failures never publish a half generation."""

    STAGING = "staging"
    READY = "ready"
    FAILED = "failed"


class DocumentStatus(StrEnum):
    """Binding-rule document status; unknown stays background material."""

    CURRENT = "current"
    DRAFT = "draft"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class InboxStatus(StrEnum):
    """Webhook inbox lifecycle (ADR-0015 §5.3)."""

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class SyncRunState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class SyncRunReason(StrEnum):
    """Why a sync run exists; rechecks refresh material only (§5.4)."""

    INITIAL_IMPORT = "initial_import"
    WEBHOOK = "webhook"
    MANUAL_RECHECK = "manual_recheck"
    PERIODIC_RECHECK = "periodic_recheck"
    STARTUP_RECOVERY = "startup_recovery"


class AssertionKind(StrEnum):
    """Where an assertion's truth comes from (ADR-0015 §3)."""

    PLATFORM_FIELD = "platform_field"
    SOURCE_STATEMENT = "source_statement"
    MODEL_EXTRACTED = "model_extracted"


class AssertionValidation(StrEnum):
    """Extraction validation status; extracted ≠ effective (ADR-0015 §3)."""

    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class RelationType(StrEnum):
    """First-version relation vocabulary; candidates never enter the DAG."""

    MENTIONS = "mentions"
    PLATFORM_LINK = "platform_link"
    DOC_REFERENCE = "doc_reference"
    CONFIRMED_MAPPING = "confirmed_mapping"
    IMPLEMENTS = "implements"
    CANDIDATE_DEPENDENCY = "candidate_dependency"


class ConfirmationStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


class BindingEnabledRule(StrEnum):
    """Why a binding (or its sharing scope) turned invalid, if it did."""

    ACTIVE = "active"
    REVOKED = "revoked"


class StopReason(StrEnum):
    """Controller-registered stop reasons; the model cannot reset them (§7.6)."""

    ANSWERED = "answered"
    NEEDS_CLARIFICATION = "needs_clarification"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_PROGRESS = "no_progress"
    ACCESS_DENIED = "access_denied"
    TOOL_DENIED = "tool_denied"
    CONTEXT_CHANGED = "context_changed"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    READ_FAILED = "read_failed"
    MODEL_FAILED = "model_failed"
    INTERRUPTED = "interrupted"


class AnswerStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CLARIFICATION = "clarification"
    UNAVAILABLE = "unavailable"


class ReadSessionStatus(StrEnum):
    """Session lifecycle; interrupted sessions keep their ledger (§7.5)."""

    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


class ReadStepKind(StrEnum):
    """What one recorded session step was (§7.6 trace)."""

    DECISION = "decision"
    TOOL_CALL = "tool_call"
    MODEL_CALL = "model_call"
    RESERVE = "reserve"
    SETTLE = "settle"
    FINALIZE = "finalize"


class ReservationKind(StrEnum):
    MODEL_CALL = "model_call"
    TOOL_BATCH = "tool_batch"
    FINAL_ANSWER = "final_answer"


class ReadModelDecisionKind(StrEnum):
    """The four structured decisions a bounded read loop accepts (§7.3)."""

    LOOKUP = "lookup"
    FINISH = "finish"
    ASK_CLARIFICATION = "ask_clarification"
    REPORT_GAP = "report_gap"


@runtime_checkable
class MonotonicTimer(Protocol):
    """Separately injected monotonic time source for duration budgets (§7.5).

    Wall-clock setbacks must not refund time budgets, so elapsed time never
    comes from the Clock; tests advance a fake timer instead.
    """

    def now(self) -> float:
        """Returns the current monotonic reading in seconds."""
        ...


class SystemTimer:
    """Production timer backed by ``time.perf_counter``."""

    def now(self) -> float:
        import time

        return time.perf_counter()


class FakeTimer:
    """Deterministic timer whose reading only moves when a caller moves it."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def set(self, value: float) -> float:
        self._now = float(value)
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now


# --------------------------------------------------------------------- bindings


class IssueSelector(KnowledgeFrozen):
    """Which issues are in scope; both fields set means the union (§2.1)."""

    milestone: str | None = None
    issue_ids: tuple[int, ...] = ()

    def covers(self, issue_number: int, issue_milestone: str | None) -> bool:
        in_milestone = self.milestone is not None and issue_milestone == self.milestone
        in_ids = issue_number in self.issue_ids
        return in_milestone or in_ids


class ProjectSourceBinding(KnowledgeFrozen):
    """Platform binding configuration, one identity per binding_version (§2.2).

    Bindings are admin-managed platform config and are never derived from
    untrusted issue bodies or repository instructions. A new scope or sharing
    change means a new version row; rows are immutable.
    """

    project_id: str
    repository_id: str
    binding_version: int = Field(ge=1)
    installation_id: str | None = None
    branch: str
    path_allowlist: tuple[str, ...] = ()
    issue_selector: IssueSelector = Field(default_factory=IssueSelector)
    explicit_pr_ids: tuple[int, ...] = ()
    maintainers: tuple[str, ...] = ()
    # Member ids allowed to read this project's material; every query rechecks.
    sharing_grants: tuple[str, ...] = ()
    # Question-domain → maintaining document map, e.g. {"delivery": "docs/delivery.md"}.
    authority_rules: dict[str, str] = Field(default_factory=dict)
    access_epoch: int = Field(ge=1)
    enabled: bool = True
    created_at: datetime

    _tz_created = field_validator("created_at")(_require_timezone)

    def allows_path(self, path: str) -> bool:
        normalized = normalize_repo_path(path)
        if not normalized:
            return False
        return any(
            normalized == normalize_repo_path(allowed)
            or normalized.startswith(f"{normalize_repo_path(allowed)}/")
            for allowed in self.path_allowlist
        )

    def allows_document(self, path: str) -> bool:
        """An allowed path that is also a Markdown file (ADR-0015 §2.1).

        Images and other binaries under an allowed folder are not documents;
        reading them would fail UTF-8 decoding and break the whole sync.
        """
        return self.allows_path(path) and is_markdown_path(path)

    def member_can_read(self, member_id: str | None) -> bool:
        return member_id is not None and member_id in self.sharing_grants


# ---------------------------------------------------------------- source objects


class SourceObjectIdentity(KnowledgeFrozen):
    """Stable identity of one source object within a binding version."""

    project_id: str
    binding_version: int
    kind: SourceKind
    external_id: str
    parent_source_id: str | None = None

    def source_id(self) -> str:
        """Deterministic source id: namespace prefix + stable identity hash."""
        material = "|".join(
            (
                self.project_id,
                str(self.binding_version),
                self.kind.value,
                self.external_id,
                self.parent_source_id or "",
            )
        )
        return f"{SOURCE_ID_PREFIX}{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


class SourceObject(KnowledgeFrozen):
    """One tracked GitHub object with its current availability (§4.1)."""

    source_id: str
    project_id: str
    binding_version: int
    kind: SourceKind
    external_id: str
    parent_source_id: str | None = None
    canonical_url: str | None = None
    title: str | None = None
    availability: SourceAvailability = SourceAvailability.AVAILABLE
    unavailable_reason: str | None = None
    latest_revision_id: str | None = None
    latest_change_sequence: int = 0
    source_updated_at: datetime | None = None
    last_observed_at: datetime | None = None
    last_verified_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceRef(KnowledgeFrozen):
    """A citation pinned to one exact revision location (§7.2, §4.2).

    ``snippet`` must be locatable in the referenced revision; the system, not
    the model, generates links from these fields.
    """

    revision_id: str
    source_id: str
    kind: SourceKind
    title: str | None = None
    paragraph: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    snippet: str
    source_version: str | None = None

    @field_validator("snippet")
    @classmethod
    def _snippet_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence snippet must be non-empty")
        return value


# -------------------------------------------------------------------- revisions


class RevisionOutcome(StrEnum):
    """Result of recording one observation (ADR-0015 §4.2)."""

    APPENDED = "appended"
    DUPLICATE = "duplicate"


class SourceRevision(KnowledgeFrozen):
    """One immutable observed version of a source object (§4.1).

    ``change_sequence`` increments atomically only when observable content
    actually changed; re-reading the current version is idempotent, and
    A→B→A keeps three revisions. Observation time never deduplicates.
    """

    revision_id: str
    source_id: str
    project_id: str
    binding_version: int
    access_epoch: int
    kind: SourceKind
    source_version: str
    change_sequence: int = Field(ge=1)
    content_hash: str
    raw_content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_updated_at: datetime | None = None
    observed_at: datetime

    _tz_observed = field_validator("observed_at")(_require_timezone)


# ------------------------------------------------------------------ sync logs


class SyncInboxEntry(KnowledgeFrozen):
    """One webhook delivery, persisted before any acknowledgement (§5.3)."""

    repository_id: str
    delivery_id: str
    event_type: str
    action: str | None = None
    payload_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime
    status: InboxStatus = InboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None
    retry_at: datetime | None = None

    _tz_received = field_validator("received_at")(_require_timezone)
    _tz_retry = field_validator("retry_at")(
        lambda value: _require_timezone(value) if value is not None else value
    )


class SyncCheckpoint(KnowledgeFrozen):
    """Per-source progress of one sync run; cursors are not understanding (§4.1)."""

    run_id: str
    per_source_cursor: dict[str, str] = Field(default_factory=dict)
    coverage: dict[str, str] = Field(default_factory=dict)
    last_success_at: datetime | None = None
    retry_at: datetime | None = None


class SyncRun(KnowledgeFrozen):
    """One material sync run with its candidate manifest and coverage (§4.1)."""

    run_id: str
    project_id: str
    binding_version: int
    access_epoch: int
    reason: SyncRunReason
    state: SyncRunState = SyncRunState.RUNNING
    candidate_manifest: dict[str, Any] = Field(default_factory=dict)
    checkpoint: SyncCheckpoint | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    _tz_created = field_validator("created_at")(_require_timezone)
    _tz_updated = field_validator("updated_at")(_require_timezone)


# ------------------------------------------------------------- derived knowledge


class KnowledgeAssertion(KnowledgeFrozen):
    """One traceable fact with at least one exact source revision (§4.1)."""

    assertion_id: str
    project_id: str
    subject_ref: str
    predicate: str
    value: str
    assertion_kind: AssertionKind
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    extractor_version: str
    validation_status: AssertionValidation = AssertionValidation.PENDING


class KnowledgeRelation(KnowledgeFrozen):
    """One link between sources; candidates never become formal DAG edges."""

    relation_id: str
    project_id: str
    from_ref: str
    relation_type: RelationType
    to_ref: str
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    confirmation_status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED


class KnowledgeGeneration(KnowledgeFrozen):
    """A published-or-staging snapshot of derived knowledge (§4.1)."""

    generation_id: str
    project_id: str
    binding_version: int
    access_epoch: int
    status: GenerationStatus = GenerationStatus.STAGING
    revision_manifest: tuple[str, ...] = Field(default_factory=tuple)
    assertion_refs: tuple[str, ...] = Field(default_factory=tuple)
    relation_refs: tuple[str, ...] = Field(default_factory=tuple)
    summary: str | None = None
    derivation_version: str = "v1"
    coverage: dict[str, str] = Field(default_factory=dict)
    valid: bool = True
    invalidated_reason: str | None = None
    created_at: datetime
    published_at: datetime | None = None

    _tz_created = field_validator("created_at")(_require_timezone)


# ----------------------------------------------------------------- read sessions


class BudgetLimits(KnowledgeFrozen):
    """First-version hard budgets (ADR-0015 §7.5); binding config may tighten."""

    max_exploration_rounds: int = Field(default=4, ge=0, le=4)
    max_tool_calls: int = Field(default=8, ge=0, le=8)
    max_model_requests: int = Field(default=6, ge=0, le=6)
    max_structured_repairs: int = Field(default=1, ge=0, le=1)
    max_input_tokens_per_call: int = Field(default=8192, ge=0, le=8192)
    max_output_tokens_per_call: int = Field(default=1024, ge=0, le=1024)
    max_total_tokens: int = Field(default=32768, ge=0, le=32768)
    max_evidence_fragments: int = Field(default=12, ge=0, le=12)
    max_evidence_chars: int = Field(default=12000, ge=0, le=12000)
    summary_max_chars: int = PROJECT_BRIEF_MAX_CHARS
    total_time_seconds: float = 60.0
    exploration_time_seconds: float = 45.0
    finalize_reserve_seconds: float = 15.0
    max_model_seconds: float = 10.0
    max_tool_seconds: float = 2.0
    max_no_progress_rounds: int = 2
    max_spend_usd: Decimal | None = Field(default=None, gt=0)
    input_usd_per_million: Decimal | None = Field(default=None, ge=0)
    output_usd_per_million: Decimal | None = Field(default=None, ge=0)


class BudgetUsage(KnowledgeFrozen):
    """Cumulative, never-resetting usage across one read session (§7.5).

    ``unknown_token_usage`` marks reserved-but-unconfirmed model spend; unknown
    consumption is never recorded as zero.
    """

    exploration_rounds: int = 0
    tool_calls: int = 0
    model_requests: int = 0
    structured_repairs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_token_usage: bool = False
    evidence_fragments: int = 0
    evidence_chars: int = 0
    elapsed_seconds: float = 0.0
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0)

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, other: "BudgetUsage") -> "BudgetUsage":
        return BudgetUsage(
            exploration_rounds=self.exploration_rounds + other.exploration_rounds,
            tool_calls=self.tool_calls + other.tool_calls,
            model_requests=self.model_requests + other.model_requests,
            structured_repairs=self.structured_repairs + other.structured_repairs,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            unknown_token_usage=self.unknown_token_usage or other.unknown_token_usage,
            evidence_fragments=self.evidence_fragments + other.evidence_fragments,
            evidence_chars=self.evidence_chars + other.evidence_chars,
            elapsed_seconds=self.elapsed_seconds + other.elapsed_seconds,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class BudgetReservation(KnowledgeFrozen):
    """Pre-authorized worst-case spend, persisted before execution (§7.5)."""

    reservation_id: str
    kind: ReservationKind
    model_requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    evidence_fragments: int = 0
    evidence_chars: int = 0
    seconds: float = 0.0
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    created_at: datetime

    _tz_created = field_validator("created_at")(_require_timezone)

    def usage(self, *, unknown: bool = False) -> BudgetUsage:
        return BudgetUsage(
            model_requests=self.model_requests,
            tool_calls=self.tool_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            unknown_token_usage=unknown,
            evidence_fragments=self.evidence_fragments,
            evidence_chars=self.evidence_chars,
            elapsed_seconds=self.seconds,
            cost_usd=self.cost_usd,
        )


class ReadStepRecord(KnowledgeFrozen):
    """One auditable loop step: decision, tool call, reserve, or settle (§7.6).

    Hidden model reasoning is never stored; only a short action summary.
    """

    session_id: str
    seq: int = Field(ge=1)
    kind: ReadStepKind
    tool_name: str | None = None
    params_summary: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] = Field(default_factory=dict)
    reservation: BudgetReservation | None = None
    usage: BudgetUsage | None = None
    outcome: str | None = None
    occurred_at: datetime

    _tz_occurred = field_validator("occurred_at")(_require_timezone)


class ProjectReadSessionSpec(KnowledgeFrozen):
    """Identity fixed at session creation; one message key maps to one session."""

    session_id: str
    project_id: str
    message_key: str
    question: str
    question_hash: str
    requester_id: str
    binding_version: int
    access_epoch: int
    generation_id: str | None = None
    org_state_refs: tuple[str, ...] = ()
    budget_limits: BudgetLimits = Field(default_factory=BudgetLimits)
    created_at: datetime

    _tz_created = field_validator("created_at")(_require_timezone)


class ProjectReadSession(KnowledgeFrozen):
    """Full read-session state including its settled budget ledger (§7.5)."""

    spec: ProjectReadSessionSpec
    status: ReadSessionStatus = ReadSessionStatus.RUNNING
    used_budget: BudgetUsage = Field(default_factory=BudgetUsage)
    reserved_budget: tuple[BudgetReservation, ...] = ()
    stop_reason: StopReason | None = None
    answer_status: AnswerStatus | None = None
    answer_json: str | None = None
    updated_at: datetime

    _tz_updated = field_validator("updated_at")(_require_timezone)


class GroundedStatement(KnowledgeFrozen):
    """One answer fact bound to the evidence that licenses it (§7.2)."""

    text: str
    evidence_refs: tuple[EvidenceRef, ...] = ()


class FreshnessInfo(KnowledgeFrozen):
    """Material water level shown with every answer (§2.1, §7.6)."""

    generation_id: str | None = None
    binding_version: int | None = None
    access_epoch: int | None = None
    last_verified_at: datetime | None = None
    coverage: dict[str, str] = Field(default_factory=dict)
    stale_sources: tuple[str, ...] = ()
    sync_note: str | None = None


class GroundedProjectAnswer(KnowledgeFrozen):
    """Output contract of the project answer generator (§7.2)."""

    read_session_id: str
    generation_id: str | None = None
    statements: tuple[GroundedStatement, ...] = ()
    uncertainties: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    missing_info: tuple[str, ...] = ()
    freshness: FreshnessInfo = Field(default_factory=FreshnessInfo)
    answer_status: AnswerStatus = AnswerStatus.UNAVAILABLE
    stop_reason: StopReason | None = None
    budget_usage: BudgetUsage = Field(default_factory=BudgetUsage)
    rendered_text: str = ""


class ProjectContextPacket(KnowledgeFrozen):
    """Single-request read model combining material and OrgState views (§4.1).

    Read-only by construction: carrying a packet grants no write permission on
    tasks, cases, or approvals (ADR-0015 §6).
    """

    packet_id: str
    read_session_id: str
    round: int = Field(ge=0)
    generation_id: str | None = None
    requester_id: str
    binding_version: int
    access_epoch: int
    org_state_refs: tuple[str, ...] = ()
    brief: str = ""
    relevant_evidence: tuple[EvidenceRef, ...] = ()
    conflicts: tuple[str, ...] = ()
    missing_info: tuple[str, ...] = ()
    freshness: FreshnessInfo = Field(default_factory=FreshnessInfo)
    excluded_evidence: tuple[str, ...] = ()
    created_at: datetime

    _tz_created = field_validator("created_at")(_require_timezone)


def is_source_ref(ref: str) -> bool:
    """True when a reference belongs to the material namespace, not OrgEvent."""
    return ref.startswith((SOURCE_ID_PREFIX, REVISION_ID_PREFIX))

"""SQLAlchemy 2.0 ORM models for event log, snapshots, cases, approvals, and conversations."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy declarative models."""


class EventRecord(Base):
    """Immutable persistent event log record."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("project_id", "event_id", name="uq_project_event_id"),)


class CaseRecord(Base):
    """Persistent CoordinationCase lifecycle record."""

    __tablename__ = "coordination_cases"

    case_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    waiting_for: Mapped[str | None] = mapped_column(String(64), nullable=True)
    waiting_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    round_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRecord(Base):
    """Persistent Human Approval request record."""

    __tablename__ = "approval_requests"

    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    case_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    approver_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StateSnapshotRecord(Base):
    """Persistent OrgState projected snapshot per project."""

    __tablename__ = "state_snapshots"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DialogueSnapshotRecord(Base):
    """Pending clarifications and sync confirmations of one project (ADR-0013)."""

    __tablename__ = "dialogue_snapshots"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SyncSessionRecord(Base):
    """One progress sync session (ADR-0013); ``id`` keeps the start order."""

    __tablename__ = "sync_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# ADR-0015 project-knowledge tables. Independent from the business tables
# above: new tables only, no column changes to existing ones, and material
# identities never enter the event-log namespaces.
# ---------------------------------------------------------------------------


class ProjectSourceBindingRecord(Base):
    """One immutable binding version (ADR-0015 §2.2); scope changes add rows."""

    __tablename__ = "project_source_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_uid: Mapped[str] = mapped_column(String(192), unique=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    repository_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    installation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch: Mapped[str] = mapped_column(String(256), nullable=False)
    path_allowlist_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    issue_selector_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    explicit_pr_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    maintainers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    sharing_grants_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    authority_rules_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    access_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "binding_version", name="uq_project_binding_version"),
    )


class SourceObjectRecord(Base):
    """One tracked GitHub object with availability and latest-observation state."""

    __tablename__ = "source_objects"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    binding_uid: Mapped[str] = mapped_column(String(192), index=True, nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    parent_source_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    canonical_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    availability: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="available"
    )
    unavailable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_revision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_change_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "binding_uid", "kind", "external_id", "parent_source_id", name="uq_source_object"
        ),
    )


class SourceRevisionRecord(Base):
    """One immutable observed version; content identity, never read time (§4.2)."""

    __tablename__ = "source_revisions"

    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    access_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_version: Mapped[str] = mapped_column(String(256), nullable=False)
    change_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "change_sequence", name="uq_source_change_sequence"),
    )


class SyncInboxRecord(Base):
    """Persisted webhook delivery, written before acknowledgement (§5.3)."""

    __tablename__ = "sync_inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("repository_id", "delivery_id", name="uq_repo_delivery"),
    )


class SyncRunRecord(Base):
    """One sync run with its checkpoint, cursors, and coverage (§4.1)."""

    __tablename__ = "sync_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    access_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="running")
    candidate_manifest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    per_source_cursor_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    coverage_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeAssertionRecord(Base):
    """One derived fact pinned to at least one exact revision (§4.1)."""

    __tablename__ = "knowledge_assertions"

    assertion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(192), nullable=False)
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    assertion_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeRelationRecord(Base):
    """One material relation; candidates never become formal DAG edges."""

    __tablename__ = "knowledge_relations"

    relation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    from_ref: Mapped[str] = mapped_column(String(192), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    to_ref: Mapped[str] = mapped_column(String(192), nullable=False)
    evidence_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confirmation_status: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="unconfirmed"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeGenerationRecord(Base):
    """One staging/ready knowledge generation with its revision manifest."""

    __tablename__ = "knowledge_generations"

    generation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    access_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="staging")
    revision_manifest_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    assertion_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    relation_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    derivation_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    coverage_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeGenerationPointerRecord(Base):
    """Current ready generation per project; switched only via verification."""

    __tablename__ = "knowledge_generation_pointers"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    current_generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    switched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectReadSessionRecord(Base):
    """One bounded read session; one message key maps to exactly one row (§7.5)."""

    __tablename__ = "project_read_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    message_key: Mapped[str] = mapped_column(String(192), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    access_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    org_state_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    budget_limits_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    used_budget_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reserved_budget_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="running"
    )
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    answer_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("project_id", "message_key", name="uq_session_message"),)


class ProjectReadStepRecord(Base):
    """One auditable loop step; no hidden model reasoning is stored (§7.6)."""

    __tablename__ = "project_read_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reservation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_read_step_seq"),)


class ProjectContextPacketRecord(Base):
    """One persisted context packet per session round, for audit and replay."""

    __tablename__ = "project_context_packets"

    packet_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    packet_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

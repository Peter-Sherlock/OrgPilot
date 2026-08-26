"""SQLAlchemy 2.0 ORM models for event log, state snapshot, cases, and approvals."""

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

    __table_args__ = (
        UniqueConstraint("project_id", "event_id", name="uq_project_event_id"),
    )


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

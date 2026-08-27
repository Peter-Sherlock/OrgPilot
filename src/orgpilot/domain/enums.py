"""Closed domain vocabularies used by the OrgPilot kernel."""

from enum import StrEnum


class WorkflowStatus(StrEnum):
    """Official workflow status from the task system."""

    TODO = "todo"
    DOING = "doing"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"


class HealthStatus(StrEnum):
    """Operational health inferred from source-backed claims."""

    UNKNOWN = "unknown"
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    DELAYED = "delayed"


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class CommitmentStatus(StrEnum):
    ACTIVE = "active"
    FULFILLED = "fulfilled"
    AT_RISK = "at_risk"
    BROKEN = "broken"
    SUPERSEDED = "superseded"


class CoordinationCaseStatus(StrEnum):
    OPEN = "open"
    WAITING_FOR_RESPONSE = "waiting_for_response"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    ESCALATED = "escalated"


class ActionType(StrEnum):
    ASK_RECOVERY_ESTIMATE = "ask_recovery_estimate"
    ASK_CLARIFICATION = "ask_clarification"
    PROPOSE_RESCHEDULE = "propose_reschedule"
    NOTIFY_GROUP = "notify_group"
    UPDATE_TASK = "update_task"


class CommandStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AgentTerminationReason(StrEnum):
    ALL_RESOLVED = "all_resolved"
    WAITING_RESPONSE = "waiting_response"
    WAITING_APPROVAL = "waiting_approval"
    MAX_ROUNDS = "max_rounds"
    DUPLICATE_BLOCKED = "duplicate_blocked"
    ESCALATED = "escalated"
    NO_ACTION = "no_action"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PolicyDisposition(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class SyncSessionStatus(StrEnum):
    INITIALIZED = "initialized"
    PROBING = "probing"
    CLARIFYING = "clarifying"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    TIMEOUT = "timeout"


class ProbeMemberStatus(StrEnum):
    PENDING = "pending"
    CLARIFYING = "clarifying"
    COLLECTED = "collected"
    TIMEOUT = "timeout"

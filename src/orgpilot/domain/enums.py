"""Closed domain vocabularies used by the P0 kernel."""

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
    RESOLVED = "resolved"


class ActionType(StrEnum):
    ASK_RECOVERY_ESTIMATE = "ask_recovery_estimate"
    ASK_CLARIFICATION = "ask_clarification"
    PROPOSE_RESCHEDULE = "propose_reschedule"
    NOTIFY_GROUP = "notify_group"
    UPDATE_TASK = "update_task"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PolicyDisposition(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"

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
    """Lifecycle of a tracked commitment. AT_RISK/BROKEN are reserved until
    commitment risk tracking is implemented (M3); do not persist them today."""

    ACTIVE = "active"
    FULFILLED = "fulfilled"
    SUPERSEDED = "superseded"


class CoordinationCaseStatus(StrEnum):
    OPEN = "open"
    WAITING_FOR_RESPONSE = "waiting_for_response"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    ESCALATED = "escalated"


class ActionType(StrEnum):
    ASK_RECOVERY_ESTIMATE = "ask_recovery_estimate"
    ASK_CLARIFICATION = "ask_clarification"
    PROPOSE_RESCHEDULE = "propose_reschedule"
    NOTIFY_GROUP = "notify_group"
    UPDATE_TASK = "update_task"
    SEND_DIRECTIVE = "send_directive"


class CommandStatus(StrEnum):
    SUCCESS = "success"
    REJECTED = "rejected"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AgentTerminationReason(StrEnum):
    ALL_RESOLVED = "all_resolved"
    WAITING_RESPONSE = "waiting_response"
    WAITING_APPROVAL = "waiting_approval"
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
    SUPERSEDED = "superseded"


class ProbeMemberStatus(StrEnum):
    PENDING = "pending"
    CLARIFYING = "clarifying"
    COLLECTED = "collected"
    NO_RESPONSE = "no_response"


class MessageIntent(StrEnum):
    """Routing-level intent classes recognized before claim extraction."""

    HEALTH_REPORT = "health_report"
    DIRECTIVE = "directive"
    TASK_CREATE = "task_create"
    TASK_REASSIGN = "task_reassign"
    DEADLINE_CHANGE = "deadline_change"
    QUESTION = "question"
    CHIT_CHAT = "chit_chat"
    UNCERTAIN = "uncertain"


class DirectiveStatus(StrEnum):
    """Lifecycle of a relayed directive from issuer to target member."""

    ISSUED = "issued"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"

"""Domain models for progress sync, clarification, and DAG synthesis."""

from datetime import datetime

from pydantic import BaseModel, Field

from orgpilot.domain.enums import HealthStatus, ProbeMemberStatus, SyncSessionStatus
from orgpilot.extraction.models import ExtractedHealthClaim


class MemberProbeState(BaseModel):
    """Tracks the multi-turn probing lifecycle for a single team member."""

    member_id: str
    display_name: str = ""
    assigned_tasks: list[str] = Field(default_factory=list)
    status: ProbeMemberStatus = ProbeMemberStatus.PENDING
    turns_count: int = 0
    raw_replies: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    extracted_claims: list[ExtractedHealthClaim] = Field(default_factory=list)
    inquiry_sent_at: datetime | None = None
    last_reply_at: datetime | None = None


class TopologicalRiskSummary(BaseModel):
    """Summarizes a specific risk identified via DAG impact propagation."""

    source_task_id: str
    source_task_title: str
    owner_id: str
    owner_name: str
    health_status: HealthStatus
    expected_completion: datetime | None = None
    blocker: str | None = None
    cascading_impact_tasks: list[str] = Field(default_factory=list)
    severity: str = "MEDIUM"  # LOW, MEDIUM, HIGH, CRITICAL


class ExecutiveBriefing(BaseModel):
    """Synthesized executive briefing containing full-spectrum topological project health."""

    session_id: str
    project_id: str
    generated_at: datetime
    initiated_by: str
    total_active_tasks: int
    on_track_count: int
    at_risk_count: int
    delayed_count: int
    critical_path_impact_days: float = 0.0
    member_statuses: list[MemberProbeState] = Field(default_factory=list)
    topological_risks: list[TopologicalRiskSummary] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    summary_text: str = ""


class SyncSession(BaseModel):
    """Aggregates a complete scatter-gather progress synchronization cycle."""

    session_id: str
    project_id: str
    initiated_by: str
    status: SyncSessionStatus = SyncSessionStatus.INITIALIZED
    created_at: datetime
    updated_at: datetime
    member_probes: dict[str, MemberProbeState] = Field(default_factory=dict)
    briefing: ExecutiveBriefing | None = None

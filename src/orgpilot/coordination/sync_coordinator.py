"""Proactive progress sync coordination: probes, convergence, and DAG briefing.

Since 3a.1 the coordinator no longer extracts or judges completeness itself —
member replies flow through the intent pipeline (MessageRouter → guards →
handlers), which persists claims and reports probe outcomes back through
:meth:`ProgressSyncCoordinator.notify_probe_finished`. The coordinator owns
session bookkeeping, timeout, convergence, and briefing synthesis only.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.dependencies.analyzer import DependencyAnalyzer
from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.domain.enums import (
    ActionType,
    CommandStatus,
    HealthStatus,
    ProbeMemberStatus,
    SyncSessionStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import ActionCommand
from orgpilot.domain.sync_models import (
    ExecutiveBriefing,
    MemberProbeState,
    SyncSession,
    TopologicalRiskSummary,
)

if TYPE_CHECKING:
    from orgpilot.agent.loop import CoordinationAgent

logger = logging.getLogger("orgpilot.coordination.sync")


class ProgressSyncCoordinator:
    """Orchestrates scatter-gather probes, convergence, and DAG synthesis."""

    def __init__(
        self,
        agent: "CoordinationAgent",
        adapter: CollaborationAdapter | None = None,
        session_timeout: timedelta = timedelta(hours=24),
        clock: Clock | None = None,
    ) -> None:
        self.agent = agent
        self.adapter = adapter or agent.adapter
        self.analyzer = DependencyAnalyzer()
        self.session_timeout = session_timeout
        self.clock: Clock = clock or SystemClock()
        self._sessions: dict[str, SyncSession] = {}

    def get_session(self, session_id: str) -> SyncSession | None:
        return self._sessions.get(session_id)

    def get_active_session(
        self, project_id: str, now: datetime | None = None
    ) -> SyncSession | None:
        """Returns the latest active session, expiring sessions idle past the timeout.

        ``now`` should carry the current message's occurred_at so the check stays
        deterministic under test; it defaults to the injected clock.
        """
        checked_at = now or self.clock.now()
        active: SyncSession | None = None
        for s in reversed(list(self._sessions.values())):
            if s.project_id != project_id or s.status not in (
                SyncSessionStatus.PROBING,
                SyncSessionStatus.CLARIFYING,
            ):
                continue
            if checked_at - s.updated_at > self.session_timeout:
                self._timeout_session(s, checked_at)
                continue
            if active is None:
                active = s
        return active

    def _timeout_session(self, session: SyncSession, now: datetime) -> None:
        """Ends a stalled session, briefing from whatever has been collected so far."""
        for probe in session.member_probes.values():
            if probe.status is not ProbeMemberStatus.COLLECTED:
                probe.status = ProbeMemberStatus.TIMEOUT
        self.synthesize_and_deliver_briefing(session.session_id)
        session.status = SyncSessionStatus.TIMEOUT
        session.updated_at = now
        unreplied = [
            p.member_id
            for p in session.member_probes.values()
            if p.status is ProbeMemberStatus.TIMEOUT
        ]
        logger.warning(
            "Progress sync session %s timed out; briefing synthesized from collected "
            "replies (unreplied members: %s)",
            session.session_id,
            ", ".join(unreplied) or "none",
        )

    def start_sync_session(
        self,
        project_id: str,
        initiated_by: str,
        custom_intro: str | None = None,
        occurred_at: datetime | None = None,
    ) -> SyncSession:
        """Dispatches 1-on-1 private inquiries to all active task owners across the project.

        ``occurred_at`` anchors the session clock (evaluations replay fixed
        timestamps); it defaults to the wall clock.
        """
        now = occurred_at or self.clock.now()
        session_id = f"sync-{project_id}-{uuid.uuid4().hex[:8]}"

        active_tasks = [
            t
            for t in self.agent.projector.state.tasks.values()
            if t.workflow_status
            in (WorkflowStatus.DOING, WorkflowStatus.TODO, WorkflowStatus.BLOCKED)
        ]

        # Group tasks by owner
        owner_tasks: dict[str, list[str]] = {}
        for t in active_tasks:
            owner_id = t.owner_id or initiated_by
            owner_tasks.setdefault(owner_id, []).append(t.task_id)

        # Fallback if no tasks assigned yet
        if not owner_tasks and initiated_by:
            owner_tasks[initiated_by] = []

        member_probes: dict[str, MemberProbeState] = {}
        failed_probe_member_ids: list[str] = []
        for member_id, task_ids in owner_tasks.items():
            member_name = (
                self.agent.projector.state.members[member_id].display_name
                if member_id in self.agent.projector.state.members
                else member_id
            )
            probe_state = MemberProbeState(
                member_id=member_id,
                display_name=member_name,
                assigned_tasks=task_ids,
                status=ProbeMemberStatus.PENDING,
                inquiry_sent_at=now,
            )
            member_probes[member_id] = probe_state

            # Send outbound inquiry via adapter
            task_titles = [
                self.agent.projector.state.tasks[tid].title
                for tid in task_ids
                if tid in self.agent.projector.state.tasks
            ]
            titles_str = "、".join(task_titles) if task_titles else "当前指派的项目任务"

            inquiry_text = custom_intro or (
                f"Hi {member_name}！项目负责人正在同步当前项目进度。"
                f"请问您负责的【{titles_str}】目前进展如何？是否有遇到阻碍或需要协调的地方？"
            )

            cmd = ActionCommand(
                command_id=f"cmd:probe:{session_id}:{member_id}",
                action_id=f"action:probe:{member_id}",
                action_type=ActionType.ASK_RECOVERY_ESTIMATE,
                targets=[member_id],
                payload={
                    "session_id": session_id,
                    "task_ids": task_ids,
                    "inquiry_text": inquiry_text,
                    "is_sync_probe": True,
                },
                created_at=now,
                idempotency_key=f"idem:probe:{session_id}:{member_id}",
            )
            # A probe that never reached the member must not be reported as sent:
            # the adapter returns FAILED instead of raising (F2), so check both.
            try:
                result = self.adapter.execute(cmd)
            except Exception as exc:
                logger.error("Failed to send inquiry to %s: %s", member_id, exc)
                failed_probe_member_ids.append(member_id)
            else:
                if result.status is not CommandStatus.SUCCESS:
                    logger.warning(
                        "Probe to %s was not delivered (%s): %s",
                        member_id,
                        result.status.value,
                        result.error,
                    )
                    failed_probe_member_ids.append(member_id)

        session = SyncSession(
            session_id=session_id,
            project_id=project_id,
            initiated_by=initiated_by,
            status=SyncSessionStatus.PROBING,
            created_at=now,
            updated_at=now,
            member_probes=member_probes,
            failed_probe_member_ids=failed_probe_member_ids,
        )
        self._sessions[session_id] = session
        logger.info(
            "Started progress sync session %s with %d members", session_id, len(member_probes)
        )
        return session

    def notify_probe_finished(
        self,
        project_id: str,
        member_id: str,
        *,
        complete: bool,
        occurred_at: datetime | None = None,
        note: str | None = None,
    ) -> SyncSession | None:
        """Public hook for the intent pipeline (3a.1): records a probe outcome.

        Marks the member's probe COLLECTED (with an ``incomplete`` flag when the
        answers stayed partial), and converges the session — synthesizing and
        delivering the briefing — once every probe has reached a terminal state.
        Returns the session when the member belonged to one, else None.
        """
        now = occurred_at or self.clock.now()
        session = self.get_active_session(project_id, now=now)
        if session is None:
            return None
        probe = session.member_probes.get(member_id)
        if probe is None or probe.status not in (
            ProbeMemberStatus.PENDING,
            ProbeMemberStatus.CLARIFYING,
        ):
            return None

        probe.status = ProbeMemberStatus.COLLECTED
        probe.last_reply_at = now
        if not complete:
            probe.incomplete = True
            probe.clarification_questions.append(note or "信息不全")
        session.updated_at = now

        finished = (ProbeMemberStatus.COLLECTED, ProbeMemberStatus.TIMEOUT)
        if all(p.status in finished for p in session.member_probes.values()):
            self.synthesize_and_deliver_briefing(session.session_id)
        return session

    def synthesize_and_deliver_briefing(self, session_id: str) -> ExecutiveBriefing | None:
        """Synthesizes all member probe findings with DAG impact analysis and dispatches to PM."""
        session = self.get_session(session_id)
        if not session:
            return None

        now = self.clock.now()
        session.status = SyncSessionStatus.SYNTHESIZING

        tasks = self.agent.projector.state.tasks
        impacts = self.analyzer.impacts(tasks)

        # Calculate metrics
        on_track = sum(1 for t in tasks.values() if t.health_status == HealthStatus.ON_TRACK)
        at_risk = sum(1 for t in tasks.values() if t.health_status == HealthStatus.AT_RISK)
        delayed = sum(1 for t in tasks.values() if t.health_status == HealthStatus.DELAYED)

        # Build topological risk summaries
        risks: list[TopologicalRiskSummary] = []
        for task in tasks.values():
            if (
                task.health_status in (HealthStatus.DELAYED, HealthStatus.AT_RISK)
                or task.workflow_status == WorkflowStatus.BLOCKED
            ):
                impacted = [
                    imp.impacted_task_id for imp in impacts if imp.source_task_id == task.task_id
                ]
                owner_name = (
                    self.agent.projector.state.members[task.owner_id].display_name
                    if task.owner_id and task.owner_id in self.agent.projector.state.members
                    else (task.owner_id or "未指派")
                )
                severity = "CRITICAL" if delayed and impacted else ("HIGH" if delayed else "MEDIUM")

                risks.append(
                    TopologicalRiskSummary(
                        source_task_id=task.task_id,
                        source_task_title=task.title,
                        owner_id=task.owner_id or "unknown",
                        owner_name=owner_name,
                        health_status=task.health_status,
                        expected_completion=task.deadline,
                        blocker=task.task_id,
                        cascading_impact_tasks=impacted,
                        severity=severity,
                    )
                )

        # Generate actionable recommendations
        recommendations: list[str] = []
        timed_out_members = [
            p for p in session.member_probes.values() if p.status is ProbeMemberStatus.TIMEOUT
        ]
        incomplete_members = [
            p for p in session.member_probes.values() if getattr(p, "incomplete", False)
        ]
        if incomplete_members:
            names = "、".join(p.display_name or p.member_id for p in incomplete_members)
            recommendations.insert(
                0,
                f"注意：{names} 的反馈经多轮追问仍不完整（信息不全），"
                "本简报按现有信息生成，建议单独跟进补充。",
            )
        if timed_out_members:
            names = "、".join(p.display_name or p.member_id for p in timed_out_members)
            recommendations.append(
                f"{names} 超时未回复，本简报基于已收集到的信息生成，后续可单独跟进。"
            )
        if delayed > 0:
            recommendations.append(
                f"发现 {delayed} 项延误任务存在下游关键依赖风险，建议优先通过改期卡片完成排期审批。"
            )
        if at_risk > 0:
            recommendations.append(
                f"有 {at_risk} 项任务处于不确定风险状态，建议关注后续技术排查进展。"
            )
        if not risks:
            recommendations.append("全链路任务推进正常，无下游阻碍风险，保持当前交付节奏。")

        summary_text = (
            f"项目全景进度汇总：共 {len(tasks)} 项活跃任务，{on_track} 项正常推进，"
            f"{delayed} 项延误，{at_risk} 项存在潜在风险。"
        )

        briefing = ExecutiveBriefing(
            session_id=session_id,
            project_id=session.project_id,
            generated_at=now,
            initiated_by=session.initiated_by,
            total_active_tasks=len(tasks),
            on_track_count=on_track,
            at_risk_count=at_risk,
            delayed_count=delayed,
            critical_path_impact_days=1.5 if delayed > 0 else 0.0,
            member_statuses=list(session.member_probes.values()),
            topological_risks=risks,
            recommended_actions=recommendations,
            summary_text=summary_text,
        )

        session.briefing = briefing
        session.status = SyncSessionStatus.COMPLETED
        session.updated_at = now

        # Notify PM via adapter
        notify_cmd = ActionCommand(
            command_id=f"cmd:briefing:{session_id}",
            action_id=f"action:briefing:{session_id}",
            action_type=ActionType.NOTIFY_GROUP,
            targets=[session.initiated_by],
            payload={
                "session_id": session_id,
                "briefing": briefing.model_dump(mode="json"),
                "is_executive_briefing": True,
            },
            created_at=now,
            idempotency_key=f"idem:briefing:{session_id}",
        )
        try:
            self.adapter.execute(notify_cmd)
        except Exception as exc:
            logger.error("Failed to dispatch briefing to PM: %s", exc)

        logger.info(
            "Successfully synthesized and delivered executive briefing for session %s", session_id
        )
        return briefing

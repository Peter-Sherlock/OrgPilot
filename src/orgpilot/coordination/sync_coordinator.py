"""Proactive progress sync and multi-turn clarification coordinator."""

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.dependencies.analyzer import DependencyAnalyzer
from orgpilot.domain.enums import (
    ActionType,
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
from orgpilot.extraction.clarification import SlotCompletenessEvaluator
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import MessageContext

if TYPE_CHECKING:
    from orgpilot.agent.loop import CoordinationAgent

logger = logging.getLogger("orgpilot.coordination.sync")


class ProgressSyncCoordinator:
    """Orchestrates scatter-gather inquiries, clarification loops, and DAG synthesis."""

    def __init__(
        self,
        agent: "CoordinationAgent",
        adapter: CollaborationAdapter | None = None,
        extractor: ClaimExtractor | None = None,
        max_clarification_turns: int = 2,
    ) -> None:
        self.agent = agent
        self.adapter = adapter or agent.adapter
        self.extractor = extractor or ClaimExtractor()
        self.analyzer = DependencyAnalyzer()
        self.max_clarification_turns = max_clarification_turns
        self._sessions: dict[str, SyncSession] = {}

    def get_session(self, session_id: str) -> SyncSession | None:
        return self._sessions.get(session_id)

    def restore_sessions(self, sessions: list[SyncSession]) -> None:
        """Reloads sessions persisted by an earlier gateway run (restart recovery)."""
        for persisted in sessions:
            self._sessions[persisted.session_id] = persisted

    def get_active_session(self, project_id: str) -> SyncSession | None:
        for s in reversed(list(self._sessions.values())):
            if s.project_id == project_id and s.status in (
                SyncSessionStatus.PROBING,
                SyncSessionStatus.CLARIFYING,
            ):
                return s
        return None

    def start_sync_session(
        self,
        project_id: str,
        initiated_by: str,
        custom_intro: str | None = None,
    ) -> SyncSession:
        """Dispatches 1-on-1 private inquiries to all active task owners across the project."""
        now = datetime.now(UTC)
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
            try:
                self.adapter.execute(cmd)
            except Exception as exc:
                logger.error("Failed to send inquiry to %s: %s", member_id, exc)

        session = SyncSession(
            session_id=session_id,
            project_id=project_id,
            initiated_by=initiated_by,
            status=SyncSessionStatus.PROBING,
            created_at=now,
            updated_at=now,
            member_probes=member_probes,
        )
        self._sessions[session_id] = session
        logger.info(
            "Started progress sync session %s with %d members", session_id, len(member_probes)
        )
        return session

    def handle_member_reply(
        self,
        session_id: str,
        member_id: str,
        message: str,
        occurred_at: datetime | None = None,
    ) -> tuple[bool, str | None]:
        """Handles reply from a probed member, driving clarification if needed."""
        session = self.get_session(session_id)
        if not session:
            logger.warning("Session %s not found for member %s reply", session_id, member_id)
            return True, None

        now = occurred_at or datetime.now(UTC)
        session.updated_at = now

        probe = session.member_probes.get(member_id)
        if not probe:
            probe = MemberProbeState(
                member_id=member_id,
                display_name=member_id,
                status=ProbeMemberStatus.PENDING,
            )
            session.member_probes[member_id] = probe

        probe.raw_replies.append(message)
        probe.turns_count += 1
        probe.last_reply_at = now

        # Run extraction scoped to member's assigned tasks
        member_tasks = {
            tid: self.agent.projector.state.tasks[tid].title
            for tid in probe.assigned_tasks
            if tid in self.agent.projector.state.tasks
        }
        known_tasks = member_tasks or {
            t.task_id: t.title for t in self.agent.projector.state.tasks.values()
        }

        context = MessageContext(
            project_id=session.project_id,
            actor_id=member_id,
            occurred_at=now,
            known_tasks=known_tasks,
            known_members={
                m.member_id: m.role for m in self.agent.projector.state.members.values()
            },
            conversation_history=probe.raw_replies[:-1],
        )
        result, events = self.extractor.extract_from_message(message, context)

        if result.claims:
            probe.extracted_claims.extend(result.claims)

        # Ingest extracted events into AgentLoop state
        if events:
            self.agent.run_turn(events, now)

        # Evaluate slot completeness
        is_complete, missing_slots = SlotCompletenessEvaluator.evaluate_completeness(
            result, message
        )

        # Autonomous follow-up clarification check
        if not is_complete and probe.turns_count <= self.max_clarification_turns:
            probe.status = ProbeMemberStatus.CLARIFYING
            session.status = SyncSessionStatus.CLARIFYING

            task_title = (
                self.agent.projector.state.tasks[probe.assigned_tasks[0]].title
                if probe.assigned_tasks
                and probe.assigned_tasks[0] in self.agent.projector.state.tasks
                else "当前任务"
            )
            question = SlotCompletenessEvaluator.generate_clarification_question(
                task_title=task_title,
                missing_slots=missing_slots,
                raw_reply=message,
            )
            probe.clarification_questions.append(question)

            clarify_cmd = ActionCommand(
                command_id=f"cmd:clarify:{session_id}:{member_id}:t{probe.turns_count}",
                action_id=f"action:clarify:{member_id}",
                action_type=ActionType.ASK_CLARIFICATION,
                targets=[member_id],
                payload={
                    "session_id": session_id,
                    "clarification_text": question,
                    "is_clarification": True,
                },
                created_at=now,
                idempotency_key=f"idem:clarify:{session_id}:{member_id}:t{probe.turns_count}",
            )
            try:
                self.adapter.execute(clarify_cmd)
            except Exception as exc:
                logger.error("Failed to send clarification to %s: %s", member_id, exc)

            return False, question

        # Marked collected
        probe.status = ProbeMemberStatus.COLLECTED

        # Check if all members have converged
        if all(p.status == ProbeMemberStatus.COLLECTED for p in session.member_probes.values()):
            self.synthesize_and_deliver_briefing(session_id)

        return True, None

    def synthesize_and_deliver_briefing(self, session_id: str) -> ExecutiveBriefing | None:
        """Synthesizes all member probe findings with DAG impact analysis and dispatches to PM."""
        session = self.get_session(session_id)
        if not session:
            return None

        now = datetime.now(UTC)
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

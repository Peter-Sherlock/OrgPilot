"""ProjectReadOrchestrator: the bounded multi-round lookup loop (ADR-0015 §7.3-7.7).

The controller — never the generator — owns the budget ledger, the timer,
tool whitelisting (delegated to :class:`~orgpilot.knowledge.read_tools.ReadTools`),
evidence identity, progress detection, stop reasons, and final persistence.
Frozen behaviors:

- One message key maps to one session: redeliveries reuse the stored result;
  a crash-left ``running`` session is marked interrupted and replays its
  template, never a fresh budget (§7.5).
- Hard budgets: 4 exploration rounds, 8 tool calls (cache hits, pagination,
  and failures included), 6 model requests (final consolidation and at most
  one structured repair included), 32,768 cumulative tokens (estimated with
  a documented estimator offline), 12 evidence fragments / 12,000 chars,
  60 s wall with the last 15 s reserved for consolidation, 10 s per model
  call, 2 s per tool call. Reservations precede execution; unknown results
  keep their reservation instead of zeroing.
- Two consecutive decision rounds without a NEW evidence fragment stop the
  loop with ``no_progress``; exploration never runs past its time reserve.
- ``finish`` answers are evidence-validated (revision in the generation
  manifest, snippet locatable in the revision content) before sending; an
  invalid answer gets exactly one structured repair, then the deterministic
  gap template cites only what was actually retrieved.
- Before anything is sent, authorization and source availability are
  re-checked; a mid-session revocation drops generated content and answers
  ``access_denied`` (§7.6).
- Sessions, steps, budgets, and answers persist; no OrgEvent, case, or
  approval is touched, and no coordination turn runs.
"""

import asyncio
import inspect
import logging
import weakref

from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.knowledge.access import check_binding, check_generation
from orgpilot.knowledge.answerer import (
    ProjectAnswerDecision,
    ProjectAnswerGenerator,
    ScriptedAnswerGenerator,
    deterministic_gap_answer,
    render_answer,
)
from orgpilot.knowledge.errors import ReadSessionConflict
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetLimits,
    EvidenceRef,
    GroundedProjectAnswer,
    GroundedStatement,
    ProjectReadSessionSpec,
    ReadSessionStatus,
    SourceAvailability,
    SourceKind,
    StopReason,
    canonical_payload_hash,
    new_session_id,
    question_hash,
)
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore
from orgpilot.storage.read_session_store import SqlReadSessionStore

logger = logging.getLogger(__name__)

QUESTION_MAX_CHARS = 500


class ProjectReadOrchestrator:
    """Runs one bounded read session per question, deterministically bounded."""

    def __init__(
        self,
        sources: SqlProjectSourceStore,
        knowledge_store: SqlKnowledgeStore,
        read_sessions: SqlReadSessionStore,
        clock: Clock | None = None,
        timer=None,
        generator: ProjectAnswerGenerator | None = None,
        limits: BudgetLimits | None = None,
        org_state_provider=None,
    ) -> None:
        self.sources = sources
        self.knowledge = knowledge_store
        self.read_sessions = read_sessions
        self.clock: Clock = clock or SystemClock()
        from orgpilot.knowledge.models import SystemTimer

        self.timer = timer or SystemTimer()
        self.generator = generator or ScriptedAnswerGenerator()
        self.limits = limits or BudgetLimits()
        # Returns the read-only OrgState snapshot for a project, or None.
        self.org_state_provider = org_state_provider
        self._locks = weakref.WeakValueDictionary()

    # ------------------------------------------------------------------ entry

    async def run(self, project_id, requester_id, message_key, question, occurred_at=None):
        key = (project_id, message_key)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        async with lock:
            return await self._run_request(
                project_id,
                requester_id,
                message_key,
                question,
                occurred_at,
            )

    async def _run_request(
        self,
        project_id: str,
        requester_id: str,
        message_key: str,
        question: str,
        occurred_at=None,
    ) -> GroundedProjectAnswer:
        """Executes (or replays) one read session for one message."""
        question = question.strip()[:QUESTION_MAX_CHARS]
        existing = await self.read_sessions.get_by_message(project_id, message_key)
        if existing is not None:
            if (
                existing.spec.requester_id != requester_id
                or existing.spec.question_hash != question_hash(question)
            ):
                raise ReadSessionConflict("message identity belongs to another request")
            binding = await self.sources.get_binding(project_id, existing.spec.binding_version)
            if binding is None or await check_binding(self.sources, binding, requester_id):
                return _session_gap(
                    existing, StopReason.ACCESS_DENIED, question, "当前资料授权已失效。"
                )
            problem = await self._context_problem(existing.spec, _answer_task_ids(existing))
            if problem:
                return _session_gap(existing, problem, question, "项目上下文已变化，请重新提问。")
            return await self._replay(existing.spec.session_id, project_id, question)
        session_id = new_session_id()
        now = occurred_at or self.clock.now()
        binding = await self.sources.get_active_binding(project_id)
        binding_version = binding.binding_version if binding else 0
        access_epoch = binding.access_epoch if binding else 0
        generation_id = None
        if binding is not None:
            generation_id = await self.knowledge.current_generation_id(project_id)
        state = (
            await self._org_state(project_id)
            if binding and binding.member_can_read(requester_id)
            else None
        )
        spec = ProjectReadSessionSpec(
            session_id=session_id,
            project_id=project_id,
            message_key=message_key,
            question=question,
            question_hash=question_hash(question),
            requester_id=requester_id,
            binding_version=binding_version,
            access_epoch=access_epoch,
            generation_id=generation_id,
            org_state_refs=_state_refs(state),
            budget_limits=self.limits,
            created_at=now,
        )
        await self.read_sessions.create_session(spec)
        start = self.timer.now()
        try:
            return await self._run_inner(spec, binding, generation_id, start, state)
        except asyncio.CancelledError:
            await asyncio.shield(self.read_sessions.mark_interrupted(session_id))
            raise
        except Exception:
            # A crash mid-session keeps the ledger and never reruns itself.
            logger.exception("project read session %s crashed", session_id)
            await self.read_sessions.mark_interrupted(session_id)
            session = await self.read_sessions.get_session(session_id)
            return _session_gap(
                session, StopReason.INTERRUPTED, question, "本次查询被中断；同消息不会重跑。"
            )

    async def _replay(
        self, session_id: str, project_id: str, question: str
    ) -> GroundedProjectAnswer:
        """Same message → same session and result (§7.7)."""
        session = await self.read_sessions.get_session(session_id)
        if session is None:  # pragma: no cover - store guarantees the row
            return deterministic_gap_answer(
                session_id, StopReason.INTERRUPTED, question, (), "查询状态丢失。"
            )
        if session.status is ReadSessionStatus.RUNNING:
            await self.read_sessions.mark_interrupted(session_id)
        if session.status in (ReadSessionStatus.INTERRUPTED, ReadSessionStatus.RUNNING):
            return _session_gap(
                session,
                StopReason.INTERRUPTED,
                question,
                "该查询此前被中断；同一消息不会重新消耗预算。",
            )
        if session.answer_json:
            import json

            answer = GroundedProjectAnswer.model_validate(json.loads(session.answer_json))
            binding = await self.sources.get_binding(project_id, session.spec.binding_version)
            if binding is None or await self._revoked_since_start(
                binding, session.spec.requester_id, answer
            ):
                return deterministic_gap_answer(
                    session_id, StopReason.ACCESS_DENIED, question, (), "当前资料授权已失效。"
                )
            if not answer.rendered_text:
                answer = answer.model_copy(
                    update={"rendered_text": render_answer(answer, question)}
                )
            return answer
        return deterministic_gap_answer(
            session_id,
            session.stop_reason or StopReason.INTERRUPTED,
            question,
            (),
            "该查询没有生成答复。",
        )

    # ------------------------------------------------------------------ engine

    async def _run_inner(self, spec, binding, generation_id, start, org_state=None):
        from orgpilot.knowledge.read_engine import ReadEngine

        return await ReadEngine(self, spec, binding, generation_id, start, org_state).run()

    async def _org_state(self, project_id):
        if self.org_state_provider is None:
            return None
        state = self.org_state_provider(project_id)
        state = await state if inspect.isawaitable(state) else state
        return state.model_copy(deep=True) if state is not None else None

    async def _context_problem(self, spec, task_ids=()):
        """Re-checks binding, grants and the pinned generation; then formal state.

        Formal state is compared only for ``task_ids``: the tasks this session
        actually read or cited. A change elsewhere in the project (another
        member's message, a new claim) does not touch what the answer relies
        on, so it no longer discards the answer (ADR-0015 §7.6, read-set check).
        With no tasks used, no event replay is needed at all.
        """
        binding = await self.sources.get_binding(spec.project_id, spec.binding_version)
        if binding is None:
            return StopReason.ACCESS_DENIED
        problem = await check_binding(self.sources, binding, spec.requester_id)
        if problem:
            return problem
        generation = (
            await self.knowledge.get_generation(spec.generation_id)
            if (spec.generation_id)
            else None
        )
        if generation is None:
            return StopReason.CONTEXT_UNAVAILABLE
        if (generation.binding_version, generation.access_epoch) != (
            spec.binding_version,
            spec.access_epoch,
        ):
            return StopReason.CONTEXT_CHANGED
        problem = await check_generation(self.sources, self.knowledge, generation)
        if problem:
            return problem
        used = tuple(dict.fromkeys(task_ids))
        if used:
            pinned = _pinned_tasks(spec.org_state_refs)
            current = _pinned_tasks(_state_refs(await self._org_state(spec.project_id)))
            if any(pinned.get(task_id) != current.get(task_id) for task_id in used):
                return StopReason.CONTEXT_CHANGED
        return None

    async def _assemble_valid_finish(
        self, spec, decision: ProjectAnswerDecision, evidence: dict[str, EvidenceRef]
    ):
        if decision.answer_status is AnswerStatus.CLARIFICATION:
            question = decision.clarification_question or (
                decision.statements[0].text if decision.statements else ""
            )
            answer = GroundedProjectAnswer(
                read_session_id=spec.session_id,
                statements=(),
                uncertainties=(question,),
                answer_status=AnswerStatus.CLARIFICATION,
            )
            return answer.model_copy(update={"rendered_text": render_answer(answer, spec.question)})
        return await _assemble_answer(spec, decision, evidence, self.sources)

    async def _revoked_since_start(
        self, binding, requester_id: str, answer: GroundedProjectAnswer
    ) -> bool:
        """True when authorization or a cited source became unavailable."""
        if await check_binding(self.sources, binding, requester_id):
            return True
        for statement in answer.statements:
            for ref in statement.evidence_refs:
                if ref.source_id.startswith("task:"):
                    continue
                obj = await self.sources.get_source_object(ref.source_id)
                if obj is None or obj.availability is not SourceAvailability.AVAILABLE:
                    return True
        return False


def _validate_statements(
    statements: tuple[GroundedStatement, ...],
    evidence: dict[str, EvidenceRef],
    generation,
) -> list[str]:
    """Accept only identities, locations and quote text actually returned."""
    problems: list[str] = []
    for index, statement in enumerate(statements, start=1):
        if not statement.text.strip():
            problems.append(f"第 {index} 条陈述为空")
            continue
        if not statement.evidence_refs:
            problems.append(f"第 {index} 条陈述缺少证据引用")
            continue
        for ref in statement.evidence_refs:
            if (
                ref.revision_id not in generation.revision_manifest
                and not ref.source_id.startswith("task:")
            ) or _returned_reference(ref, evidence) is None:
                problems.append(f"第 {index} 条陈述的来源、位置或原文不属于已返回证据")
            if not ref.snippet.strip():
                problems.append(f"第 {index} 条陈述的证据缺少原文片段")
    return problems


def _session_gap(session, reason, question, note):
    usage = session.used_budget
    for reservation in session.reserved_budget:
        usage = usage.plus(reservation.usage(unknown=True))
    return deterministic_gap_answer(
        session.spec.session_id,
        reason,
        question,
        (),
        note,
    ).model_copy(update={"budget_usage": usage})


TASK_REF_PREFIX = "task:"


def _state_refs(state):
    """Per-task fingerprints of the pinned formal state: ``task:<id>:<hash>``."""
    if state is None:
        return ()
    return tuple(
        f"{TASK_REF_PREFIX}{task_id}:"
        f"{canonical_payload_hash(state.tasks[task_id].model_dump(mode='json'))}"
        for task_id in sorted(state.tasks)
    )


def _pinned_tasks(refs) -> dict[str, str]:
    pinned: dict[str, str] = {}
    for ref in refs:
        if ref.startswith(TASK_REF_PREFIX):
            task_id, _, fingerprint = ref[len(TASK_REF_PREFIX) :].rpartition(":")
            pinned[task_id] = fingerprint
    return pinned


def _task_ids_in(refs) -> tuple[str, ...]:
    """Task ids behind ``task:<id>`` evidence sources, in first-seen order."""
    ids = (
        ref.source_id[len(TASK_REF_PREFIX) :]
        for ref in refs
        if ref.source_id.startswith(TASK_REF_PREFIX)
    )
    return tuple(dict.fromkeys(ids))


def _answer_task_ids(session) -> tuple[str, ...]:
    """Tasks a stored answer cites; a replay re-checks only those."""
    if not session.answer_json:
        return ()
    answer = GroundedProjectAnswer.model_validate_json(session.answer_json)
    return _task_ids_in(ref for statement in answer.statements for ref in statement.evidence_refs)


def _returned_reference(ref: EvidenceRef, evidence: dict[str, EvidenceRef]) -> EvidenceRef | None:
    """The returned evidence a citation points at, rebuilt server-side.

    Identity (revision + source + kind), the quote, and any stated location
    must match returned evidence; a forged location is still rejected. Two
    spellings mean "no location claimed": an empty string, and the evidence's
    own title (the old prompt asked for "paragraph or title").
    """
    quote = " ".join(ref.snippet.split())
    if not quote:
        return None
    for known in evidence.values():
        if (known.revision_id, known.source_id, known.kind) != (
            ref.revision_id,
            ref.source_id,
            ref.kind,
        ):
            continue
        paragraph = ref.paragraph.strip() if ref.paragraph else None
        if paragraph and paragraph != known.paragraph and paragraph == (known.title or "").strip():
            paragraph = None
        if paragraph is not None and paragraph != known.paragraph:
            continue
        if any(
            getattr(ref, field) is not None and getattr(ref, field) != getattr(known, field)
            for field in ("line_start", "line_end")
        ):
            continue
        if quote in " ".join(known.snippet.split()):
            return known.model_copy(update={"snippet": ref.snippet})
    return None


async def _assemble_answer(
    spec,
    decision: ProjectAnswerDecision,
    evidence: dict[str, EvidenceRef],
    sources: SqlProjectSourceStore,
) -> GroundedProjectAnswer:
    """Assembles the answer; work-item statements gain their cited revision's
    deterministic metadata facts (state / merged / review author), so platform
    fields are quotable without any model invention."""
    statements = [
        statement.model_copy(
            update={
                "evidence_refs": tuple(
                    _returned_reference(ref, evidence) for ref in statement.evidence_refs
                ),
            }
        )
        for statement in decision.statements
    ]
    for index, statement in enumerate(statements):
        for ref in statement.evidence_refs:
            note = await _work_item_note(sources, ref)
            if note and note not in statements[index].text:
                statements[index] = statement.model_copy(
                    update={"text": f"{statement.text}（{note}）"}
                )
    answer = GroundedProjectAnswer(
        read_session_id=spec.session_id,
        statements=tuple(statements),
        uncertainties=decision.uncertainties,
        conflicts=decision.conflicts,
        missing_info=decision.missing_info,
        answer_status=decision.answer_status,
    )
    return answer.model_copy(update={"rendered_text": render_answer(answer, spec.question)})


async def _work_item_note(sources: SqlProjectSourceStore, ref: EvidenceRef) -> str | None:
    """Deterministic facts from the cited revision's stored metadata."""
    if ref.source_id.startswith("task:"):
        return "正式任务状态"
    revision = await sources.get_revision(ref.revision_id)
    if revision is None:
        return None
    metadata = revision.metadata or {}
    if ref.kind is SourceKind.DOCUMENT:
        obj = await sources.get_source_object(ref.source_id)
        if obj is not None and obj.external_id:
            return f"来源文档：{obj.external_id}"
        return None
    if ref.kind is SourceKind.ISSUE:
        parts = [f"工作记录 issue：状态={metadata.get('state', 'unknown')}"]
        if metadata.get("assignees"):
            parts.append(f"负责人={','.join(metadata['assignees'])}")
        return "、".join(parts)
    if ref.kind is SourceKind.PULL_REQUEST:
        merged = "已合并" if metadata.get("merged") else "未合并"
        return f"工作记录 PR：状态={metadata.get('state', 'unknown')}、{merged}"
    if ref.kind is SourceKind.REVIEW:
        return (
            f"工作记录 review：{metadata.get('author', 'unknown')} 提交 {metadata.get('state', '')}"
        )
    return None


def _kind_from_tool(item: dict):
    from orgpilot.knowledge.models import SourceKind

    value = item.get("kind")
    if value is None:
        return SourceKind.DOCUMENT
    if isinstance(value, SourceKind):
        return value
    try:
        return SourceKind(str(value))
    except ValueError:
        return SourceKind.DOCUMENT


def _item_for_key(tool_result, key: str) -> dict:
    """Finds the result item matching an evidence key produced by the tool."""
    revision_id = key.split("|", 1)[0]
    for item in tool_result.items:
        if item.get("revision_id") == revision_id:
            return item
    return dict(tool_result.items[0]) if tool_result.items else {}

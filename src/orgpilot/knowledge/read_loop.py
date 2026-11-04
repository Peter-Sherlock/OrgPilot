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
import contextlib
import inspect
import logging
from dataclasses import dataclass

from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.domain.models import OrgState
from orgpilot.knowledge.access import check_binding, check_generation
from orgpilot.knowledge.answerer import (
    TOKEN_ESTIMATOR_VERSION,
    DecisionValidationError,
    ProjectAnswerDecision,
    ProjectAnswerGenerator,
    ReadLoopView,
    ScriptedAnswerGenerator,
    deterministic_gap_answer,
    estimate_tokens,
    render_answer,
)
from orgpilot.knowledge.context import ProjectContextBuilder
from orgpilot.knowledge.errors import KnowledgeContractError, ReadSessionConflict
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetLimits,
    BudgetReservation,
    BudgetUsage,
    EvidenceRef,
    FreshnessInfo,
    GroundedProjectAnswer,
    GroundedStatement,
    ProjectReadSessionSpec,
    ReadSessionStatus,
    ReadStepKind,
    ReadStepRecord,
    ReservationKind,
    SourceAvailability,
    SourceKind,
    StopReason,
    canonical_payload_hash,
    new_session_id,
    question_hash,
)
from orgpilot.knowledge.read_tools import ReadTools, ReadToolSession
from orgpilot.knowledge.retrieval import MaterialIndex
from orgpilot.storage.knowledge_store import SqlKnowledgeStore
from orgpilot.storage.project_source_store import SqlProjectSourceStore
from orgpilot.storage.read_session_store import SqlReadSessionStore

logger = logging.getLogger(__name__)

QUESTION_MAX_CHARS = 500


@dataclass
class _Ledger:
    """Mutable per-session accumulator; freezes into BudgetUsage for storage."""

    exploration_rounds: int = 0
    tool_calls: int = 0
    model_requests: int = 0
    structured_repairs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_token_usage: bool = False
    evidence_fragments: int = 0
    evidence_chars: int = 0

    def snapshot(self) -> BudgetUsage:
        return BudgetUsage(
            exploration_rounds=self.exploration_rounds,
            tool_calls=self.tool_calls,
            model_requests=self.model_requests,
            structured_repairs=self.structured_repairs,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            unknown_token_usage=self.unknown_token_usage,
            evidence_fragments=self.evidence_fragments,
            evidence_chars=self.evidence_chars,
        )

    def add(self, usage: BudgetUsage) -> None:
        self.model_requests += usage.model_requests
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.unknown_token_usage = self.unknown_token_usage or usage.unknown_token_usage


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

    # ------------------------------------------------------------------ entry

    async def run(
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
                return deterministic_gap_answer(
                    existing.spec.session_id,
                    StopReason.ACCESS_DENIED,
                    question,
                    (),
                    "当前资料授权已失效。",
                )
            problem = await self._context_problem(existing.spec)
            if problem:
                return deterministic_gap_answer(
                    existing.spec.session_id,
                    problem,
                    question,
                    (),
                    "项目上下文已变化，请重新提问。",
                )
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
        ledger = _Ledger()
        try:
            return await self._run_inner(spec, binding, generation_id, start, ledger, state)
        except Exception:
            # A crash mid-session keeps the ledger and never reruns itself.
            logger.exception("project read session %s crashed", session_id)
            await self.read_sessions.mark_interrupted(session_id)
            return deterministic_gap_answer(
                session_id,
                StopReason.INTERRUPTED,
                question,
                (),
                "本次查询被中断；重新提问将产生新的查询。",
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
        if session.status is ReadSessionStatus.INTERRUPTED:
            return deterministic_gap_answer(
                session_id,
                StopReason.INTERRUPTED,
                question,
                (),
                "该查询此前被中断；同一消息不会重新消耗预算，请换一条新消息重试。",
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

    async def _run_inner(
        self,
        spec: ProjectReadSessionSpec,
        binding,
        generation_id: str | None,
        start: float,
        ledger: _Ledger,
        org_state: OrgState | None = None,
    ) -> GroundedProjectAnswer:
        question = spec.question
        seq = 0

        def next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        async def step(kind: ReadStepKind, **fields) -> None:
            await self.read_sessions.append_step(
                ReadStepRecord(
                    session_id=spec.session_id,
                    seq=next_seq(),
                    kind=kind,
                    occurred_at=self.clock.now(),
                    **fields,
                )
            )

        def remaining_budget() -> BudgetUsage:
            # Elapsed time is tracked separately through the monotonic timer.
            return BudgetUsage(
                exploration_rounds=max(
                    0, self.limits.max_exploration_rounds - ledger.exploration_rounds
                ),
                tool_calls=max(0, self.limits.max_tool_calls - ledger.tool_calls),
                model_requests=max(0, self.limits.max_model_requests - ledger.model_requests),
                input_tokens=max(
                    0,
                    self.limits.max_total_tokens - ledger.input_tokens - ledger.output_tokens,
                ),
                evidence_fragments=max(
                    0, self.limits.max_evidence_fragments - ledger.evidence_fragments
                ),
                evidence_chars=max(0, self.limits.max_evidence_chars - ledger.evidence_chars),
            )

        def limits_summary() -> str:
            remaining = remaining_budget()
            elapsed = self.timer.now() - start
            return (
                f"轮次{remaining.exploration_rounds}/工具{remaining.tool_calls}"
                f"/模型{remaining.model_requests}/tokens{remaining.input_tokens}"
                f"/片段{remaining.evidence_fragments}/已用时{elapsed:.0f}s"
            )

        async def mark_unknown_result(note: str) -> None:
            # Unknown model results keep their reservation, never zeroed (§7.5).
            await self._settle_unknown(spec.session_id, reservation_id_holder[0])
            ledger.model_requests += 1
            ledger.unknown_token_usage = True
            stop_holder[0] = StopReason.MODEL_FAILED
            unresolved.append(note)

        reservation_id_holder: list[str | None] = [None]
        stop_holder: list[StopReason | None] = [None]

        # -- material resolution ------------------------------------------------
        if binding is None:
            return await self._finish_simple(
                spec,
                StopReason.CONTEXT_UNAVAILABLE,
                AnswerStatus.UNAVAILABLE,
                "项目尚未绑定资料仓库。",
                ledger.snapshot(),
                step,
            )
        if not binding.member_can_read(spec.requester_id):
            return await self._finish_simple(
                spec,
                StopReason.ACCESS_DENIED,
                AnswerStatus.UNAVAILABLE,
                "你没有该项目资料的访问授权。",
                ledger.snapshot(),
                step,
            )
        generation = None
        if generation_id:
            generation = await self.knowledge.get_generation(generation_id)
        if generation is None or not generation.valid:
            return await self._finish_simple(
                spec,
                StopReason.CONTEXT_UNAVAILABLE,
                AnswerStatus.UNAVAILABLE,
                "项目资料还在生成或已失效，请稍后重试。",
                ledger.snapshot(),
                step,
            )

        # -- round-0 packet: initial retrieval counts against the budget (§7.3)
        builder = ProjectContextBuilder(self.clock)
        objects = await self.sources.list_source_objects(spec.project_id, binding.binding_version)
        revisions = {}
        for revision_id in generation.revision_manifest:
            revision = await self.sources.get_revision(revision_id)
            if revision is not None:
                revisions[revision.source_id] = revision
        index = MaterialIndex.from_generation_material(objects, revisions)
        try:
            packet = builder.build_packet(
                read_session_id=spec.session_id,
                requester_id=spec.requester_id,
                binding=binding,
                generation=generation,
                index=index,
                objects=objects,
                question=question,
                org_state=org_state,
            )
        except PermissionError:
            return await self._finish_simple(
                spec,
                StopReason.ACCESS_DENIED,
                AnswerStatus.UNAVAILABLE,
                "你没有该项目资料的访问授权。",
                ledger.snapshot(),
                step,
            )
        await self.read_sessions.save_packet(packet)
        ledger.tool_calls += 1  # initial retrieval counts (§7.3)
        evidence: dict[str, EvidenceRef] = {}
        for ref in packet.relevant_evidence:
            key = f"{ref.revision_id}|{ref.paragraph or ref.title or ''}"
            evidence[key] = ref
            ledger.evidence_fragments += 1
            ledger.evidence_chars += len(ref.snippet)
        tool_session = ReadToolSession(
            session_id=spec.session_id,
            project_id=spec.project_id,
            requester_id=spec.requester_id,
            binding=binding,
            generation=generation,
            index=index,
            org_state=org_state,
            relations=await self.knowledge.list_relations(spec.project_id),
            sources=self.sources,
            clock=self.clock,
            context_check=lambda: self._context_problem(spec),
        )
        tools = ReadTools(tool_session)

        unresolved: list[str] = list(packet.missing_info)
        recent_results: list[dict] = []
        no_progress_rounds = 0
        repairs_used = 0
        decision_answer: GroundedProjectAnswer | None = None
        clarification: str | None = None

        exploration_deadline = start + self.limits.exploration_time_seconds
        total_deadline = start + self.limits.total_time_seconds

        # -- exploration rounds ---------------------------------------------
        while (
            ledger.exploration_rounds < self.limits.max_exploration_rounds
            and stop_holder[0] is None
        ):
            problem = await self._context_problem(spec)
            if problem:
                stop_holder[0] = problem
                break
            if self.timer.now() >= exploration_deadline:
                stop_holder[0] = StopReason.BUDGET_EXHAUSTED
                unresolved.append("探索时间已用尽，停止继续查阅。")
                break
            # Reserve the worst-case model call before asking the generator.
            reservation = BudgetReservation(
                reservation_id=f"{spec.session_id}-m{ledger.model_requests + 1}",
                kind=ReservationKind.MODEL_CALL,
                model_requests=1,
                input_tokens=min(
                    self.limits.max_input_tokens_per_call, remaining_budget().input_tokens
                ),
                output_tokens=self.limits.max_output_tokens_per_call,
                seconds=max(0.0, exploration_deadline - self.timer.now()),
                created_at=self.clock.now(),
            )
            reservation_id_holder[0] = reservation.reservation_id
            try:
                await self.read_sessions.reserve(spec.session_id, reservation)
            except KnowledgeContractError as exc:
                stop_holder[0] = StopReason.BUDGET_EXHAUSTED
                unresolved.append(f"预算预留失败：{exc}")
                break
            await step(
                ReadStepKind.RESERVE,
                reservation=reservation,
                outcome="model_call_reserved",
            )
            view = ReadLoopView(
                round_number=ledger.exploration_rounds + 1,
                question=question,
                brief=packet.brief,
                evidence=tuple(evidence.values()),
                unresolved=tuple(unresolved),
                remaining=remaining_budget(),
                limits_summary=limits_summary(),
                tool_results=tuple(recent_results[-2:]),
            )
            prompt_chars = len(view.to_prompt())
            try:
                decision = await asyncio.wait_for(
                    self.generator.decide(view), timeout=self.limits.max_model_seconds
                )
            except DecisionValidationError as exc:
                decision = await self._repair_or_fail(
                    spec,
                    view,
                    None,
                    str(exc),
                    repairs_used,
                )
                if decision is None:
                    await mark_unknown_result(f"模型输出不合约：{exc}")
                    ledger.model_requests += 1  # the failed attempt itself
                    break
                repairs_used += 1
                ledger.model_requests += 2  # failed attempt + repair
            except TimeoutError:
                await mark_unknown_result("模型请求超时，结果未知。")
                break
            except Exception as exc:  # transport failures never auto-retry
                await mark_unknown_result(f"模型请求失败：{exc}")
                break

            actual = BudgetUsage(
                model_requests=1,
                input_tokens=estimate_tokens(view.to_prompt()),
                output_tokens=estimate_tokens(decision.action_summary or "decision"),
            )
            await self.read_sessions.settle(spec.session_id, reservation.reservation_id, actual)
            await step(
                ReadStepKind.DECISION,
                params_summary={
                    "kind": decision.kind,
                    "tool": decision.tool,
                    "summary": decision.action_summary[:200],
                    "estimator": TOKEN_ESTIMATOR_VERSION,
                    "prompt_chars": prompt_chars,
                },
                usage=actual,
            )
            ledger.add(actual)

            if decision.kind == "finish":
                result, repaired = await self._validate_finish(
                    spec,
                    decision,
                    evidence,
                    generation,
                    repairs_used,
                    step,
                    ledger,
                )
                if repaired:
                    repairs_used += 1
                if result is None:
                    stop_holder[0] = StopReason.MODEL_FAILED
                    unresolved.append("答案引用校验未通过且修复失败。")
                    break
                decision_answer = result
                if result.answer_status is AnswerStatus.CLARIFICATION:
                    clarification = next((u for u in result.uncertainties if u.strip()), None)
                    stop_holder[0] = StopReason.NEEDS_CLARIFICATION
                else:
                    stop_holder[0] = StopReason.ANSWERED
                break
            if decision.kind == "ask_clarification":
                clarification = decision.clarification_question
                stop_holder[0] = StopReason.NEEDS_CLARIFICATION
                break
            if decision.kind == "report_gap":
                unresolved.append(decision.gap or "资料中缺少可回答的依据。")
                stop_holder[0] = StopReason.INSUFFICIENT_EVIDENCE
                break

            # -- lookup ------------------------------------------------------
            if ledger.tool_calls >= self.limits.max_tool_calls:
                stop_holder[0] = StopReason.BUDGET_EXHAUSTED
                unresolved.append("工具调用额度已用尽。")
                break
            ledger.tool_calls += 1
            ledger.exploration_rounds += 1
            tool_result = await asyncio.wait_for(
                tools.call(decision.tool, decision.args),
                timeout=self.limits.max_tool_seconds,
            )
            recent_results.append(tool_result.to_payload())
            await step(
                ReadStepKind.TOOL_CALL,
                tool_name=decision.tool,
                params_summary=decision.args,
                result_summary={
                    "ok": tool_result.ok,
                    "error_code": tool_result.error_code,
                    "cached": tool_result.cached,
                    "truncated": tool_result.truncated,
                    "items": len(tool_result.items),
                },
                outcome=tool_result.error_code
                or ("cache_hit" if tool_result.cached else "executed"),
            )
            new_evidence = False
            relation_hints = 0
            if tool_result.ok:
                for key in tool_result.evidence_keys:
                    if key in evidence:
                        continue
                    item = _item_for_key(tool_result, key)
                    snippet = item.get("snippet") or item.get("content", "")[:200]
                    if not snippet.strip():
                        # Relation lines are progress (confirmed relation refs
                        # count, ADR-0015 §7.4) but never quotable evidence:
                        # a hit title proves nothing about unread content.
                        relation_hints += 1
                        new_evidence = True
                        unresolved.append(
                            f"发现关联线索：{item.get('from_ref', '')} → "
                            f"{item.get('to_ref', '')}（可继续读取原文）"
                        )
                        continue
                    new_evidence = True
                    next_chars = ledger.evidence_chars + len(snippet)
                    if (
                        ledger.evidence_fragments >= self.limits.max_evidence_fragments
                        or next_chars > self.limits.max_evidence_chars
                    ):
                        unresolved.append("证据额度已满，部分返回内容未纳入证据清单。")
                        continue
                    ref = EvidenceRef(
                        revision_id=item.get("revision_id", ""),
                        source_id=item.get("source_id", ""),
                        kind=_kind_from_tool(item),
                        title=item.get("title"),
                        paragraph=item.get("paragraph") or item.get("locator"),
                        line_start=item.get("line_start"),
                        line_end=item.get("line_end"),
                        snippet=snippet,
                    )
                    evidence[key] = ref
                    ledger.evidence_fragments += 1
                    ledger.evidence_chars += len(ref.snippet)
                if not tool_result.items and tool_result.detail:
                    unresolved.append(tool_result.detail)
                if relation_hints:
                    # Follow-up reading happens through the next round's
                    # decisions; the hints alone stay non-evidence.
                    pass
            else:
                unresolved.append(
                    f"{decision.tool} 失败（{tool_result.error_code}）：{tool_result.detail or ''}"
                )
            if not new_evidence:
                no_progress_rounds += 1
            else:
                no_progress_rounds = 0
            if no_progress_rounds >= self.limits.max_no_progress_rounds:
                stop_holder[0] = StopReason.NO_PROGRESS
                unresolved.append("连续查阅没有新增可用证据，停止重复搜索。")

        stop = stop_holder[0]

        # -- final consolidation within the reserved window (§7.5) -----------
        consolidatable = stop in (None, StopReason.BUDGET_EXHAUSTED, StopReason.NO_PROGRESS)
        if decision_answer is None and consolidatable and self.timer.now() < total_deadline:
            reserve_seconds = min(
                self.limits.finalize_reserve_seconds, total_deadline - self.timer.now()
            )
            final_reservation = BudgetReservation(
                reservation_id=f"{spec.session_id}-final",
                kind=ReservationKind.FINAL_ANSWER,
                model_requests=1,
                input_tokens=min(
                    self.limits.max_input_tokens_per_call,
                    max(
                        0,
                        self.limits.max_total_tokens - ledger.input_tokens - ledger.output_tokens,
                    ),
                ),
                output_tokens=self.limits.max_output_tokens_per_call,
                seconds=reserve_seconds,
                created_at=self.clock.now(),
            )
            reserved_ok = True
            try:
                await self.read_sessions.reserve(spec.session_id, final_reservation)
            except KnowledgeContractError:
                reserved_ok = False
            if reserved_ok:
                view = ReadLoopView(
                    round_number=self.limits.max_exploration_rounds + 1,
                    question=question,
                    brief=packet.brief,
                    evidence=tuple(evidence.values()),
                    unresolved=tuple(unresolved),
                    remaining=remaining_budget(),
                    limits_summary=limits_summary(),
                    tool_results=tuple(recent_results[-2:]),
                    allow_tools=False,
                )
                try:
                    final_decision = await asyncio.wait_for(
                        self.generator.decide(view),
                        timeout=max(0.1, total_deadline - self.timer.now()),
                    )
                except Exception:
                    final_decision = None
                actual_final = BudgetUsage(
                    model_requests=1,
                    input_tokens=estimate_tokens(view.to_prompt()),
                    output_tokens=estimate_tokens(
                        final_decision.action_summary if final_decision else ""
                    ),
                )
                await self.read_sessions.settle(
                    spec.session_id, final_reservation.reservation_id, actual_final
                )
                ledger.add(actual_final)
                await step(
                    ReadStepKind.FINALIZE,
                    params_summary={"kind": final_decision.kind if final_decision else None},
                    usage=actual_final,
                )
                if final_decision is not None and final_decision.kind == "finish":
                    validated, _repaired = await self._validate_finish(
                        spec,
                        final_decision,
                        evidence,
                        generation,
                        repairs_used,
                        step,
                        ledger,
                    )
                    if validated is not None:
                        decision_answer = validated
                        stop = StopReason.ANSWERED
                        if validated.answer_status is AnswerStatus.CLARIFICATION:
                            clarification = (
                                validated.statements[0].text if validated.statements else None
                            )
                            stop = StopReason.NEEDS_CLARIFICATION

        if decision_answer is None:
            decision_answer = await self._template_answer(
                spec,
                stop or StopReason.BUDGET_EXHAUSTED,
                unresolved,
                evidence,
                generation,
                ledger.snapshot(),
                clarification,
                packet,
            )

        # -- pre-send revalidation: authorization and availability (§7.2) ----
        problem = await self._context_problem(spec)
        if problem:
            revoked = deterministic_gap_answer(
                spec.session_id,
                problem,
                question,
                (),
                "资料访问授权在查询期间发生变化，已停止使用本次结果。",
            )
            decision_answer = revoked.model_copy(update={"freshness": decision_answer.freshness})

        final_answer = decision_answer.model_copy(
            update={
                "generation_id": generation.generation_id,
                "freshness": FreshnessInfo(
                    generation_id=generation.generation_id,
                    binding_version=generation.binding_version,
                    access_epoch=generation.access_epoch,
                    last_verified_at=self.clock.now(),
                    coverage=dict(generation.coverage),
                ),
                "budget_usage": ledger.snapshot(),
                "stop_reason": decision_answer.stop_reason or stop or StopReason.ANSWERED,
            }
        )
        if not final_answer.rendered_text:
            final_answer = final_answer.model_copy(
                update={"rendered_text": render_answer(final_answer, question)}
            )
        await self.read_sessions.finish_session(
            spec.session_id,
            final_answer.stop_reason or StopReason.ANSWERED,
            final_answer.answer_status,
            answer=final_answer.model_dump(mode="json"),
            final_budget=ledger.snapshot(),
        )
        return final_answer

    # ------------------------------------------------------------------ helpers

    async def _org_state(self, project_id):
        if self.org_state_provider is None:
            return None
        state = self.org_state_provider(project_id)
        state = await state if inspect.isawaitable(state) else state
        return state.model_copy(deep=True) if state is not None else None

    async def _context_problem(self, spec):
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
        problem = await check_generation(self.sources, self.knowledge, generation)
        if problem:
            return problem
        if _state_refs(await self._org_state(spec.project_id)) != spec.org_state_refs:
            return StopReason.CONTEXT_CHANGED
        return None

    async def _settle_unknown(self, session_id: str, reservation_id: str | None) -> None:
        if reservation_id is None:
            return
        with contextlib.suppress(KnowledgeContractError):
            await self.read_sessions.settle(session_id, reservation_id, None)

    async def _repair_or_fail(
        self, spec, view, rejected, reason, repairs_used
    ) -> ProjectAnswerDecision | None:
        if repairs_used >= self.limits.max_structured_repairs:
            return None
        try:
            return await self.generator.repair(view, rejected, reason)
        except Exception:
            return None

    async def _validate_finish(
        self, spec, decision, evidence, generation, repairs_used, step, ledger
    ):
        """Evidence-validates a finish decision with one in-place repair.

        Returns ``(answer, repaired_flag)``: the answer when a valid finish
        (original or repaired) could be assembled, or ``(None, ...)`` when the
        generator failed its contract and the gap template takes over. The
        repair is one extra model request, counted here against the ledger.
        """
        problems = _validate_statements(decision.statements, evidence, generation)
        if not problems:
            return await self._assemble_valid_finish(spec, decision, evidence), False
        if repairs_used >= self.limits.max_structured_repairs:
            return None, False
        view = ReadLoopView(
            round_number=ledger.exploration_rounds + 1,
            question=spec.question,
            brief="",
            evidence=tuple(evidence.values()),
            unresolved=tuple(problems),
            remaining=BudgetUsage(),
            limits_summary="repair round",
        )
        try:
            repaired_decision = await self.generator.repair(view, decision, "; ".join(problems))
        except Exception:
            return None, False
        ledger.model_requests += 1
        ledger.input_tokens += estimate_tokens(view.to_prompt())
        ledger.output_tokens += estimate_tokens(repaired_decision.action_summary)
        problems_after = _validate_statements(repaired_decision.statements, evidence, generation)
        if problems_after:
            return None, True
        return await self._assemble_valid_finish(spec, repaired_decision, evidence), True

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

    async def _template_answer(
        self, spec, stop, unresolved, evidence, generation, usage, clarification, packet
    ) -> GroundedProjectAnswer:
        if clarification:
            answer = GroundedProjectAnswer(
                read_session_id=spec.session_id,
                uncertainties=(clarification,),
                answer_status=AnswerStatus.CLARIFICATION,
                stop_reason=stop,
                budget_usage=usage,
            )
            return answer.model_copy(update={"rendered_text": render_answer(answer, spec.question)})
        note = "; ".join(unresolved[-3:]) or "未获得足够依据。"
        answer = deterministic_gap_answer(
            spec.session_id, stop, spec.question, tuple(evidence.values()), note
        )
        return answer.model_copy(
            update={
                "conflicts": packet.conflicts,
                "freshness": packet.freshness,
                "budget_usage": usage,
                "stop_reason": stop,
            }
        )

    async def _finish_simple(self, spec, stop, status, note, usage, step) -> GroundedProjectAnswer:
        answer = GroundedProjectAnswer(
            read_session_id=spec.session_id,
            uncertainties=(note,),
            missing_info=(note,),
            answer_status=status,
            stop_reason=stop,
            budget_usage=usage,
        )
        rendered = render_answer(answer, spec.question)
        await self.read_sessions.finish_session(
            spec.session_id,
            stop,
            status,
            answer=answer.model_copy(update={"rendered_text": rendered}).model_dump(mode="json"),
        )
        return answer.model_copy(update={"rendered_text": rendered})

    async def _revoked_since_start(
        self, binding, requester_id: str, answer: GroundedProjectAnswer
    ) -> bool:
        """True when authorization or a cited source became unavailable."""
        if await check_binding(self.sources, binding, requester_id):
            return True
        for statement in answer.statements:
            for ref in statement.evidence_refs:
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
                or _returned_reference(ref, evidence) is None
            ):
                problems.append(f"第 {index} 条陈述的来源、位置或原文不属于已返回证据")
            if not ref.snippet.strip():
                problems.append(f"第 {index} 条陈述的证据缺少原文片段")
    return problems


def _state_refs(state):
    if state is None:
        return ()
    data = state.model_dump(mode="json")
    data["processed_event_ids"] = sorted(data["processed_event_ids"])
    return (f"org:{canonical_payload_hash(data)}",)


def _returned_reference(ref: EvidenceRef, evidence: dict[str, EvidenceRef]) -> EvidenceRef | None:
    for known in evidence.values():
        if (known.revision_id, known.source_id, known.kind) != (
            ref.revision_id,
            ref.source_id,
            ref.kind,
        ):
            continue
        if any(
            getattr(ref, field) is not None and getattr(ref, field) != getattr(known, field)
            for field in ("paragraph", "line_start", "line_end")
        ):
            continue
        quote = " ".join(ref.snippet.split())
        if quote and quote in " ".join(known.snippet.split()):
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
        answer_status=(
            decision.answer_status
            if decision.answer_status is not AnswerStatus.UNAVAILABLE
            else AnswerStatus.COMPLETE
        ),
    )
    return answer.model_copy(update={"rendered_text": render_answer(answer, spec.question)})


async def _work_item_note(sources: SqlProjectSourceStore, ref: EvidenceRef) -> str | None:
    """Deterministic facts from the cited revision's stored metadata."""
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

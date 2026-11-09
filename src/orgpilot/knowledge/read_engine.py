"""Execution of one pinned, budgeted read session (no business side effects)."""

import asyncio
import json

from orgpilot.knowledge.answerer import (
    DecisionValidationError,
    ReadLoopView,
    deterministic_gap_answer,
    render_answer,
)
from orgpilot.knowledge.budget import ReadStopped, SessionBudget
from orgpilot.knowledge.context import ProjectContextBuilder
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetReservation,
    BudgetUsage,
    EvidenceRef,
    FreshnessInfo,
    GroundedProjectAnswer,
    ReadStepKind,
    ReadStepRecord,
    ReservationKind,
    SourceKind,
    StopReason,
    content_hash,
)
from orgpilot.knowledge.read_tools import ReadTools, ReadToolSession
from orgpilot.knowledge.retrieval import MaterialIndex


def evidence_key(ref):
    return f"{ref.revision_id}|{ref.paragraph or ''}|{content_hash(ref.snippet)}"


class ReadEngine:
    def __init__(self, owner, spec, binding, generation_id, start, org_state):
        self.owner, self.spec, self.binding = owner, spec, binding
        self.generation_id, self.start, self.org_state = generation_id, start, org_state
        self.limits, self.seq = spec.budget_limits, 0
        self.budget = SessionBudget(
            spec, owner.read_sessions, owner.clock, owner.timer, start, owner.generator, self.step
        )
        self.evidence, self.results, self.unresolved = {}, [], []
        self.relation_ids = set()
        self.generation = None
        self.packet = None

    async def step(self, kind, **fields):
        self.seq += 1
        await self.owner.read_sessions.append_step(
            ReadStepRecord(
                session_id=self.spec.session_id,
                seq=self.seq,
                kind=kind,
                occurred_at=self.owner.clock.now(),
                **fields,
            )
        )

    def used_task_ids(self) -> tuple[str, ...]:
        """Formal tasks this session has read so far (its formal-state read set)."""
        from orgpilot.knowledge.read_loop import _task_ids_in

        return _task_ids_in(self.evidence.values())

    async def context_problem(self):
        return await self.owner._context_problem(self.spec, self.used_task_ids())

    async def check(self):
        problem = await self.context_problem()
        if problem:
            raise ReadStopped(problem, "项目上下文或访问授权已变化，请重新提问。")

    def view(self, final=False):
        used = self.budget.used
        remaining = BudgetUsage(
            exploration_rounds=max(0, self.limits.max_exploration_rounds - used.exploration_rounds),
            tool_calls=max(0, self.limits.max_tool_calls - used.tool_calls),
            model_requests=max(0, self.limits.max_model_requests - used.model_requests),
            input_tokens=max(0, self.limits.max_total_tokens - used.total_tokens()),
            evidence_chars=max(0, self.limits.max_evidence_chars - used.evidence_chars),
            evidence_fragments=max(0, self.limits.max_evidence_fragments - used.evidence_fragments),
        )
        return ReadLoopView(
            round_number=self.limits.max_exploration_rounds + 1
            if final
            else used.exploration_rounds,
            question=self.spec.question,
            brief=self.packet.brief,
            evidence=tuple(self.evidence.values()),
            unresolved=tuple(self.unresolved[-8:]),
            remaining=remaining,
            limits_summary=f"{remaining.model_dump_json()} time_remaining="
            f"{max(0, self.start + self.limits.total_time_seconds - self.owner.timer.now()):.2f}",
            tool_results=tuple(self.results[-2:]),
            allow_tools=not final,
            max_output_tokens=self.limits.max_output_tokens_per_call,
        )

    async def choose(self, final=False):
        from orgpilot.knowledge.read_loop import _validate_statements

        await self.check()
        view = self.view(final)
        rejected, repair_reason = None, None
        while True:
            try:
                decision = await self.budget.call(
                    view,
                    final=final and repair_reason is None,
                    rejected=rejected,
                    repair_reason=repair_reason,
                )
                await self.check()
                if decision.kind == "finish":
                    problems = _validate_statements(
                        decision.statements, self.evidence, self.generation
                    )
                    if not decision.statements and decision.answer_status is not (
                        AnswerStatus.CLARIFICATION
                    ):
                        problems.append("finish requires returned evidence")
                    if problems:
                        raise DecisionValidationError("; ".join(problems))
                if decision.kind not in ("lookup", "finish", "ask_clarification", "report_gap"):
                    raise DecisionValidationError("unknown decision")
                await self.step(
                    ReadStepKind.DECISION,
                    params_summary={
                        "kind": decision.kind,
                        "tool": decision.tool,
                        "summary": decision.action_summary[:200],
                    },
                )
                return decision
            except DecisionValidationError as exc:
                if repair_reason is not None or self.budget.used.structured_repairs >= (
                    self.limits.max_structured_repairs
                ):
                    raise ReadStopped(
                        StopReason.MODEL_FAILED, "模型输出或引用校验未通过。"
                    ) from exc
                rejected, repair_reason = locals().get("decision"), str(exc)
                await self.check()

    async def run(self):
        stop = None
        answer = None
        try:
            if self.binding is None:
                raise ReadStopped(StopReason.CONTEXT_UNAVAILABLE, "项目尚未绑定资料仓库。")
            if not self.binding.member_can_read(self.spec.requester_id):
                raise ReadStopped(StopReason.ACCESS_DENIED, "你没有该项目资料的访问授权。")
            self.generation = (
                await self.owner.knowledge.get_generation(self.generation_id)
                if (self.generation_id)
                else None
            )
            if self.generation is None or not self.generation.valid:
                raise ReadStopped(StopReason.CONTEXT_UNAVAILABLE, "项目资料尚未就绪或已失效。")
            await self.check()
            await self.initial_packet()
            held = await self.budget.hold_final()
            no_progress = 0
            if held:
                while self.budget.used.exploration_rounds < self.limits.max_exploration_rounds:
                    if self.owner.timer.now() >= min(
                        self.start + self.limits.exploration_time_seconds,
                        self.start
                        + self.limits.total_time_seconds
                        - self.limits.finalize_reserve_seconds,
                    ):
                        stop = StopReason.BUDGET_EXHAUSTED
                        break
                    await self.budget.charge(BudgetUsage(exploration_rounds=1))
                    try:
                        decision = await self.choose()
                    except ReadStopped as exc:
                        if exc.reason is not StopReason.BUDGET_EXHAUSTED:
                            raise
                        self.unresolved.append(exc.detail)
                        stop = exc.reason
                        break
                    answer, stop = await self.apply(decision)
                    if stop is not None:
                        break
                    try:
                        progress = await self.lookup(decision)
                    except ReadStopped as exc:
                        if exc.reason is not StopReason.BUDGET_EXHAUSTED:
                            raise
                        stop = exc.reason
                        self.unresolved.append(exc.detail)
                        break
                    no_progress = 0 if progress else no_progress + 1
                    if no_progress >= self.limits.max_no_progress_rounds:
                        stop = StopReason.NO_PROGRESS
                        self.unresolved.append("连续查阅没有新增可用证据。")
                        break
            if answer is None and stop in (
                None,
                StopReason.BUDGET_EXHAUSTED,
                StopReason.NO_PROGRESS,
            ):
                # No tool action from this call can execute; its held budget
                # is consumed once, not reserved a second time.
                try:
                    final_decision = await self.choose(final=True)
                    if final_decision.kind != "lookup":
                        answer, stop = await self.apply(final_decision)
                except ReadStopped as exc:
                    if exc.reason is not StopReason.BUDGET_EXHAUSTED:
                        raise
                    stop = stop or exc.reason
                    self.unresolved.append(exc.detail)
        except ReadStopped as exc:
            stop = exc.reason
            self.unresolved.append(exc.detail)
        return await self.finish(answer, stop or StopReason.BUDGET_EXHAUSTED)

    async def initial_packet(self):
        count = min(4, self.limits.max_evidence_fragments)
        chars = min(8000, self.limits.max_evidence_chars)
        reservation = BudgetReservation(
            reservation_id=f"{self.spec.session_id}-initial",
            kind=ReservationKind.TOOL_BATCH,
            tool_calls=1,
            evidence_fragments=count,
            evidence_chars=chars,
            created_at=self.owner.clock.now(),
        )
        try:
            await self.owner.read_sessions.reserve(self.spec.session_id, reservation)
        except Exception as exc:
            raise ReadStopped(
                StopReason.BUDGET_EXHAUSTED, "initial lookup budget exhausted"
            ) from exc
        objects = await self.owner.sources.list_source_objects(
            self.spec.project_id,
            self.binding.binding_version,
        )
        revisions = {}
        for revision_id in self.generation.revision_manifest:
            revision = await self.owner.sources.get_revision(revision_id)
            revisions[revision.source_id] = revision
        index = MaterialIndex.from_generation_material(objects, revisions)
        self.packet = ProjectContextBuilder(self.owner.clock).build_packet(
            read_session_id=self.spec.session_id,
            requester_id=self.spec.requester_id,
            binding=self.binding,
            generation=self.generation,
            index=index,
            objects=objects,
            question=self.spec.question,
            org_state=self.org_state,
            max_evidence=count,
            max_chars=chars,
        )
        if len(self.packet.brief) > self.limits.summary_max_chars:
            self.packet = self.packet.model_copy(
                update={
                    "brief": self.packet.brief[: self.limits.summary_max_chars],
                    "missing_info": self.packet.missing_info + ("项目摘要受会话额度限制已截断。",),
                }
            )
        await self.check()
        await self.owner.read_sessions.save_packet(self.packet)
        self.evidence = {evidence_key(ref): ref for ref in self.packet.relevant_evidence}
        usage = BudgetUsage(
            tool_calls=1,
            evidence_fragments=len(self.evidence),
            evidence_chars=sum(len(ref.snippet) for ref in self.evidence.values()),
        )
        await self.owner.read_sessions.settle(
            self.spec.session_id, reservation.reservation_id, usage
        )
        await self.budget.refresh()
        self.unresolved.extend(self.packet.missing_info)
        relations = await self.owner.knowledge.list_relations(self.spec.project_id)
        self.tools = ReadTools(
            ReadToolSession(
                session_id=self.spec.session_id,
                project_id=self.spec.project_id,
                requester_id=self.spec.requester_id,
                binding=self.binding,
                generation=self.generation,
                index=index,
                org_state=self.org_state,
                relations=tuple(
                    r for r in relations if r.relation_id in self.generation.relation_refs
                ),
                sources=self.owner.sources,
                clock=self.owner.clock,
                context_check=self.context_problem,
            )
        )

    async def apply(self, decision):
        if decision.kind == "lookup":
            return None, None
        if decision.kind == "finish":
            answer = await self.owner._assemble_valid_finish(self.spec, decision, self.evidence)
            stop = {
                AnswerStatus.COMPLETE: StopReason.ANSWERED,
                AnswerStatus.CLARIFICATION: StopReason.NEEDS_CLARIFICATION,
            }.get(answer.answer_status, StopReason.INSUFFICIENT_EVIDENCE)
            return answer, stop
        if decision.kind == "ask_clarification":
            return GroundedProjectAnswer(
                read_session_id=self.spec.session_id,
                answer_status=AnswerStatus.CLARIFICATION,
                uncertainties=(decision.clarification_question or "请明确问题指向。",),
            ), StopReason.NEEDS_CLARIFICATION
        self.unresolved.append(decision.gap or "资料不足以回答该问题。")
        return None, StopReason.INSUFFICIENT_EVIDENCE

    async def lookup(self, decision):
        from orgpilot.knowledge.read_loop import _kind_from_tool

        await self.check()
        used = self.budget.used
        chars = min(4096, self.limits.max_evidence_chars - used.evidence_chars)
        fragments = min(5, self.limits.max_evidence_fragments - used.evidence_fragments)
        if used.tool_calls >= self.limits.max_tool_calls or chars <= 0 or fragments <= 0:
            raise ReadStopped(StopReason.BUDGET_EXHAUSTED, "tool/evidence budget exhausted")
        timeout = min(
            self.limits.max_tool_seconds,
            self.start + self.limits.total_time_seconds - self.owner.timer.now(),
        )
        if timeout <= 0:
            raise ReadStopped(StopReason.BUDGET_EXHAUSTED, "time budget exhausted")
        reservation = BudgetReservation(
            reservation_id=f"{self.spec.session_id}-tool-{used.tool_calls + 1}",
            kind=ReservationKind.TOOL_BATCH,
            tool_calls=1,
            evidence_chars=chars,
            evidence_fragments=fragments,
            created_at=self.owner.clock.now(),
        )
        await self.owner.read_sessions.reserve(self.spec.session_id, reservation)
        self.tools.session.max_evidence_chars = chars
        self.tools.session.max_fragments = fragments
        try:
            result = await asyncio.wait_for(self.tools.call(decision.tool, decision.args), timeout)
        except TimeoutError as exc:
            await self.owner.read_sessions.settle(
                self.spec.session_id, reservation.reservation_id, BudgetUsage(tool_calls=1)
            )
            raise ReadStopped(StopReason.READ_FAILED, "资料读取超时。") from exc
        await self.check()
        progress, new_chars, new_fragments = False, 0, 0
        self.results.append(result.to_payload())
        for item in result.items:
            snippet = item.get("snippet") or item.get("content")
            if snippet:
                new_chars += len(snippet)  # repeated/cached text is still charged
                ref = EvidenceRef(
                    revision_id=item["revision_id"],
                    source_id=item["source_id"],
                    kind=_kind_from_tool(item),
                    title=item.get("title"),
                    paragraph=item.get("paragraph")
                    or (f"offset:{item['char_offset']}" if "char_offset" in item else None),
                    line_start=item.get("line_start"),
                    line_end=item.get("line_end"),
                    snippet=snippet,
                )
                key = evidence_key(ref)
                covered = any(
                    old.revision_id == ref.revision_id
                    and old.paragraph == ref.paragraph
                    and " ".join(ref.snippet.split()) in " ".join(old.snippet.split())
                    for old in self.evidence.values()
                )
                if key not in self.evidence and not covered:
                    self.evidence[key] = ref
                    new_fragments += 1
                    progress = True
            elif item.get("relation_id"):
                relation_id = item["relation_id"]
                if relation_id not in self.relation_ids:
                    self.relation_ids.add(relation_id)
                    progress = item.get("confirmation_status") == "confirmed" or progress
            elif item.get("task_id"):
                raw = json.dumps(item, ensure_ascii=False)
                new_chars += len(raw)
                key = f"task:{item['task_id']}:{content_hash(raw)}"
                if key not in self.evidence:
                    self.evidence[key] = EvidenceRef(
                        revision_id=f"state:{content_hash(raw)}",
                        source_id=f"task:{item['task_id']}",
                        kind=SourceKind.DOCUMENT,
                        paragraph=f"正式任务:{item['task_id']}",
                        snippet=raw,
                    )
                    new_fragments += 1
                    progress = True
        usage = BudgetUsage(
            tool_calls=1, evidence_chars=new_chars, evidence_fragments=new_fragments
        )
        await self.owner.read_sessions.settle(
            self.spec.session_id, reservation.reservation_id, usage
        )
        await self.budget.refresh()
        result_summary = {
            "ok": result.ok,
            "cached": result.cached,
            "error_code": result.error_code,
            "truncated": result.truncated,
        }
        if decision.tool == "read_task_state" and result.ok:
            result_summary["returned_task_ids"] = [item["task_id"] for item in result.items]
        await self.step(
            ReadStepKind.TOOL_CALL,
            tool_name=decision.tool,
            params_summary=decision.args,
            usage=usage,
            result_summary=result_summary,
            outcome="cache_hit" if result.cached else (result.error_code or "executed"),
        )
        if not result.ok:
            self.unresolved.append(f"读取失败：{result.error_code}")
            if result.error_code == "tool_denied":
                raise ReadStopped(StopReason.TOOL_DENIED, "该读取方式未被允许。")
        return progress

    async def finish(self, answer, stop):
        problem = await self.context_problem() if self.generation else None
        if problem:
            stop, answer = problem, None
        restricted = stop in (
            StopReason.ACCESS_DENIED,
            StopReason.CONTEXT_CHANGED,
            StopReason.CONTEXT_UNAVAILABLE,
        )
        if answer is None:
            answer = deterministic_gap_answer(
                self.spec.session_id,
                stop,
                self.spec.question,
                () if restricted else tuple(self.evidence.values()),
                "当前资料授权或固定上下文已失效，请重新提问。"
                if restricted
                else ("; ".join(self.unresolved[-3:]) or "未获得足够依据。"),
            )
        await self.budget.release_final()
        await self.budget.charge(
            BudgetUsage(
                elapsed_seconds=max(
                    0,
                    self.owner.timer.now() - self.start,
                )
            )
        )
        answer = answer.model_copy(
            update={
                "stop_reason": stop,
                "budget_usage": self.budget.used,
                "generation_id": self.generation_id,
                "answer_status": AnswerStatus.UNAVAILABLE if restricted else answer.answer_status,
                "conflicts": ()
                if restricted
                else tuple(
                    dict.fromkeys(
                        answer.conflicts + (self.packet.conflicts if self.packet else ()),
                    )
                ),
                "missing_info": ()
                if restricted
                else tuple(
                    dict.fromkeys(
                        answer.missing_info + (self.packet.missing_info if self.packet else ()),
                    )
                ),
                "freshness": FreshnessInfo(
                    generation_id=self.generation_id,
                    binding_version=self.binding.binding_version if self.binding else None,
                    access_epoch=self.binding.access_epoch if self.binding else None,
                    last_verified_at=self.generation.published_at if self.generation else None,
                    coverage=self.generation.coverage if self.generation and not restricted else {},
                ),
            }
        )
        answer = answer.model_copy(
            update={"rendered_text": render_answer(answer, self.spec.question)}
        )
        await self.owner.read_sessions.finish_session(
            self.spec.session_id,
            stop,
            answer.answer_status,
            answer=answer.model_dump(mode="json"),
        )
        return answer

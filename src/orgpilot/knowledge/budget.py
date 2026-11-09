"""One persistent accounting path for exploration, repair and final generation."""

import asyncio
from decimal import Decimal

from orgpilot.knowledge.answerer import (
    TOKEN_ESTIMATOR_VERSION,
    DecisionValidationError,
    decision_wire,
    estimate_tokens,
)
from orgpilot.knowledge.errors import KnowledgeContractError
from orgpilot.knowledge.models import (
    BudgetReservation,
    BudgetUsage,
    ReadStepKind,
    ReservationKind,
    StopReason,
)


class ReadStopped(Exception):
    def __init__(self, reason: StopReason, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class SessionBudget:
    def __init__(self, spec, store, clock, timer, start, generator, step):
        self.spec, self.store, self.clock = spec, store, clock
        self.timer, self.start, self.generator = timer, start, generator
        self.limits, self.step = spec.budget_limits, step
        self.used = BudgetUsage()
        self.final_hold = None
        self.attempt = 0

    def cost(self, inputs, outputs):
        if not getattr(self.generator, "online", False):
            return Decimal("0")
        if any(
            value is None
            for value in (
                self.limits.max_spend_usd,
                self.limits.input_usd_per_million,
                self.limits.output_usd_per_million,
            )
        ):
            raise ReadStopped(StopReason.MODEL_FAILED, "online price/spend configuration missing")
        return (
            inputs * self.limits.input_usd_per_million
            + outputs * self.limits.output_usd_per_million
        ) / Decimal(1_000_000)

    async def refresh(self):
        self.used = (await self.store.get_session(self.spec.session_id)).used_budget
        return self.used

    async def charge(self, usage):
        self.used = await self.store.add_usage(self.spec.session_id, usage)

    async def hold_final(self):
        reservation = BudgetReservation(
            reservation_id=f"{self.spec.session_id}-final-hold",
            kind=ReservationKind.FINAL_ANSWER,
            model_requests=1,
            input_tokens=self.limits.max_input_tokens_per_call,
            output_tokens=self.limits.max_output_tokens_per_call,
            cost_usd=self.cost(
                self.limits.max_input_tokens_per_call, self.limits.max_output_tokens_per_call
            ),
            created_at=self.clock.now(),
        )
        try:
            await self.store.reserve(self.spec.session_id, reservation)
        except KnowledgeContractError:
            return False
        self.final_hold = reservation
        await self.step(ReadStepKind.RESERVE, reservation=reservation, outcome="final_budget_held")
        return True

    async def release_final(self):
        if self.final_hold is not None:
            await self.store.release(self.spec.session_id, self.final_hold.reservation_id)
            self.final_hold = None

    async def call(self, view, *, final=False, rejected=None, repair_reason=None):
        now = self.timer.now()
        total = self.start + self.limits.total_time_seconds
        deadline = (
            total
            if final or not view.allow_tools
            else min(
                total,
                self.start + self.limits.exploration_time_seconds,
            )
        )
        if now >= deadline:
            raise ReadStopped(StopReason.BUDGET_EXHAUSTED, "time budget exhausted")
        if repair_reason is not None:
            await self.refresh()
            if self.used.structured_repairs >= self.limits.max_structured_repairs:
                raise ReadStopped(StopReason.MODEL_FAILED, "structured repair limit exhausted")
        self.generator.max_output_tokens = self.limits.max_output_tokens_per_call
        bound = getattr(self.generator, "request_bound", None)
        input_bound = bound(view, repair_reason) if bound else estimate_tokens(view.to_prompt())
        if input_bound > self.limits.max_input_tokens_per_call:
            raise ReadStopped(StopReason.BUDGET_EXHAUSTED, "input token limit exhausted")
        self.attempt += 1
        reservation = (
            self.final_hold
            if final and self.final_hold is not None
            else (
                BudgetReservation(
                    reservation_id=f"{self.spec.session_id}-attempt-{self.attempt}",
                    kind=ReservationKind.MODEL_CALL,
                    model_requests=1,
                    input_tokens=input_bound,
                    output_tokens=self.limits.max_output_tokens_per_call,
                    cost_usd=self.cost(input_bound, self.limits.max_output_tokens_per_call),
                    created_at=self.clock.now(),
                )
            )
        )
        if reservation is self.final_hold:
            self.final_hold = None
        else:
            try:
                await self.store.reserve(self.spec.session_id, reservation)
            except KnowledgeContractError as exc:
                raise ReadStopped(StopReason.BUDGET_EXHAUSTED, str(exc)) from exc
        await self.step(
            ReadStepKind.MODEL_CALL,
            reservation=reservation,
            params_summary={
                "repair": repair_reason is not None,
                "final": final,
                "input_bound": input_bound,
                "generator_version": self.generator.generator_version,
                "token_estimator": "utf8-request-plus-512-v1" if bound else TOKEN_ESTIMATOR_VERSION,
            },
            outcome="attempt_started",
        )
        if repair_reason is not None:
            await self.charge(BudgetUsage(structured_repairs=1))
        # Reset diagnostic usage; per-response usage on the decision takes
        # precedence and remains safe across simultaneous questions.
        if hasattr(self.generator, "last_usage"):
            self.generator.last_usage = None
        try:
            operation = (
                self.generator.repair(view, rejected, repair_reason)
                if (repair_reason is not None)
                else self.generator.decide(view)
            )
            decision = await asyncio.wait_for(
                operation,
                timeout=min(self.limits.max_model_seconds, deadline - self.timer.now()),
            )
        except DecisionValidationError as exc:
            usage = getattr(exc, "usage", None)
            await self._settle(reservation, usage)
            raise
        except asyncio.CancelledError:
            # If cancellation wins between response and settlement, the
            # persisted reservation remains unknown; never send it again.
            raise
        except Exception as exc:
            await self._settle(reservation, None)
            raise ReadStopped(StopReason.MODEL_FAILED, "model request failed or timed out") from exc
        usage = getattr(decision, "usage", None) or getattr(self.generator, "last_usage", None)
        if usage is None:
            usage = BudgetUsage(
                model_requests=1,
                input_tokens=input_bound,
                output_tokens=estimate_tokens(decision_wire(decision)),
            )
        await self._settle(reservation, usage)
        if self.timer.now() > total:
            raise ReadStopped(StopReason.BUDGET_EXHAUSTED, "total time budget exhausted")
        if usage.input_tokens > input_bound or (
            usage.output_tokens > self.limits.max_output_tokens_per_call
        ):
            raise ReadStopped(StopReason.MODEL_FAILED, "provider exceeded the declared token bound")
        return decision

    async def _settle(self, reservation, usage):
        if usage is not None:
            inputs = (
                max(usage.input_tokens, reservation.input_tokens)
                if (usage.unknown_token_usage)
                else usage.input_tokens
            )
            outputs = (
                max(usage.output_tokens, reservation.output_tokens)
                if (usage.unknown_token_usage)
                else usage.output_tokens
            )
            usage = BudgetUsage(
                model_requests=1,
                input_tokens=inputs,
                output_tokens=outputs,
                unknown_token_usage=usage.unknown_token_usage,
                cost_usd=self.cost(inputs, outputs),
            )
        settled = await self.store.settle(self.spec.session_id, reservation.reservation_id, usage)
        await self.refresh()
        await self.step(
            ReadStepKind.SETTLE,
            usage=settled,
            outcome="unknown" if settled.unknown_token_usage else "actual",
        )

"""R7/R8: monetary preflight, unknown amounts, and unrefundable settlement."""

from decimal import Decimal

import pytest

from orgpilot.knowledge.answerer import ProjectAnswerDecision
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetLimits,
    BudgetUsage,
    GroundedStatement,
    StopReason,
)
from tests.test_project_read_loop import make_loop
from tests.test_project_review_regressions import material_fixture  # noqa: F401


class PricedGenerator:
    generator_version = "offline-price-contract"
    online = True
    calls = 0

    def request_bound(self, view, reason=None):
        return 300

    async def decide(self, view):
        self.calls += 1
        decision = ProjectAnswerDecision.finish(
            (GroundedStatement(text="来源事实", evidence_refs=(view.evidence[0],)),),
            AnswerStatus.COMPLETE,
        )
        decision.usage = BudgetUsage(model_requests=1, input_tokens=20, output_tokens=40)
        return decision


def priced_limits(**kwargs):
    return BudgetLimits(
        max_spend_usd=Decimal("1"),
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("2"),
        **kwargs,
    )


async def test_r8_price_limit_rejects_before_provider_attempt(env):
    generator = PricedGenerator()
    limits = priced_limits().model_copy(update={"max_spend_usd": Decimal("0.000001")})
    answer = await make_loop(env, generator=generator, limits=limits).run(
        "proj-a",
        "alice",
        "price-denied",
        "项目目标是什么？",
    )
    assert generator.calls == 0
    assert answer.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert answer.budget_usage.cost_usd == 0


async def test_r8_actual_usage_and_price_are_settled_once(env):
    generator = PricedGenerator()
    answer = await make_loop(env, generator=generator, limits=priced_limits()).run(
        "proj-a",
        "alice",
        "price-actual",
        "项目目标是什么？",
    )
    assert generator.calls == 1
    assert answer.budget_usage.cost_usd == Decimal("0.0001")
    assert answer.budget_usage.total_tokens() == 60


async def test_r8_transport_unknown_keeps_worst_case_price_and_tokens(env):
    class Unknown(PricedGenerator):
        async def decide(self, view):
            self.calls += 1
            raise TimeoutError("unknown")

    answer = await make_loop(env, generator=Unknown(), limits=priced_limits()).run(
        "proj-a",
        "alice",
        "price-unknown",
        "项目目标是什么？",
    )
    assert answer.budget_usage.unknown_token_usage
    assert answer.budget_usage.input_tokens == 300
    assert answer.budget_usage.output_tokens == 1024
    assert answer.budget_usage.cost_usd == Decimal("0.002348")
    session = await env["read_sessions"].get_by_message("proj-a", "price-unknown")
    assert session.used_budget == answer.budget_usage
    assert not session.reserved_budget


async def test_r8_response_without_usage_keeps_whole_reservation(env):
    class MissingUsage(PricedGenerator):
        async def decide(self, view):
            decision = await super().decide(view)
            decision.usage = BudgetUsage(
                model_requests=1, input_tokens=1, output_tokens=1, unknown_token_usage=True
            )
            return decision

    answer = await make_loop(env, generator=MissingUsage(), limits=priced_limits()).run(
        "proj-a",
        "alice",
        "price-missing-usage",
        "项目目标是什么？",
    )
    assert answer.budget_usage.input_tokens == 300
    assert answer.budget_usage.output_tokens == 1024
    assert answer.budget_usage.cost_usd == Decimal("0.002348")


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_model_requests", 7),
        ("max_tool_calls", 9),
        ("max_total_tokens", 32769),
        ("max_structured_repairs", 2),
        ("max_evidence_chars", -1),
    ],
)
def test_r7_limits_may_only_tighten_contract(field, value):
    with pytest.raises(ValueError):
        BudgetLimits(**{field: value})


def test_r8_online_config_requires_explicit_prices_and_cap():
    from orgpilot.config import OrgPilotSettings

    settings = OrgPilotSettings(project_qa_enabled=True, project_qa_online=True)
    with pytest.raises(ValueError, match="spend cap"):
        settings.validate()

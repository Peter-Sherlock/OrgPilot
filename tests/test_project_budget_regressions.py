"""R7/R8: monetary preflight, unknown amounts, and unrefundable settlement."""

from decimal import Decimal

import pytest

from orgpilot.knowledge.answerer import (
    ProjectAnswerDecision,
    ReadLoopView,
    ScriptedAnswerGenerator,
    decision_wire,
    estimate_tokens,
)
from orgpilot.knowledge.models import (
    AnswerStatus,
    BudgetLimits,
    BudgetUsage,
    EvidenceRef,
    GroundedStatement,
    SourceKind,
    StopReason,
)
from tests.test_project_read_loop import make_loop
from tests.test_project_review_regressions import material_fixture  # noqa: F401


@pytest.mark.parametrize(("limit", "kind"), [(1024, "finish"), (220, "finish"), (80, "report_gap")])
async def test_scripted_finish_fits_output_limit_with_the_relevant_quote(limit, kind):
    row = "| `DEMO_WRITES` | `false` | 写入工具仅在显式开启后才存在 |"
    ref = EvidenceRef(
        revision_id="rev-config",
        source_id="src-config",
        kind=SourceKind.DOCUMENT,
        paragraph="配置#L3-L8",
        snippet="背景说明" * 1100 + "\n\n| 变量 | 默认值 | 说明 |\n| --- | --- | --- |\n" + row,
    )
    weak = ref.model_copy(update={"paragraph": "术语#L9-L10", "snippet": "工具分类见目录。"})
    view = ReadLoopView(
        round_number=2,
        question="写入工具默认是否开启？",
        brief="",
        evidence=(ref, weak),
        unresolved=(),
        remaining=BudgetUsage(),
        limits_summary="",
        max_output_tokens=limit,
    )
    decision = await ScriptedAnswerGenerator().decide(view)

    assert decision.kind == kind
    assert estimate_tokens(decision_wire(decision)) <= limit
    if kind == "finish":
        (statement,) = decision.statements
        assert "默认值 `false`" in statement.text
        assert "仅在显式开启后才存在" in statement.text
        assert statement.evidence_refs == (ref.model_copy(update={"snippet": row}),)
    else:
        assert not decision.statements


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

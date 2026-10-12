"""Unit tests for the layer-2 guardrails (pure functions, ADR-0008 §3)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from orgpilot.intent.client import (
    IntentContext,
    PendingQuestionView,
    TaskSnapshot,
)
from orgpilot.intent.guards import (
    check_confidence,
    check_grounding,
    check_hedging,
    check_role,
    check_speech_act,
    check_task_reference,
    check_tier,
    check_time_expression,
    evaluate_item_guards,
)
from orgpilot.intent.models import (
    Certainty,
    IntentItem,
    IntentType,
    Polarity,
    SpeechAct,
)

BJ = ZoneInfo("Asia/Shanghai")
OCCURRED_AT = datetime(2026, 10, 13, 10, 0, tzinfo=BJ)  # Tuesday

CONTEXT = IntentContext(
    project_id="proj",
    message="支付SDK报错了",
    occurred_at=OCCURRED_AT,
    sender_id="ou_wei",
    sender_role="backend",
    sender_display_name="王伟",
    sender_task_ids=["pay_sdk", "merchant_admin"],
    known_tasks=[
        TaskSnapshot(
            task_id="pay_sdk", title="支付SDK接入", owner_id="ou_wei", deadline=None
        ),
        TaskSnapshot(
            task_id="merchant_admin",
            title="商户后台管理端",
            owner_id="ou_wei",
            deadline=None,
        ),
    ],
)


def _item(message: str, **overrides: object) -> IntentItem:
    values: dict[str, object] = {
        "intent": IntentType.REPORT_STATUS,
        "task_id": "pay_sdk",
        "speech_act": SpeechAct.STATEMENT,
        "polarity": Polarity.AFFIRMATIVE,
        "certainty": Certainty.CERTAIN,
        "health_status": "at_risk",
        "source_quote": message,
        "confidence": 0.9,
    }
    values.update(overrides)
    return IntentItem(**values)  # type: ignore[arg-type]


def test_grounding_requires_verbatim_quote() -> None:
    assert check_grounding(_item("支付SDK报错了"), "支付SDK报错了").verdict == "KEEP"
    bad_quote = _item("支付SDK报错了", source_quote=" completely different")
    outcome = check_grounding(bad_quote, "支付SDK报错了")
    assert outcome.verdict == "DROP"
    assert "grounding" in outcome.reason


def test_task_reference_asks_r7_question_listing_sender_tasks() -> None:
    item = _item("没有遇到阻塞", task_id=None)
    outcome = check_task_reference(item, CONTEXT)
    assert outcome.verdict == "ASK"
    assert "是指哪个任务" in (outcome.ask_question or "")
    assert "支付SDK接入" in (outcome.ask_question or "")
    assert "商户后台管理端" in (outcome.ask_question or "")


def test_task_reference_rejects_unknown_task() -> None:
    outcome = check_task_reference(_item("x", task_id="nope"), CONTEXT)
    assert outcome.verdict == "ASK"


def test_answer_pending_inherits_pending_question_task() -> None:
    context = CONTEXT.model_copy(
        update={
            "pending_question": PendingQuestionView(question="什么时候恢复？", task_id="pay_sdk")
        }
    )
    item = _item("明天恢复", intent=IntentType.ANSWER_PENDING, task_id=None)
    assert check_task_reference(item, context).verdict == "KEEP"
    # without a pending target the same item must ask
    outcome = check_task_reference(item, CONTEXT)
    assert outcome.verdict == "ASK"


def test_query_and_dependency_do_not_need_task() -> None:
    item = _item("进度怎么样", intent=IntentType.QUERY_STATUS, task_id=None)
    assert check_task_reference(item, CONTEXT).verdict == "KEEP"


def test_speech_act_routes_question_and_hypothetical() -> None:
    question = check_speech_act(_item("x", speech_act=SpeechAct.QUESTION))
    assert question.verdict == "QUERY"
    hypothetical = check_speech_act(_item("x", speech_act=SpeechAct.HYPOTHETICAL))
    assert hypothetical.verdict == "NOTED"
    assert check_speech_act(_item("x")).verdict == "KEEP"


def test_hedging_asks_for_time_only_without_one() -> None:
    hedged = _item("应该快了", certainty=Certainty.HEDGED)
    outcome = check_hedging(hedged)
    assert outcome.verdict == "ASK" and "什么时候" in (outcome.ask_question or "")
    with_time = _item("明天能好", certainty=Certainty.HEDGED, time_expression="明天")
    assert check_hedging(with_time).verdict == "KEEP"
    assert check_hedging(_item("肯定没问题")).verdict == "KEEP"


def test_confidence_below_threshold_asks() -> None:
    assert check_confidence(_item("x", confidence=0.4)).asks
    assert check_confidence(_item("x", confidence=0.9)).verdict == "KEEP"
    assert check_confidence(_item("x", confidence=0.6)).verdict == "KEEP"


def test_time_expression_verbatim_and_date_only_compare() -> None:
    # not a substring → ask
    outcome = check_time_expression(
        _item("明天能好", time_expression="周五", expected_completion=None),
        "明天能好",
        OCCURRED_AT,
    )
    assert outcome.verdict == "ASK"
    # parseable, same date → keep (hours deliberately differ: date-only compare)
    same_date = check_time_expression(
        _item(
            "明天下午3点能好",
            time_expression="明天下午3点",
            expected_completion=datetime(2026, 10, 14, 15, 0, tzinfo=BJ),
        ),
        "明天下午3点能好",
        OCCURRED_AT,
    )
    assert same_date.verdict == "KEEP"
    # parseable, different date → ask
    diff_date = check_time_expression(
        _item(
            "明天能好",
            time_expression="明天",
            expected_completion=datetime(2026, 10, 20, 18, 0, tzinfo=BJ),
        ),
        "明天能好",
        OCCURRED_AT,
    )
    assert diff_date.verdict == "ASK"
    # unparseable expression → keep (resolver returns None)
    assert (
        check_time_expression(
            _item("月底能好", time_expression="月底", expected_completion=None),
            "月底能好",
            OCCURRED_AT,
        ).verdict
        == "KEEP"
    )


def test_role_gate_for_request_sync() -> None:
    pm_item = _item("同步", intent=IntentType.REQUEST_SYNC, task_id=None)
    pm = CONTEXT.model_copy(update={"sender_role": "pm"})
    assert check_role(pm_item, pm).verdict == "KEEP"
    assert check_role(pm_item, CONTEXT).verdict == "NO_PERMISSION"  # CONTEXT is an engineer


def test_p1_p2_tier_goes_unsupported() -> None:
    item = _item(
        "明天请假", intent=IntentType.UNAVAILABILITY, task_id=None
    )
    assert check_tier(item).verdict == "UNSUPPORTED"
    assert check_tier(_item("x")).verdict == "KEEP"


def test_negative_report_without_health_is_dropped() -> None:
    item = _item("接口我还没碰", polarity=Polarity.NEGATIVE, health_status=None)
    outcome = evaluate_item_guards(item, "商户后台的接口我还没碰", CONTEXT)
    assert outcome.verdict == "DROP"


def test_pipeline_order_task_guard_precedes_hedging() -> None:
    # vague AND task-less: R7 (which task) fires before R6 (which time)
    item = _item("应该快了", task_id=None, certainty=Certainty.HEDGED)
    outcome = evaluate_item_guards(item, "应该快了", CONTEXT)
    assert outcome.verdict == "ASK"
    assert "哪个任务" in (outcome.ask_question or "")


def test_clean_item_passes_all_guards() -> None:
    item = _item(
        "支付SDK报错了，明天下午5点能修好",
        time_expression="明天下午5点",
        expected_completion=datetime(2026, 10, 14, 17, 0, tzinfo=BJ),
        blocker="报错",
    )
    outcome = evaluate_item_guards(
        item, "支付SDK报错了，明天下午5点能修好", CONTEXT
    )
    assert outcome.verdict == "KEEP"


def test_negative_request_sync_bypasses_role_gate() -> None:
    # "不用同步进度了" from a non-PM declines a sync instead of requesting one,
    # so it must NOT hit the PM-only gate (R8 / R2 negative request_sync → noted).
    item = _item(
        "不用同步进度了", intent=IntentType.REQUEST_SYNC, task_id=None, polarity=Polarity.NEGATIVE
    )
    assert check_role(item, CONTEXT).verdict == "KEEP"
    affirmative = _item(
        "同步一下", intent=IntentType.REQUEST_SYNC, task_id=None, polarity=Polarity.AFFIRMATIVE
    )
    assert check_role(affirmative, CONTEXT).verdict == "NO_PERMISSION"

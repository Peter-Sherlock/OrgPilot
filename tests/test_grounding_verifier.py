"""Tests for GroundingVerifier and TemporalResolver."""

from datetime import datetime, timedelta

from orgpilot.domain.enums import HealthStatus
from orgpilot.extraction.models import (
    ExtractedCommitment,
    ExtractedHealthClaim,
    ExtractionResult,
    MessageContext,
)
from orgpilot.extraction.verifier import GroundingVerifier, TemporalResolver

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


def test_grounding_verifier_quote_validation() -> None:
    verifier = GroundingVerifier()
    msg = "支付 SDK 报错，排查需要到明天下午 5 点"

    assert verifier.verify_quote(msg, "支付 SDK 报错")
    assert verifier.verify_quote(msg, "明天下午 5 点")
    assert not verifier.verify_quote(msg, "Redis 挂了")
    assert not verifier.verify_quote(msg, "")


def test_grounding_verifier_filters_hallucinated_claims() -> None:
    verifier = GroundingVerifier()
    msg = "支付 SDK 报错，排查需要到明天下午 5 点"
    context = MessageContext(
        project_id="p1",
        actor_id="alice",
        occurred_at=NOW,
        known_tasks={"backend_api": "Backend API"},
    )

    valid_claim = ExtractedHealthClaim(
        task_id="backend_api",
        health_status=HealthStatus.DELAYED,
        expected_completion=NOW + timedelta(days=1),
        blocker="支付 SDK 报错",
        confidence=0.95,
        source_quote="支付 SDK 报错",
    )
    fake_quote_claim = ExtractedHealthClaim(
        task_id="backend_api",
        health_status=HealthStatus.DELAYED,
        expected_completion=None,
        blocker="Redis 挂了",
        confidence=0.95,
        source_quote="Redis 挂了",
    )
    unknown_task_claim = ExtractedHealthClaim(
        task_id="unknown_task",
        health_status=HealthStatus.DELAYED,
        expected_completion=None,
        blocker="支付 SDK 报错",
        confidence=0.95,
        source_quote="支付 SDK 报错",
    )

    raw_result = ExtractionResult(
        is_actionable=True,
        claims=[valid_claim, fake_quote_claim, unknown_task_claim],
        commitments=[],
        reasoning="test",
    )

    filtered = verifier.filter_and_verify(raw_result, msg, context)
    assert len(filtered.claims) == 1
    assert filtered.claims[0].source_quote == "支付 SDK 报错"
    assert filtered.is_actionable is True


def test_grounding_verifier_filters_hallucinated_commitments() -> None:
    verifier = GroundingVerifier()
    msg = "我承诺明天下午提 PR"
    context = MessageContext(
        project_id="p1",
        actor_id="alice",
        occurred_at=NOW,
        known_tasks={"backend_api": "Backend API"},
    )

    valid_cmt = ExtractedCommitment(
        target_id="backend_api",
        predicate="workflow_status",
        expected_value="review",
        confidence=0.9,
        source_quote="我承诺明天下午提 PR",
    )
    fake_cmt = ExtractedCommitment(
        target_id="unknown_task",
        predicate="workflow_status",
        expected_value="review",
        confidence=0.9,
        source_quote="我承诺明天下午提 PR",
    )
    filtered = verifier.filter_and_verify(
        ExtractionResult(is_actionable=True, claims=[], commitments=[valid_cmt, fake_cmt]),
        msg,
        context,
    )
    assert len(filtered.commitments) == 1
    assert filtered.commitments[0].target_id == "backend_api"


def test_temporal_resolver_expressions() -> None:
    # Tomorrow
    dt_tom = TemporalResolver.resolve_relative_time("明天下午 5 点", NOW)
    assert dt_tom is not None
    assert dt_tom.year == 2026 and dt_tom.month == 9 and dt_tom.day == 11
    assert dt_tom.hour == 17

    # Day after tomorrow
    dt_dat = TemporalResolver.resolve_relative_time("后天晚上 8 点", NOW)
    assert dt_dat is not None
    assert dt_dat.day == 12
    assert dt_dat.hour == 20

    # N days later
    dt_days = TemporalResolver.resolve_relative_time("3 天后", NOW)
    assert dt_days is not None
    assert dt_days.day == 13
    assert dt_days.hour == 18

    # Morning hour
    dt_am = TemporalResolver.resolve_relative_time("明天上午 10 点", NOW)
    assert dt_am is not None
    assert dt_am.hour == 10

    # Weekday (2026-09-10 is Thursday)
    dt_fri = TemporalResolver.resolve_relative_time("周五下午 4 点", NOW)
    assert dt_fri is not None
    assert dt_fri.day == 11
    assert dt_fri.hour == 16

    # Direct ISO
    iso_str = "2026-09-20T18:00:00+08:00"
    dt_iso = TemporalResolver.resolve_relative_time(iso_str, NOW)
    assert dt_iso is not None
    assert dt_iso.day == 20

    # Invalid expression
    assert TemporalResolver.resolve_relative_time("无时间描述", NOW) is None

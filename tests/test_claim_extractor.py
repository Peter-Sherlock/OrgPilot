"""Tests for ClaimExtractor and event generation."""

from datetime import datetime

from orgpilot.domain.enums import HealthStatus
from orgpilot.events.models import CommitmentMadeEvent, TaskHealthReportedEvent
from orgpilot.extraction.client import RecordedReplayClient
from orgpilot.extraction.extractor import ClaimExtractor
from orgpilot.extraction.models import (
    ExtractedHealthClaim,
    ExtractionResult,
    MessageContext,
)

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


def test_claim_extractor_generates_health_reported_event() -> None:
    extractor = ClaimExtractor()
    context = MessageContext(
        project_id="p1",
        actor_id="alice",
        occurred_at=NOW,
        known_tasks={"backend_api": "Backend API"},
        known_members={"alice": "engineer"},
    )
    msg = "支付 SDK 报错，排查需要到明天下午 5 点"
    result, events = extractor.extract_from_message(msg, context)

    assert result.is_actionable is True
    assert len(result.claims) == 1
    assert result.claims[0].health_status is HealthStatus.DELAYED

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TaskHealthReportedEvent)
    assert event.project_id == "p1"
    assert event.actor_id == "alice"
    assert event.payload.task_id == "backend_api"
    assert event.payload.health_status is HealthStatus.DELAYED
    assert event.payload.expected_completion is not None
    assert event.payload.expected_completion.hour == 17


def test_claim_extractor_generates_commitment_event() -> None:
    extractor = ClaimExtractor()
    context = MessageContext(
        project_id="p1",
        actor_id="alice",
        occurred_at=NOW,
        known_tasks={"backend_api": "Backend API"},
    )
    msg = "我承诺周四前把所有接口测试提 PR"
    result, events = extractor.extract_from_message(msg, context)

    assert result.is_actionable is True
    assert len(result.commitments) == 1

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, CommitmentMadeEvent)
    assert event.actor_id == "alice"
    assert event.payload.target_id == "backend_api"
    assert event.payload.expected_value == "review"


def test_claim_extractor_handles_casual_chat() -> None:
    extractor = ClaimExtractor()
    context = MessageContext(
        project_id="p1",
        actor_id="bob",
        occurred_at=NOW,
    )
    msg = "今天好累啊，中午大家一起去吃火锅吗？"
    result, events = extractor.extract_from_message(msg, context)

    assert result.is_actionable is False
    assert len(result.claims) == 0
    assert len(result.commitments) == 0
    assert len(events) == 0


def test_claim_extractor_with_recorded_replay_client() -> None:
    msg = "自定义录制消息"
    custom_res = ExtractionResult(
        is_actionable=True,
        claims=[
            ExtractedHealthClaim(
                task_id="t1",
                health_status=HealthStatus.ON_TRACK,
                confidence=1.0,
                source_quote=msg,
            )
        ],
    )
    replay_client = RecordedReplayClient({msg: custom_res})
    extractor = ClaimExtractor(llm_client=replay_client)
    context = MessageContext(
        project_id="p1",
        actor_id="alice",
        occurred_at=NOW,
        known_tasks={"t1": "Task 1"},
    )

    res, events = extractor.extract_from_message(msg, context)
    assert res.is_actionable is True
    assert len(events) == 1


def test_mock_extractor_refuses_ambiguous_task_assignment() -> None:
    extractor = ClaimExtractor()
    context = MessageContext(
        project_id="p1",
        actor_id="alice",
        occurred_at=NOW,
        known_tasks={"task_a": "Alpha", "task_b": "Beta"},
    )

    result, events = extractor.extract_from_message("这个活儿延期了", context)

    assert result.is_actionable is False
    assert result.reasoning == "No unambiguous task reference found"
    assert events == []


def test_distinct_messages_in_same_second_have_distinct_event_ids() -> None:
    extractor = ClaimExtractor()
    context = MessageContext(
        project_id="p1",
        actor_id="alice",
        occurred_at=NOW,
        known_tasks={"backend_api": "Backend API"},
    )

    _, first_events = extractor.extract_from_message("后端接口延期了", context)
    _, second_events = extractor.extract_from_message("后端接口卡住了", context)

    assert first_events[0].event_id != second_events[0].event_id


def test_upstream_message_id_produces_stable_event_id() -> None:
    extractor = ClaimExtractor()
    context = MessageContext(
        project_id="p1",
        actor_id="alice",
        occurred_at=NOW,
        source_ref="om_123",
        known_tasks={"backend_api": "Backend API"},
    )

    _, first_events = extractor.extract_from_message("后端接口延期了", context)
    _, retry_events = extractor.extract_from_message("后端接口延期了", context)

    assert first_events[0].event_id == retry_events[0].event_id

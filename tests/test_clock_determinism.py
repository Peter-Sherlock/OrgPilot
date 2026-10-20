"""Regression tests pinning the injectable clock contract.

These guard against a defect class rather than a single defect: when a
time-dependent decision reads the wall clock instead of the injected clock, the
suite still passes on the day it is written and starts failing once real time
drifts past the fixture window. Every scenario below runs the approval path
against clock instants far from today, so a reintroduced `datetime.now()` fails
immediately instead of two weeks later.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from orgpilot.config import OrgPilotSettings
from orgpilot.domain.clock import Clock, FrozenClock, SystemClock
from orgpilot.gateway.app import create_app
from orgpilot.storage.database import Database

# Deliberately far from any plausible wall clock, in both directions.
LONG_PAST = datetime.fromisoformat("2019-03-04T10:00:00+08:00")
FAR_FUTURE = datetime.fromisoformat("2088-11-20T10:00:00+08:00")


def _setup_events(project_id: str, now: datetime) -> list[dict]:
    return [
        {
            "schema_version": 1,
            "project_id": project_id,
            "event_id": f"evt-{project_id}-alice",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": now.isoformat(),
            "received_at": now.isoformat(),
            "payload": {"member_id": "alice", "display_name": "Alice", "role": "backend"},
        },
        {
            "schema_version": 1,
            "project_id": project_id,
            "event_id": f"evt-{project_id}-carol",
            "event_type": "member.registered",
            "source": "human",
            "source_ref": "setup",
            "occurred_at": now.isoformat(),
            "received_at": now.isoformat(),
            "payload": {"member_id": "carol", "display_name": "Carol", "role": "pm"},
        },
        {
            "schema_version": 1,
            "project_id": project_id,
            "event_id": f"evt-{project_id}-task-api",
            "event_type": "task.created",
            "source": "task",
            "source_ref": "setup",
            "occurred_at": now.isoformat(),
            "received_at": now.isoformat(),
            "payload": {
                "task_id": "backend_api",
                "title": "Backend API",
                "owner_id": "alice",
                "workflow_status": "doing",
                "deadline": (now + timedelta(hours=3)).isoformat(),
            },
        },
    ]


async def _run_approval_flow(project_id: str, now: datetime) -> dict:
    """Drives message -> case -> approval -> execution with the clock pinned to `now`."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init_db()
    settings = OrgPilotSettings(collaboration_adapter="mock", feishu_use_ws=False)
    app = create_app(db, settings=settings, clock=FrozenClock(now))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                f"/api/v1/projects/{project_id}/events",
                json={"events": _setup_events(project_id, now)},
            )

            resp_msg = await client.post(
                f"/api/v1/projects/{project_id}/messages",
                json={
                    "message": "支付 SDK 报错，排查需要到明天下午 5 点",
                    "actor_id": "alice",
                    "occurred_at": now.isoformat(),
                    "auto_run_turn": True,
                },
            )
            assert resp_msg.status_code == 200, resp_msg.text
            assert resp_msg.json()["turn_termination_reason"] == "waiting_approval"

            resp_appr = await client.get(f"/api/v1/projects/{project_id}/approvals")
            approvals = resp_appr.json()
            assert len(approvals) == 1, approvals
            approval_id = approvals[0]["approval_id"]

            resp_dec = await client.post(
                f"/api/v1/projects/{project_id}/approvals/{approval_id}/decision",
                json={"decision": "approved", "approver_id": "carol", "reason": "ok"},
            )
            assert resp_dec.status_code == 200, resp_dec.text
            return resp_dec.json()
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("label", "pinned_now"),
    [("long_past", LONG_PAST), ("far_future", FAR_FUTURE)],
)
async def test_approval_flow_ignores_wall_clock(label: str, pinned_now: datetime) -> None:
    """An approval created and decided on the injected clock never reads as expired.

    Before clock injection this failed for `long_past`: the approval expiry was
    computed from the event clock while the decision path used `datetime.now()`,
    so any fixture older than the expiry window was rejected as expired.
    """
    decision = await _run_approval_flow(f"proj-clock-{label}", pinned_now)

    assert decision["decision"] == "approved"
    assert decision["status"] == "approved"
    assert decision["turn_termination_reason"] == "all_resolved"


async def test_identical_pinned_clocks_produce_identical_outcomes() -> None:
    """The same scenario replayed twice on the same instant yields the same result."""
    first = await _run_approval_flow("proj-clock-replay-a", LONG_PAST)
    second = await _run_approval_flow("proj-clock-replay-b", LONG_PAST)

    assert first["turn_termination_reason"] == second["turn_termination_reason"]
    assert first["status"] == second["status"]


def test_frozen_clock_only_moves_when_moved() -> None:
    clock = FrozenClock(LONG_PAST)

    assert clock.now() == LONG_PAST
    assert clock.now() == LONG_PAST

    clock.advance(timedelta(hours=3))
    assert clock.now() == LONG_PAST + timedelta(hours=3)

    clock.set(FAR_FUTURE)
    assert clock.now() == FAR_FUTURE


def test_clocks_satisfy_the_protocol_and_return_aware_timestamps() -> None:
    system_clock = SystemClock()
    frozen_clock = FrozenClock(LONG_PAST)

    assert isinstance(system_clock, Clock)
    assert isinstance(frozen_clock, Clock)
    assert system_clock.now().tzinfo is not None
    assert system_clock.now().utcoffset() == timedelta(0)
    assert frozen_clock.now().tzinfo is not None


def test_naive_timestamps_are_rejected() -> None:
    """Matches the event contract: a timestamp without a timezone is a bug, not a default."""
    with pytest.raises(ValueError, match="explicit timezone"):
        FrozenClock(datetime(2026, 9, 10, 10, 0))

    clock = FrozenClock(datetime(2026, 9, 10, 10, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="explicit timezone"):
        clock.set(datetime(2026, 9, 11, 10, 0))

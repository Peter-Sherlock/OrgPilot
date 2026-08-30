"""Tests for the directive lifecycle: issue, relay, acknowledge, complete, escalate."""

from datetime import datetime, timedelta

import pytest

from orgpilot.adapter.mock import MockCollaborationAdapter
from orgpilot.coordination.directives import DirectiveManager
from orgpilot.domain.enums import DirectiveStatus
from orgpilot.domain.errors import DomainInvariantError
from orgpilot.events.models import (
    DirectiveIssuedEvent,
    EventSource,
    MemberRegisteredEvent,
    TaskCreatedEvent,
)
from orgpilot.extraction.models import IntentHint
from orgpilot.state.projector import OrgProjector

NOW = datetime.fromisoformat("2026-09-10T15:00:00+08:00")


def _base_events() -> list:
    """Member + task setup events for proj-dir (carol pm; alice 1 task; bob 2 tasks)."""
    events: list = []
    members = [("carol", "Carol", "pm"), ("alice", "Alice", "engineer"), ("bob", "Bob", "engineer")]
    for i, (mid, name, role) in enumerate(members, start=1):
        events.append(
            MemberRegisteredEvent(
                project_id="proj-dir",
                event_id=f"evt-m-{i}",
                event_type="member.registered",
                source=EventSource.HUMAN,
                source_ref="setup",
                occurred_at=NOW,
                received_at=NOW,
                payload={"member_id": mid, "display_name": name, "role": role},
            )
        )
    task_payloads = [
        ("evt-t-1", "task-payment", "支付SDK接入", "alice"),
        ("evt-t-2", "task-ui", "收银台前端", "bob"),
        ("evt-t-3", "task-qa", "全链路压测", "bob"),
    ]
    for event_id, task_id, title, owner in task_payloads:
        events.append(
            TaskCreatedEvent(
                project_id="proj-dir",
                event_id=event_id,
                event_type="task.created",
                source=EventSource.TASK,
                source_ref="setup",
                occurred_at=NOW,
                received_at=NOW,
                payload={"task_id": task_id, "title": title, "owner_id": owner},
            )
        )
    return events


def _setup_state() -> tuple[OrgProjector, str, str, str]:
    """Returns (projector, pm_id, single_task_owner, multi_task_owner)."""
    projector = OrgProjector(project_id="proj-dir")
    for event in _base_events():
        projector.apply(event)
    return projector, "carol", "alice", "bob"


def _manager() -> DirectiveManager:
    return DirectiveManager(adapter=MockCollaborationAdapter(project_id="proj-dir"))


def _issue_to_alice() -> tuple[OrgProjector, DirectiveManager, object]:
    projector, pm, alice, _bob = _setup_state()
    manager = _manager()
    outcome = manager.handle_directive_intent(
        message="告诉Alice，支付SDK必须在明天下午5点之前完成",
        actor_id=pm,
        hints=IntentHint(
            mentioned_member_ids=("alice",),
            raw_time_expr="明天下午5点",
        ),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "issued"
    for event in outcome.events:
        projector.apply(event)
    return projector, manager, outcome


def test_issue_directive_resolves_slots_and_relays() -> None:
    projector, _manager, outcome = _issue_to_alice()
    directive = next(iter(projector.state.directives.values()))
    assert directive.target_id == "alice"
    # Single-task owner: task auto-bound
    assert directive.task_id == "task-payment"
    assert directive.deadline == datetime.fromisoformat("2026-09-11T17:00:00+08:00")
    assert directive.status is DirectiveStatus.ISSUED
    # Relay notice goes to the target pane
    assert outcome.notices[0].actor_id == "alice"
    assert "指令" in outcome.notices[0].text
    assert outcome.outbound[0].action_type.value == "send_directive"


def test_deadline_uses_reference_timezone_not_storage_tz() -> None:
    """A UTC event timestamp with a Shanghai team: 「明天下午5点」 must land on
    17:00+08:00, not drift by the timezone delta."""
    projector, pm, _alice, _bob = _setup_state()
    manager = DirectiveManager(
        adapter=MockCollaborationAdapter(project_id="proj-dir"),
        reference_timezone="Asia/Shanghai",
    )
    outcome = manager.handle_directive_intent(
        message="告诉Alice，支付SDK必须在明天下午5点之前完成",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("alice",), raw_time_expr="明天下午5点"),
        state=projector.state,
        occurred_at=datetime.fromisoformat("2026-09-10T07:00:00+00:00"),
    )
    assert outcome.kind == "issued"
    assert outcome.directive.deadline == datetime.fromisoformat("2026-09-11T17:00:00+08:00")


def test_ambiguous_time_asks_before_issuing() -> None:
    projector, pm, _alice, _bob = _setup_state()
    outcome = _manager().handle_directive_intent(
        message="告诉Alice，必须在明天上午12点之前完成",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("alice",), raw_time_expr="明天上午12点"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "clarify"
    assert "中午 12:00" in outcome.bot_reply
    assert projector.state.directives == {}


def test_missing_target_asks_for_clarification() -> None:
    projector, pm, _alice, _bob = _setup_state()
    outcome = _manager().handle_directive_intent(
        message="明天必须完成",
        actor_id=pm,
        hints=IntentHint(),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "clarify"
    assert "谁" in outcome.bot_reply


def test_multi_task_target_requires_task_clarification() -> None:
    projector, pm, _alice, bob = _setup_state()
    outcome = _manager().handle_directive_intent(
        message="告诉Bob加快进度",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("bob",)),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "clarify"
    assert "收银台前端" in outcome.bot_reply and "全链路压测" in outcome.bot_reply


def test_non_privileged_issuer_is_declined() -> None:
    projector, _pm, alice, _bob = _setup_state()
    outcome = _manager().handle_directive_intent(
        message="告诉Bob加快进度",
        actor_id=alice,
        hints=IntentHint(mentioned_member_ids=("bob",)),
        state=projector.state,
        occurred_at=NOW,
    )
    assert outcome.kind == "declined"
    assert "无下达权限" in outcome.bot_reply
    assert projector.state.directives == {}


def test_member_ack_and_completion_flow() -> None:
    projector, manager, _outcome = _issue_to_alice()

    ack = manager.handle_member_reply("alice", "收到，马上处理", projector.state, NOW)
    assert ack is not None and ack.kind == "acknowledged"
    for event in ack.events:
        projector.apply(event)
    assert ack.notices[0].actor_id == "carol"
    directive = next(iter(projector.state.directives.values()))
    assert directive.status is DirectiveStatus.ACKNOWLEDGED

    done = manager.handle_member_reply("alice", "支付SDK已完成，已交付", projector.state, NOW)
    assert done is not None and done.kind == "completed"
    for event in done.events:
        projector.apply(event)
    directive = next(iter(projector.state.directives.values()))
    assert directive.status is DirectiveStatus.COMPLETED
    assert done.notices[0].actor_id == "carol"


def test_substantive_reply_without_keywords_is_not_intercepted() -> None:
    projector, manager, _outcome = _issue_to_alice()
    reply = manager.handle_member_reply("alice", "联调环境有点问题", projector.state, NOW)
    assert reply is None


def test_directive_survives_replay() -> None:
    """Replaying the full event stream (members + tasks + directive) must rebuild
    the directive state — no side table required."""
    _projector, _manager, outcome = _issue_to_alice()
    fresh = OrgProjector(project_id="proj-dir")
    for event in _base_events():
        fresh.apply(event)
    fresh.apply(outcome.events[0])
    directive = next(iter(fresh.state.directives.values()))
    assert directive.target_id == "alice"
    assert directive.task_id == "task-payment"
    assert directive.status is DirectiveStatus.ISSUED


def test_projector_rejects_unknown_target() -> None:
    projector, pm, _alice, _bob = _setup_state()
    outcome = _manager().handle_directive_intent(
        message="告诉Alice加快",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("stranger",)),
        state=projector.state,
        occurred_at=NOW,
    )
    # The router only reports known members, but a forged event must still be rejected.
    forged = DirectiveIssuedEvent(
        project_id="proj-dir",
        event_id="evt-dir-forged",
        event_type="directive.issued",
        source=EventSource.MESSAGE,
        source_ref="forged",
        actor_id=pm,
        occurred_at=NOW,
        received_at=NOW,
        payload={
            "directive_id": "dir-forged",
            "text": "x",
            "issuer_id": pm,
            "target_id": "stranger",
        },
    )
    with pytest.raises(DomainInvariantError):
        projector.apply(forged)
    assert outcome.kind == "clarify"  # unknown member never reached hint targets


def test_remind_and_escalate_paths() -> None:
    projector, manager, _outcome = _issue_to_alice()

    remind = manager.remind_open_directives(projector.state, "carol", NOW)
    assert remind.kind == "reminded"
    for event in remind.events:
        projector.apply(event)
    directive = next(iter(projector.state.directives.values()))
    assert directive.reminder_count == 1

    # Already reminded and below escalation threshold: the sweep stays quiet.
    quiet = manager.sweep_timeouts(projector.state, NOW + timedelta(minutes=90))
    assert quiet.kind == "none"

    hard = manager.sweep_timeouts(projector.state, NOW + timedelta(minutes=1500))
    assert any(e.event_type == "directive.escalated" for e in hard.events)
    for event in hard.events:
        projector.apply(event)
    directive = next(iter(projector.state.directives.values()))
    assert directive.escalated is True


def test_remind_with_no_open_directives() -> None:
    projector, _pm, _alice, _bob = _setup_state()
    outcome = _manager().remind_open_directives(projector.state, "carol", NOW)
    assert outcome.kind == "none"
    assert "没有待确认" in (outcome.bot_reply or "")


# ------------------------------------------------- multi-turn clarification


def _apply(projector: OrgProjector, outcome) -> None:
    for event in outcome.events:
        projector.apply(event)


def test_ambiguous_time_clarification_closes_loop() -> None:
    """P1 regression: the clarify answer must restore the original draft and
    finally issue the directive — asking alone is not a closed loop."""
    projector, pm, _alice, _bob = _setup_state()
    manager = _manager()
    first = manager.handle_directive_intent(
        message="告诉Alice，必须在明天上午12点之前完成",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("alice",), raw_time_expr="明天上午12点"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert first.kind == "clarify"
    _apply(projector, first)
    assert len(projector.state.pending_directive_clarifications) == 1
    pending = next(iter(projector.state.pending_directive_clarifications.values()))
    assert pending.missing_slots == ("deadline",)
    assert pending.targets == ("alice",)

    resolved = manager.resolve_pending_clarification(pm, "中午12点", projector.state, NOW)
    assert resolved is not None and resolved.kind == "issued"
    _apply(projector, resolved)
    directive = next(iter(projector.state.directives.values()))
    assert directive.target_id == "alice"
    assert directive.deadline == datetime.fromisoformat("2026-09-11T12:00:00+08:00")
    assert projector.state.pending_directive_clarifications == {}


def test_clarification_answer_with_midnight_keyword() -> None:
    projector, pm, _alice, _bob = _setup_state()
    manager = _manager()
    first = manager.handle_directive_intent(
        message="告诉Alice，必须在明天上午12点之前完成",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("alice",), raw_time_expr="明天上午12点"),
        state=projector.state,
        occurred_at=NOW,
    )
    _apply(projector, first)
    resolved = manager.resolve_pending_clarification(pm, "凌晨，半夜就要", projector.state, NOW)
    assert resolved is not None and resolved.kind == "issued"
    _apply(projector, resolved)
    directive = next(iter(projector.state.directives.values()))
    assert directive.deadline == datetime.fromisoformat("2026-09-11T00:00:00+08:00")


def test_missing_target_clarification_restored_by_reply() -> None:
    projector, pm, _alice, _bob = _setup_state()
    manager = _manager()
    first = manager.handle_directive_intent(
        message="必须在明天下午5点之前完成联调",
        actor_id=pm,
        hints=IntentHint(raw_time_expr="明天下午5点"),
        state=projector.state,
        occurred_at=NOW,
    )
    assert first.kind == "clarify"
    _apply(projector, first)

    resolved = manager.resolve_pending_clarification(pm, "Alice", projector.state, NOW)
    assert resolved is not None and resolved.kind == "issued"
    _apply(projector, resolved)
    directive = next(iter(projector.state.directives.values()))
    assert directive.target_id == "alice"
    assert directive.deadline == datetime.fromisoformat("2026-09-11T17:00:00+08:00")


def test_task_clarification_restored_by_reply() -> None:
    projector, pm, _alice, bob = _setup_state()
    manager = _manager()
    first = manager.handle_directive_intent(
        message="告诉Bob，务必尽快推进",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("bob",)),
        state=projector.state,
        occurred_at=NOW,
    )
    assert first.kind == "clarify"
    _apply(projector, first)

    resolved = manager.resolve_pending_clarification(pm, "全链路压测", projector.state, NOW)
    assert resolved is not None and resolved.kind == "issued"
    _apply(projector, resolved)
    directive = next(iter(projector.state.directives.values()))
    assert directive.task_id == "task-qa"
    assert directive.target_id == "bob"


def test_clarification_can_be_cancelled() -> None:
    projector, pm, _alice, _bob = _setup_state()
    manager = _manager()
    first = manager.handle_directive_intent(
        message="告诉Alice，必须在明天上午12点之前完成",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("alice",), raw_time_expr="明天上午12点"),
        state=projector.state,
        occurred_at=NOW,
    )
    _apply(projector, first)
    cancelled = manager.resolve_pending_clarification(pm, "算了，先不下", projector.state, NOW)
    assert cancelled is not None and cancelled.kind == "cancelled"
    _apply(projector, cancelled)
    assert projector.state.directives == {}
    assert projector.state.pending_directive_clarifications == {}


def test_pending_clarification_survives_replay() -> None:
    """Restart mid-clarify: a replayed event log restores the pending draft and
    the issuer's later answer still issues the original directive."""
    projector, pm, _alice, _bob = _setup_state()
    manager = _manager()
    first = manager.handle_directive_intent(
        message="告诉Alice，必须在明天上午12点之前完成",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("alice",), raw_time_expr="明天上午12点"),
        state=projector.state,
        occurred_at=NOW,
    )
    _apply(projector, first)

    replayed = OrgProjector(project_id="proj-dir")
    replayed.replay(_base_events() + first.events)
    assert len(replayed.state.pending_directive_clarifications) == 1

    resolved = manager.resolve_pending_clarification(pm, "中午12点", replayed.state, NOW)
    assert resolved is not None and resolved.kind == "issued"
    for event in resolved.events:
        replayed.apply(event)
    directive = next(iter(replayed.state.directives.values()))
    assert directive.deadline == datetime.fromisoformat("2026-09-11T12:00:00+08:00")
    assert replayed.state.pending_directive_clarifications == {}


def test_multiple_open_directives_require_disambiguation() -> None:
    projector, pm, alice, _bob = _setup_state()
    # Alice owns two tasks in this scenario.
    for event in [
        TaskCreatedEvent(
            project_id="proj-dir",
            event_id="evt-t-9",
            event_type="task.created",
            source=EventSource.TASK,
            source_ref="setup",
            occurred_at=NOW,
            received_at=NOW,
            payload={"task_id": "task-report", "title": "周报系统", "owner_id": "alice"},
        )
    ]:
        projector.apply(event)
    manager = _manager()
    first = manager.handle_directive_intent(
        message="推进支付SDK",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("alice",), mentioned_task_ids=("task-payment",)),
        state=projector.state,
        occurred_at=NOW,
    )
    _apply(projector, first)
    second = manager.handle_directive_intent(
        message="推进周报系统",
        actor_id=pm,
        hints=IntentHint(mentioned_member_ids=("alice",), mentioned_task_ids=("task-report",)),
        state=projector.state,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _apply(projector, second)
    assert len(projector.state.directives) == 2

    vague = manager.handle_member_reply(alice, "收到", projector.state, NOW)
    assert vague is not None and vague.kind == "none"
    assert "多条进行中的指令" in (vague.bot_reply or "")

    bound = manager.handle_member_reply(alice, "收到，支付SDK接入那条", projector.state, NOW)
    assert bound is not None and bound.kind == "acknowledged"
    for event in bound.events:
        projector.apply(event)
    statuses = {d.task_id: d.status for d in projector.state.directives.values()}
    assert statuses["task-payment"] is DirectiveStatus.ACKNOWLEDGED
    assert statuses["task-report"] is DirectiveStatus.ISSUED

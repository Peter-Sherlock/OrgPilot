"""New evidence starts a new coordination episode; old history cannot authorize it."""

from datetime import datetime, timedelta

import pytest

from orgpilot.coordination.approval import ApprovalManager
from orgpilot.coordination.ledger import CaseLedger
from orgpilot.domain.enums import ActionType, AgentTerminationReason, CoordinationCaseStatus
from orgpilot.domain.models import ActionCommand, CoordinationAction, CoordinationCase
from orgpilot.gateway.service import GatewayService
from orgpilot.storage.database import Database
from tests.conftest import make_event
from tests.test_approval_manager import _fixture
from tests.test_case_ledger import _claim, _state

NOW = datetime.fromisoformat("2026-09-01T10:00:00+08:00")
CLOSED = NOW + timedelta(hours=1)
CASE_ID = "case:task-health:t1"
TERMINAL = [
    CoordinationCaseStatus.RESOLVED,
    CoordinationCaseStatus.CANCELLED,
    CoordinationCaseStatus.ESCALATED,
]


def _closed_ledger(status):
    state = _state(_claim("old", None, occurred_at=NOW))
    ledger = CaseLedger()
    ledger.reconcile(state, (), NOW)
    ledger.transition(
        CASE_ID, status, CLOSED, terminal_reason="previous episode ended", candidate_actions=()
    )
    return ledger


@pytest.mark.parametrize("status", TERMINAL)
def test_newer_risk_reopens_terminal_case_and_resets_only_episode_metadata(status):
    ledger = _closed_ledger(status)
    command = ActionCommand(
        command_id="old-question",
        action_id="ask",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("alice",),
        idempotency_key="old",
        created_at=NOW,
    )
    ledger.record_command(CASE_ID, command, CLOSED)
    case = ledger.get_case(CASE_ID)
    case.round_count = 3
    action = CoordinationAction(
        action_id="ask",
        action_type=ActionType.ASK_RECOVERY_ESTIMATE,
        targets=("alice",),
        reason_refs=("new-risk",),
        expected_effect="ask again",
    )
    assert ledger.is_action_duplicate(CASE_ID, action)

    new_time = CLOSED + timedelta(minutes=1)
    ledger.reconcile(_state(_claim("new", None, occurred_at=new_time)), (), new_time)

    assert case.status is CoordinationCaseStatus.OPEN
    assert case.terminal_reason is None
    assert case.waiting_for is None and case.waiting_until is None
    assert case.round_count == 0
    assert case.generation == 1
    assert case.episode_command_start == 1
    assert case.current_approval_id is None
    assert case.created_at == NOW
    assert case.updated_at == new_time
    assert case.evidence_claim_ids == ("new",)
    assert case.missing_information == ("expected_completion",)
    assert case.executed_commands == (command,)
    assert not ledger.is_action_duplicate(CASE_ID, action)
    second = command.model_copy(update={"command_id": "new-question", "created_at": new_time})
    ledger.record_command(CASE_ID, second, new_time)
    assert ledger.is_action_duplicate(CASE_ID, action)


@pytest.mark.parametrize("status", TERMINAL)
@pytest.mark.parametrize("claim_time", [NOW, CLOSED])
def test_old_or_equal_time_risk_does_not_reopen_a_terminal_case(status, claim_time):
    ledger = _closed_ledger(status)
    state = _state(_claim("late-arriving-old", None, occurred_at=claim_time))
    before = ledger.get_case(CASE_ID).model_dump(mode="json")
    ledger.reconcile(state, (), CLOSED + timedelta(days=1))
    assert ledger.get_case(CASE_ID).model_dump(mode="json") == before


def test_completion_and_unrelated_task_edits_do_not_reopen_case():
    ledger = _closed_ledger(CoordinationCaseStatus.RESOLVED)
    state = _state(_claim("old", None, occurred_at=NOW))
    state.tasks["t1"].last_update_at = CLOSED + timedelta(hours=1)
    ledger.reconcile(state, (), CLOSED + timedelta(hours=1))
    assert ledger.get_case(CASE_ID).status is CoordinationCaseStatus.RESOLVED
    state = _state(_claim("new", None, occurred_at=CLOSED + timedelta(hours=2)))
    state.tasks["t1"].workflow_status = "done"
    ledger.reconcile(state, (), CLOSED + timedelta(hours=2))
    assert ledger.get_case(CASE_ID).status is CoordinationCaseStatus.RESOLVED


def test_missing_closure_time_cannot_prove_newer_risk():
    ledger = _closed_ledger(CoordinationCaseStatus.RESOLVED)
    ledger.get_case(CASE_ID).updated_at = None
    state = _state(_claim("new", None, occurred_at=CLOSED + timedelta(hours=1)))
    ledger.reconcile(state, (), CLOSED + timedelta(hours=1))
    assert ledger.get_case(CASE_ID).status is CoordinationCaseStatus.RESOLVED


@pytest.mark.parametrize("first_decision", ["approve", "reject", "cancel"])
async def test_second_approval_survives_restart_and_ignores_historical_decisions(
    tmp_path, first_decision
):
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'episodes.db').as_posix()}"
    db = Database(db_url)
    await db.init_db()
    service = GatewayService(db)
    events = [
        make_event(
            "alice",
            "member.registered",
            {"member_id": "alice", "display_name": "Alice", "role": "engineer"},
        ),
        make_event(
            "carol",
            "member.registered",
            {"member_id": "carol", "display_name": "Carol", "role": "pm"},
        ),
        make_event(
            "task",
            "task.created",
            {
                "task_id": "t1",
                "title": "API",
                "owner_id": "alice",
                "workflow_status": "doing",
                "deadline": "2026-09-01T12:00:00+08:00",
            },
            minute=1,
        ),
        make_event(
            "risk-1",
            "task.health_reported",
            {
                "task_id": "t1",
                "health_status": "delayed",
                "expected_completion": "2026-09-02T12:00:00+08:00",
                "confidence": 0.9,
            },
            actor_id="alice",
            minute=2,
        ),
    ]
    try:
        await service.ingest_raw_events("test-project", [e.model_dump(mode="json") for e in events])
        agent = await service.get_or_replay_agent("test-project")
        await service.run_agent_turn(agent, [], NOW)
        old = agent.approval_manager.get_pending_requests()[0]
        if first_decision == "approve":
            agent.approval_manager.approve(old.approval_id, "carol", NOW + timedelta(minutes=1))
        elif first_decision == "reject":
            agent.approval_manager.reject(
                old.approval_id, "carol", "keep deadline", NOW + timedelta(minutes=1)
            )
        if first_decision == "cancel":
            recovery = make_event(
                "recovered",
                "task.health_reported",
                {"task_id": "t1", "health_status": "on_track", "confidence": 0.9},
                actor_id="alice",
                minute=3,
            )
            await service.event_store.append(recovery)
            await service.run_agent_turn(agent, [recovery], NOW + timedelta(minutes=1))
        else:
            await service.run_agent_turn(agent, [], NOW + timedelta(minutes=1))
        old_snapshot = old.model_dump(mode="json")
    finally:
        await db.close()

    db = Database(db_url)
    await db.init_db()
    service = GatewayService(db)
    later = NOW + timedelta(hours=2)
    new_risk = events[-1].model_copy(
        update={
            "event_id": "risk-2",
            "occurred_at": later,
            "received_at": later,
            "payload": events[-1].payload.model_copy(
                update={"expected_completion": NOW + timedelta(days=2)}
            ),
        }
    )
    try:
        await service.ingest_raw_events("test-project", [new_risk.model_dump(mode="json")])
        agent = await service.get_or_replay_agent("test-project")
        trace, _ = await service.run_agent_turn(agent, [], later)
        assert trace.termination_reason is AgentTerminationReason.WAITING_APPROVAL
        requests = agent.approval_manager.get_all_requests()
        assert len(requests) == 2
        new = next(r for r in requests if r.approval_id != old.approval_id)
        assert new.approval_id != old.approval_id
        assert (
            agent.approval_manager.get_request(old.approval_id).model_dump(mode="json")
            == old_snapshot
        )

        # Rebuild the agent again: the turn counter resets but the episode and
        # current approval binding must not. A historical rejection cannot fire.
        agent = await service.get_or_replay_agent("test-project")
        if first_decision == "cancel":
            # Clicking a stale card cannot authorize work in the new episode.
            agent.approval_manager.approve(old.approval_id, "carol", later)
            old_snapshot = agent.approval_manager.get_request(old.approval_id).model_dump(
                mode="json"
            )
        trace, generated = await service.run_agent_turn(agent, [], later + timedelta(minutes=1))
        assert trace.termination_reason is AgentTerminationReason.WAITING_APPROVAL
        assert not generated
        agent.approval_manager.approve(new.approval_id, "carol", later + timedelta(minutes=2))
        await service.run_agent_turn(agent, [], later + timedelta(minutes=2))
        restored = await service.get_or_replay_agent("test-project")
        assert restored.state.tasks["t1"].deadline == NOW + timedelta(days=2)
        assert restored.case_ledger.get_case(CASE_ID).status is CoordinationCaseStatus.RESOLVED
        assert restored.approval_manager.get_request(new.approval_id).consumed
        assert (
            restored.approval_manager.get_request(old.approval_id).model_dump(mode="json")
            == old_snapshot
        )
        updates = [
            e
            for e in await service.event_store.get_events("test-project")
            if e.event_type == "task.updated"
        ]
        assert len(updates) == (2 if first_decision == "approve" else 1)
        assert len({e.event_id for e in updates}) == len(updates)
    finally:
        await db.close()


def test_old_case_snapshots_load_with_first_episode_defaults():
    old = CoordinationCase(
        case_id=CASE_ID, source_task_id="t1", status=CoordinationCaseStatus.OPEN
    ).model_dump()
    for name in ("generation", "episode_command_start", "current_approval_id"):
        old.pop(name, None)
    restored = CoordinationCase.model_validate(old)
    assert restored.generation == 0
    assert restored.episode_command_start == 0
    assert restored.current_approval_id is None


def test_approval_creation_is_idempotent_and_preserves_prior_consumption():
    manager, action, command = _fixture()
    first = manager.create_request(CASE_ID, action, command, "carol", NOW)
    manager.approve(first.approval_id, "carol", NOW)
    manager.consume(first.approval_id, NOW)
    old_snapshot = first.model_dump(mode="json")
    assert manager.create_request(CASE_ID, action, command, "carol", NOW) is first
    assert first.model_dump(mode="json") == old_snapshot

    new_command = command.model_copy(update={"command_id": "cmd:2", "idempotency_key": "idem:2"})
    second = manager.create_request(CASE_ID, action, new_command, "carol", NOW)
    assert second.approval_id != first.approval_id
    assert manager.get_request(first.approval_id).model_dump(mode="json") == old_snapshot

    restored = ApprovalManager()
    for request in manager.get_all_requests():
        restored._requests[request.approval_id] = request.model_copy(deep=True)
    third = restored.create_request(
        CASE_ID, action, new_command.model_copy(update={"command_id": "cmd:3"}), "carol", NOW
    )
    assert len({first.approval_id, second.approval_id, third.approval_id}) == 3

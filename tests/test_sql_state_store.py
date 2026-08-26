"""Tests for SqlStateStore persistence and loading of state, cases, and approvals."""

from datetime import datetime, timedelta

import pytest

from orgpilot.domain.enums import (
    ActionType,
    ApprovalStatus,
    CoordinationCaseStatus,
    HealthStatus,
    WorkflowStatus,
)
from orgpilot.domain.models import (
    ActionCommand,
    ApprovalRequest,
    CoordinationAction,
    CoordinationCase,
    MemberState,
    OrgState,
    TaskState,
)
from orgpilot.storage.database import Database
from orgpilot.storage.state_store import SqlStateStore

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


@pytest.fixture
async def db() -> Database:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init_db()
    yield database
    await database.close()


async def test_sql_state_store_state_lifecycle(db: Database) -> None:
    store = SqlStateStore(db)
    assert await store.load_state("p1") is None

    state = OrgState(
        project_id="p1",
        members={
            "alice": MemberState(
                member_id="alice",
                display_name="Alice",
                role="backend",
                source_event_id="evt-mem-1",
                last_update_at=NOW,
            )
        },
        tasks={
            "t1": TaskState(
                task_id="t1",
                title="Task 1",
                owner_id="alice",
                workflow_status=WorkflowStatus.DOING,
                health_status=HealthStatus.DELAYED,
                health_conflict=False,
                dependencies=(),
                source_event_ids=("evt-task-1",),
                last_update_at=NOW,
            )
        },
    )
    await store.save_state(state)

    loaded = await store.load_state("p1")
    assert loaded is not None
    assert loaded.project_id == "p1"
    assert "alice" in loaded.members
    assert loaded.tasks["t1"].health_status is HealthStatus.DELAYED


async def test_sql_state_store_cases_lifecycle(db: Database) -> None:
    store = SqlStateStore(db)
    case = CoordinationCase(
        case_id="case:1",
        source_task_id="t1",
        status=CoordinationCaseStatus.WAITING_FOR_APPROVAL,
        waiting_for="carol",
        candidate_actions=(
            CoordinationAction(
                action_id="act:1",
                action_type=ActionType.PROPOSE_RESCHEDULE,
                targets=("carol",),
                reason_refs=("evt-1",),
                expected_effect="reschedule",
            ),
        ),
    )
    await store.save_cases("p1", [case])

    loaded_cases = await store.load_cases("p1")
    assert len(loaded_cases) == 1
    assert loaded_cases[0].case_id == "case:1"
    assert loaded_cases[0].status is CoordinationCaseStatus.WAITING_FOR_APPROVAL


async def test_sql_state_store_approvals_lifecycle(db: Database) -> None:
    store = SqlStateStore(db)
    req = ApprovalRequest(
        approval_id="appr:1",
        case_id="case:1",
        action_id="act:1",
        action_type=ActionType.PROPOSE_RESCHEDULE,
        proposed_command=ActionCommand(
            command_id="cmd:1",
            action_id="act:1",
            action_type=ActionType.PROPOSE_RESCHEDULE,
            targets=("carol",),
            idempotency_key="idem:1",
            created_at=NOW,
        ),
        approver_id="carol",
        status=ApprovalStatus.PENDING,
        expires_at=NOW + timedelta(days=1),
    )
    await store.save_approvals("p1", [req])

    loaded = await store.load_approvals("p1")
    assert len(loaded) == 1
    assert loaded[0].approval_id == "appr:1"
    assert loaded[0].status is ApprovalStatus.PENDING

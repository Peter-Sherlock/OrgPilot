"""Tests for SqlEventStore idempotency, conflict rejection, and query operations."""

from datetime import UTC, datetime

import pytest

from orgpilot.domain.errors import DuplicateEventConflict
from orgpilot.events.log import AppendResult
from orgpilot.events.models import (
    EventSource,
    MemberRegisteredEvent,
    MemberRegisteredPayload,
)
from orgpilot.storage.database import Database
from orgpilot.storage.event_store import SqlEventStore

NOW = datetime.fromisoformat("2026-09-10T10:00:00+08:00")


@pytest.fixture
async def db() -> Database:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init_db()
    yield database
    await database.close()


async def test_sql_event_store_append_and_duplicate(db: Database) -> None:
    store = SqlEventStore(db)
    event = MemberRegisteredEvent(
        project_id="proj-1",
        event_id="evt-mem-alice",
        event_type="member.registered",
        source=EventSource.HUMAN,
        source_ref="setup",
        occurred_at=NOW,
        received_at=NOW,
        payload=MemberRegisteredPayload(member_id="alice", display_name="Alice", role="backend"),
    )

    # First append: SUCCESS
    res1 = await store.append(event)
    assert res1 is AppendResult.APPENDED
    assert await store.count("proj-1") == 1

    # Exact duplicate append: DUPLICATE
    res2 = await store.append(event)
    assert res2 is AppendResult.DUPLICATE
    assert await store.count("proj-1") == 1


async def test_sql_event_store_rejects_conflict(db: Database) -> None:
    store = SqlEventStore(db)
    event1 = MemberRegisteredEvent(
        project_id="proj-1",
        event_id="evt-same-id",
        event_type="member.registered",
        source=EventSource.HUMAN,
        source_ref="setup",
        occurred_at=NOW,
        received_at=NOW,
        payload=MemberRegisteredPayload(member_id="alice", display_name="Alice", role="backend"),
    )
    await store.append(event1)

    # Different payload with same event_id: CONFLICT ERROR
    event2 = MemberRegisteredEvent(
        project_id="proj-1",
        event_id="evt-same-id",
        event_type="member.registered",
        source=EventSource.HUMAN,
        source_ref="setup",
        occurred_at=NOW,
        received_at=NOW,
        payload=MemberRegisteredPayload(member_id="bob", display_name="Bob", role="frontend"),
    )
    with pytest.raises(DuplicateEventConflict, match="already exists with different payload"):
        await store.append(event2)


async def test_sql_event_store_query_and_ordering(db: Database) -> None:
    store = SqlEventStore(db)
    event1 = MemberRegisteredEvent(
        project_id="proj-2",
        event_id="evt-1",
        event_type="member.registered",
        source=EventSource.HUMAN,
        source_ref="setup",
        occurred_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
        received_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
        payload=MemberRegisteredPayload(member_id="alice", display_name="Alice", role="backend"),
    )
    event2 = MemberRegisteredEvent(
        project_id="proj-2",
        event_id="evt-2",
        event_type="member.registered",
        source=EventSource.HUMAN,
        source_ref="setup",
        occurred_at=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
        received_at=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
        payload=MemberRegisteredPayload(member_id="bob", display_name="Bob", role="frontend"),
    )
    await store.append(event2)
    await store.append(event1)

    # Retrieval should be strictly chronological (evt-1 first, then evt-2)
    events = await store.get_events("proj-2")
    assert len(events) == 2
    assert events[0].event_id == "evt-1"
    assert events[1].event_id == "evt-2"

    # Filter with since
    filtered = await store.get_events("proj-2", since=datetime(2026, 9, 10, 9, 30, tzinfo=UTC))
    assert len(filtered) == 1
    assert filtered[0].event_id == "evt-2"

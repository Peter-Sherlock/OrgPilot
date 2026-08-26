"""Append-only log guarantees idempotency without hiding event collisions."""

import pytest
from pydantic import ValidationError

from orgpilot.domain.errors import DuplicateEventConflict
from orgpilot.events.log import AppendResult, InMemoryEventLog
from orgpilot.events.models import parse_event
from tests.conftest import event_data, make_event


def test_identical_duplicate_event_is_idempotent() -> None:
    event = make_event(
        "evt-member",
        "member.registered",
        {"member_id": "alice", "display_name": "Alice", "role": "backend"},
    )
    log = InMemoryEventLog()

    assert log.append(event) is AppendResult.APPENDED
    assert log.append(event) is AppendResult.DUPLICATE
    assert len(log) == 1
    assert log.all() == (event,)


def test_reused_event_id_with_different_content_is_rejected() -> None:
    original = make_event(
        "evt-member",
        "member.registered",
        {"member_id": "alice", "display_name": "Alice", "role": "backend"},
    )
    collision = make_event(
        "evt-member",
        "member.registered",
        {"member_id": "bob", "display_name": "Bob", "role": "frontend"},
    )
    log = InMemoryEventLog()
    log.append(original)

    with pytest.raises(DuplicateEventConflict, match="reused"):
        log.append(collision)


def test_event_requires_timezone_and_forbids_unknown_fields() -> None:
    naive = event_data(
        "evt-member",
        "member.registered",
        {"member_id": "alice", "display_name": "Alice", "role": "backend"},
    )
    naive["occurred_at"] = "2026-09-01T09:00:00"
    with pytest.raises(ValidationError, match="explicit timezone"):
        parse_event(naive)

    extra = event_data(
        "evt-member",
        "member.registered",
        {"member_id": "alice", "display_name": "Alice", "role": "backend"},
    )
    extra["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        parse_event(extra)


def test_event_is_frozen() -> None:
    event = make_event(
        "evt-member",
        "member.registered",
        {"member_id": "alice", "display_name": "Alice", "role": "backend"},
    )

    with pytest.raises(ValidationError, match="frozen"):
        event.source_ref = "changed"  # type: ignore[misc]

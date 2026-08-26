"""Append-only event log abstraction used by deterministic replays."""

from enum import StrEnum

from orgpilot.domain.errors import DuplicateEventConflict
from orgpilot.events.models import OrgEvent


class AppendResult(StrEnum):
    APPENDED = "appended"
    DUPLICATE = "duplicate"


class InMemoryEventLog:
    """Minimal P0 event log with idempotency and collision detection."""

    def __init__(self) -> None:
        self._events: list[OrgEvent] = []
        self._by_id: dict[str, OrgEvent] = {}

    def append(self, event: OrgEvent) -> AppendResult:
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            if existing == event:
                return AppendResult.DUPLICATE
            raise DuplicateEventConflict(
                f"event id {event.event_id!r} was reused with different content"
            )

        self._events.append(event)
        self._by_id[event.event_id] = event
        return AppendResult.APPENDED

    def all(self) -> tuple[OrgEvent, ...]:
        return tuple(self._events)

    def __len__(self) -> int:
        return len(self._events)

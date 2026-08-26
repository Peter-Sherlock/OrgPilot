"""Shared factories for deterministic domain tests."""

from collections.abc import Mapping
from typing import Any

from orgpilot.events.models import OrgEvent, parse_event


def event_data(
    event_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    project_id: str = "test-project",
    actor_id: str | None = None,
    source: str = "scenario",
    source_ref: str | None = None,
    minute: int = 0,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "project_id": project_id,
        "event_id": event_id,
        "event_type": event_type,
        "source": source,
        "source_ref": source_ref or f"test:{event_id}",
        "occurred_at": f"2026-09-01T09:{minute:02d}:00+08:00",
        "received_at": f"2026-09-01T09:{minute:02d}:01+08:00",
        "payload": dict(payload),
    }
    if actor_id is not None:
        data["actor_id"] = actor_id
    return data


def make_event(*args: Any, **kwargs: Any) -> OrgEvent:
    return parse_event(event_data(*args, **kwargs))

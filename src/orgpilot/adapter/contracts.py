"""Canonical adapter payload contract shared by Mock and real adapters.

Every ``ActionCommand.payload`` crossing the adapter boundary is parsed through
this module, so Mock and Feishu adapters cannot drift apart on field names or
fallback behavior. Canonical keys are listed first; legacy aliases remain
readable for one release but producers must emit canonical keys.

Required fields are fail-closed: a missing deadline raises
:class:`PayloadContractError` instead of silently defaulting to ``now()``.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

TEXT_KEY = "text"
DEADLINE_KEY = "new_deadline"

# Canonical key first; aliases kept for payloads persisted by older versions.
_TEXT_KEYS = (TEXT_KEY, "clarification_text", "inquiry_text")
_DEADLINE_KEYS = (DEADLINE_KEY, "deadline", "proposed_deadline")


class PayloadContractError(ValueError):
    """An ActionCommand payload violates the canonical adapter contract."""


class PermanentDeliveryError(RuntimeError):
    """The platform rejected the delivery logically (bad target, bad request).

    Retrying cannot fix a permanent rejection — the outbox dead-letters these
    immediately instead of burning retries; transport-level failures
    (timeouts, 5xx) remain retryable.
    """


def parse_text(payload: dict[str, Any]) -> str | None:
    """Returns the canonical message text, or None when the payload carries none."""
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def parse_deadline(payload: dict[str, Any]) -> datetime | None:
    """Parses the canonical deadline field, or None when the payload carries none."""
    for key in _DEADLINE_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise PayloadContractError(
                    f"payload field {key!r} is not an ISO-8601 datetime: {value!r}"
                ) from exc
    return None


@dataclass(frozen=True)
class TextMessage:
    """Canonical text-message payload for ASK_* / SEND_DIRECTIVE commands."""

    text: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TextMessage | None":
        text = parse_text(payload)
        return cls(text=text) if text is not None else None


@dataclass(frozen=True)
class DeadlineUpdate:
    """Canonical UPDATE_TASK payload. Fail-closed: never defaults to now()."""

    task_id: str
    new_deadline: datetime

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DeadlineUpdate":
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise PayloadContractError("UPDATE_TASK payload requires a non-empty 'task_id'")
        deadline = parse_deadline(payload)
        if deadline is None:
            raise PayloadContractError(
                "UPDATE_TASK payload requires 'new_deadline'; refusing to default to now()"
            )
        return cls(task_id=task_id, new_deadline=deadline)


@dataclass(frozen=True)
class RescheduleProposal:
    """Canonical PROPOSE_RESCHEDULE approval payload."""

    task_id: str
    new_deadline: datetime
    task_title: str
    impacted_tasks: list[str]
    risk_level: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RescheduleProposal":
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise PayloadContractError("PROPOSE_RESCHEDULE payload requires a non-empty 'task_id'")
        deadline = parse_deadline(payload)
        if deadline is None:
            raise PayloadContractError("PROPOSE_RESCHEDULE payload requires 'new_deadline'")
        title = payload.get("task_title") or payload.get("title")
        impacted = payload.get("impacted_tasks")
        return cls(
            task_id=task_id,
            new_deadline=deadline,
            task_title=str(title) if title else task_id,
            impacted_tasks=[str(t) for t in impacted] if impacted else [],
            risk_level=str(payload.get("risk_level", "HIGH")),
        )


def format_deadline_for_card(deadline: datetime) -> str:
    """Renders a deadline for card display without locale-dependent output."""
    return deadline.strftime("%Y-%m-%d %H:%M")

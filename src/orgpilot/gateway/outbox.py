"""Outbox dispatcher: reliable outbound delivery with retry, recovery, and a
delivery ledger that keeps the event log honest about what actually shipped."""

import asyncio
from datetime import UTC, datetime, timedelta

from orgpilot.adapter.base import CollaborationAdapter
from orgpilot.adapter.contracts import PermanentDeliveryError
from orgpilot.domain.models import ActionCommand
from orgpilot.events.models import (
    DirectiveDeliveredEvent,
    DirectiveDeliveryFailedEvent,
    EventSource,
    OrgEvent,
)
from orgpilot.storage.outbox_store import SqlOutboxStore


def _is_permanent(exc: BaseException, _depth: int = 0) -> bool:
    """True when retrying this delivery failure cannot help.

    Covers typed platform rejections and HTTP 4xx (except timeouts 408 and
    rate-limit 429), walking the exception chain the adapter may have wrapped.
    """
    if _depth > 6:
        return False
    if isinstance(exc, PermanentDeliveryError):
        return True
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, int) and 400 <= status_code < 500 and status_code not in (408, 429):
        return True
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_permanent(cause, _depth + 1)
    return False


class OutboxDispatcher:
    """Drains pending outbound commands through an adapter and records outcomes.

    Delivery semantics: at-least-once with linear backoff. Commands already
    relayed in the same turn are recorded as delivered without re-execution;
    commands that failed transport stay pending and are re-driven by
    :meth:`execute_due` (called inline after enqueue and by the startup/sweep
    recovery loops, which makes "crashed between persist and send" recover).
    """

    def __init__(
        self,
        store: SqlOutboxStore,
        max_attempts: int = 3,
        retry_seconds: int = 30,
    ) -> None:
        self.store = store
        self.max_attempts = max_attempts
        self.retry_seconds = retry_seconds

    async def record_pre_delivered(self, project_id: str, command: ActionCommand) -> list[OrgEvent]:
        """Persists a command the adapter already confirmed within this turn.

        Used by the agent-loop fast path, where execution is inline and the
        outbox exists to make the delivery ledger durable and inspectable.
        """
        now = datetime.now(UTC)
        await self.store.record_completed(project_id, command, now)
        return self._delivery_event(
            project_id, command, success=True, attempts=1, error=None, now=now
        )

    async def enqueue_and_deliver(
        self, project_id: str, command: ActionCommand, adapter: CollaborationAdapter
    ) -> list[OrgEvent]:
        """Persists first, then attempts immediate delivery; failures stay recoverable."""
        now = datetime.now(UTC)
        await self.store.enqueue(project_id, command, now)
        return await self.execute_due(project_id, adapter, now=now)

    async def execute_due(
        self,
        project_id: str,
        adapter: CollaborationAdapter,
        *,
        now: datetime | None = None,
    ) -> list[OrgEvent]:
        """Delivers every due pending command, settling delivered/retrying/dead."""
        now = now or datetime.now(UTC)
        due = await self.store.due_commands(project_id, now)
        events: list[OrgEvent] = []
        for command in due:
            events.extend(await self._attempt(project_id, command, adapter, now))
        return events

    async def _attempt(
        self,
        project_id: str,
        command: ActionCommand,
        adapter: CollaborationAdapter,
        now: datetime,
    ) -> list[OrgEvent]:
        attempts = await self.store.attempts_of(project_id, command.idempotency_key) + 1
        try:
            # The adapter interface is synchronous and may block on IO.
            await asyncio.to_thread(adapter.execute, command)
        except Exception as exc:
            # Transport failures are recoverable (backoff, then dead-letter);
            # permanent rejections (bad target, 4xx) dead-letter immediately —
            # burning retries on a request that can never succeed is noise.
            dead = _is_permanent(exc) or attempts >= self.max_attempts
            retry_at = now if dead else now + timedelta(seconds=self.retry_seconds * attempts)
            await self.store.settle(
                project_id,
                command.idempotency_key,
                delivered=False,
                attempts=attempts,
                error=str(exc),
                retry_at=retry_at,
                now=now,
                dead=dead,
            )
            if dead:
                return self._delivery_event(
                    project_id, command, success=False, attempts=attempts, error=str(exc), now=now
                )
            return []

        await self.store.settle(
            project_id,
            command.idempotency_key,
            delivered=True,
            attempts=attempts,
            error=None,
            retry_at=now,
            now=now,
        )
        return self._delivery_event(
            project_id, command, success=True, attempts=attempts, error=None, now=now
        )

    def _delivery_event(
        self,
        project_id: str,
        command: ActionCommand,
        *,
        success: bool,
        attempts: int,
        error: str | None,
        now: datetime,
    ) -> list[OrgEvent]:
        """Builds directive delivery ledger events; non-directive transports are
        tracked by the outbox table alone."""
        directive_id = command.payload.get("directive_id")
        if not directive_id or not command.targets:
            return []
        target_id = command.targets[0]
        event_id = f"evt-directive-delivery-{command.idempotency_key}"
        common = {
            "project_id": project_id,
            "event_id": event_id,
            "source_ref": f"command:{command.command_id}",
            "occurred_at": now,
            "received_at": now,
        }
        if success:
            return [
                DirectiveDeliveredEvent(
                    event_type="directive.delivered",
                    source=EventSource.TASK,
                    payload={
                        "directive_id": directive_id,
                        "command_id": command.command_id,
                        "target_id": target_id,
                        "attempts": attempts,
                    },
                    **common,
                )
            ]
        return [
            DirectiveDeliveryFailedEvent(
                event_type="directive.delivery_failed",
                source=EventSource.TASK,
                payload={
                    "directive_id": directive_id,
                    "command_id": command.command_id,
                    "target_id": target_id,
                    "error": error or "unknown transport error",
                    "attempts": attempts,
                },
                **common,
            )
        ]

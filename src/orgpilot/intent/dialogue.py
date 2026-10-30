"""Dialogue state store keyed by (project, sender) (ADR-0008 layer 0).

Two state kinds live here and NOWHERE else for routing decisions:

- ``PendingClarification``: an open follow-up question (from a sync probe or from
  the router itself) with the partially-completed claim it wants to finish;
- ``PendingSyncConfirmation``: a sync confirmation card awaiting yes/no.

Sync probes register their clarifications here so the router has a single source
of truth for "what is the bot waiting for from this person".

The store itself is synchronous and in-memory; the router checkpoints each
project's entries to SQL at the end of every turn and restores them on first
access after a restart (ADR-0013), via :meth:`DialogueStateStore.export_project`
and :meth:`DialogueStateStore.restore_project`. Expiry stays a pure function of
the persisted ``created_at`` and the injected clock, so a restored entry expires
exactly when it would have without the restart.
"""

from datetime import datetime, timedelta

from pydantic import Field

from orgpilot.config import DEFAULT_MAX_TOPIC_REASKS
from orgpilot.domain.clock import Clock, SystemClock
from orgpilot.extraction.models import StrictModel

DEFAULT_CLARIFICATION_TTL = timedelta(hours=24)
DEFAULT_CONFIRMATION_TTL = timedelta(minutes=30)
DEFAULT_MAX_CLARIFICATION_ROUNDS = 3

CLARIFICATION_SOURCE_PROBE = "probe"
CLARIFICATION_SOURCE_CLARIFY = "clarify"


class PendingClaimDraft(StrictModel):
    """The partially-completed claim a pending clarification wants to finish."""

    task_id: str | None = None
    health_status: str | None = None
    expected_completion: datetime | None = None
    blocker: str | None = None


class PendingClarification(StrictModel):
    """An open question the bot asked and the draft claims it wants to complete.

    ``task_ids`` holds EVERY task the question targets (a sync probe targets all
    tasks the member owns, 3a.1); ``claims`` keeps the partially-merged draft per
    task and ``missing`` the still-missing slots per task.
    """

    question: str
    task_ids: list[str] = Field(default_factory=list)
    claims: dict[str, PendingClaimDraft] = Field(default_factory=dict)
    missing: dict[str, list[str]] = Field(default_factory=dict)
    asked_rounds: int = 0
    # Topic-switch re-asks (3a.2), counted SEPARATELY from asked_rounds: the
    # combined asked_rounds + reask_count decides when the bot stops asking.
    reask_count: int = 0
    created_at: datetime
    source: str = CLARIFICATION_SOURCE_CLARIFY


class PendingSyncConfirmation(StrictModel):
    """A confirmation card for a side-effectful sync, awaiting yes/no."""

    initiated_by: str
    member_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class DialogueSnapshot(StrictModel):
    """One project's dialogue state keyed by sender, as persisted (ADR-0013)."""

    clarifications: dict[str, PendingClarification] = Field(default_factory=dict)
    confirmations: dict[str, PendingSyncConfirmation] = Field(default_factory=dict)


class DialogueStateStore:
    """Per (project_id, sender_id) dialogue state with TTL-based expiry."""

    def __init__(
        self,
        clarification_ttl: timedelta = DEFAULT_CLARIFICATION_TTL,
        confirmation_ttl: timedelta = DEFAULT_CONFIRMATION_TTL,
        max_clarification_rounds: int = DEFAULT_MAX_CLARIFICATION_ROUNDS,
        max_topic_reasks: int = DEFAULT_MAX_TOPIC_REASKS,
        clock: Clock | None = None,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self.clarification_ttl = clarification_ttl
        self.confirmation_ttl = confirmation_ttl
        self.max_clarification_rounds = max_clarification_rounds
        self.max_topic_reasks = max_topic_reasks
        self._clarifications: dict[tuple[str, str], PendingClarification] = {}
        self._confirmations: dict[tuple[str, str], PendingSyncConfirmation] = {}

    # -- pending clarification -------------------------------------------------

    def register_pending_clarification(
        self,
        project_id: str,
        sender_id: str,
        question: str,
        task_ids: list[str] | None = None,
        *,
        source: str = CLARIFICATION_SOURCE_CLARIFY,
        claims: dict[str, PendingClaimDraft] | None = None,
        missing: dict[str, list[str]] | None = None,
        asked_rounds: int = 0,
        reask_count: int = 0,
        created_at: datetime | None = None,
    ) -> PendingClarification:
        entry = PendingClarification(
            question=question,
            task_ids=list(task_ids or []),
            claims=claims or {},
            missing=missing or {},
            asked_rounds=asked_rounds,
            reask_count=reask_count,
            created_at=created_at or self.clock.now(),
            source=source,
        )
        self._clarifications[(project_id, sender_id)] = entry
        return entry

    def get_pending_clarification(
        self, project_id: str, sender_id: str, now: datetime | None = None
    ) -> PendingClarification | None:
        key = (project_id, sender_id)
        entry = self._clarifications.get(key)
        if entry is None:
            return None
        checked = now or self.clock.now()
        if checked - entry.created_at > self.clarification_ttl:
            del self._clarifications[key]
            return None
        return entry

    def clear_pending_clarification(self, project_id: str, sender_id: str) -> None:
        self._clarifications.pop((project_id, sender_id), None)

    # -- pending sync confirmation ----------------------------------------------

    def register_pending_sync_confirmation(
        self,
        project_id: str,
        sender_id: str,
        initiated_by: str,
        member_ids: list[str],
        created_at: datetime | None = None,
    ) -> PendingSyncConfirmation:
        entry = PendingSyncConfirmation(
            initiated_by=initiated_by,
            member_ids=list(member_ids),
            created_at=created_at or self.clock.now(),
        )
        self._confirmations[(project_id, sender_id)] = entry
        return entry

    def get_pending_sync_confirmation(
        self, project_id: str, sender_id: str, now: datetime | None = None
    ) -> PendingSyncConfirmation | None:
        key = (project_id, sender_id)
        entry = self._confirmations.get(key)
        if entry is None:
            return None
        checked = now or self.clock.now()
        if checked - entry.created_at > self.confirmation_ttl:
            del self._confirmations[key]
            return None
        return entry

    def clear_pending_sync_confirmation(self, project_id: str, sender_id: str) -> None:
        self._confirmations.pop((project_id, sender_id), None)

    # -- persistence (ADR-0013) ---------------------------------------------------

    def export_project(self, project_id: str) -> DialogueSnapshot:
        """Every entry of one project, expired or not: expiry is decided on access."""
        return DialogueSnapshot(
            clarifications={
                sender: entry
                for (project, sender), entry in self._clarifications.items()
                if project == project_id
            },
            confirmations={
                sender: entry
                for (project, sender), entry in self._confirmations.items()
                if project == project_id
            },
        )

    def restore_project(self, project_id: str, snapshot: DialogueSnapshot) -> None:
        """Loads persisted entries; an entry already in memory is newer and wins."""
        for sender, clarification in snapshot.clarifications.items():
            self._clarifications.setdefault((project_id, sender), clarification)
        for sender, confirmation in snapshot.confirmations.items():
            self._confirmations.setdefault((project_id, sender), confirmation)

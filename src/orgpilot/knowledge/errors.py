"""Project-knowledge specific failures with stable meanings (ADR-0015)."""

from orgpilot.domain.errors import OrgPilotError


class DuplicateDeliveryConflict(OrgPilotError):
    """Same (repository_id, delivery_id) redelivered with a different payload."""


class RevisionConflict(OrgPilotError):
    """A revision identity or sequence collides with different content."""


class KnowledgeContractError(OrgPilotError):
    """A knowledge write violated a frozen contract (namespace, scope, epoch)."""


class ReadSessionConflict(OrgPilotError):
    """A read session update violated its lifecycle (e.g. finish twice)."""


class BindingConflict(OrgPilotError):
    """A binding version row conflicts with the existing row of that version."""

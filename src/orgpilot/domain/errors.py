"""Domain-specific failures with stable meanings for callers and tests."""


class OrgPilotError(Exception):
    """Base exception for expected OrgPilot failures."""


class DomainInvariantError(OrgPilotError):
    """Raised when an event would violate a domain invariant."""


class DuplicateEventConflict(OrgPilotError):
    """Raised when an existing event id is reused with different content."""


class DependencyCycleError(OrgPilotError):
    """Raised when task dependencies contain a cycle."""


class GroundTruthMismatch(OrgPilotError):
    """Raised when a scenario result differs from declared ground truth."""

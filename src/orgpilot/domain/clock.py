"""Injectable time source keeping time-dependent decisions deterministic and replayable.

The coordination kernel never reads the wall clock directly: every decision that
depends on "now" (approval expiry, response timeouts, generated event timestamps)
receives its instant from a `Clock`. Production wires `SystemClock`; tests and
replay wire `FrozenClock`, so a scenario produces the same trace regardless of
when it runs.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock timestamps must include an explicit timezone")
    return value


@runtime_checkable
class Clock(Protocol):
    """Time source for any decision that depends on the current instant."""

    def now(self) -> datetime:
        """Returns the current timezone-aware instant."""
        ...


class SystemClock:
    """Production clock backed by the real wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic clock whose instant only moves when a caller moves it."""

    def __init__(self, fixed: datetime) -> None:
        self._now = _require_timezone(fixed)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> datetime:
        """Pins the clock to an explicit instant."""
        self._now = _require_timezone(value)
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        """Moves the clock forward (or backward for a negative delta)."""
        self._now += delta
        return self._now

"""A clock that only moves when a test tells it to."""

from __future__ import annotations

from datetime import datetime, timedelta


class FakeClock:
    """Implements ``hottub.domain.Clock`` with time under the test's control.

    Example:
        clock = FakeClock(datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
        clock.advance(hours=2)
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start time")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, *, hours: float = 0, minutes: float = 0, seconds: float = 0) -> None:
        """Move time forward. Time never goes backwards."""
        delta = timedelta(hours=hours, minutes=minutes, seconds=seconds)
        if delta < timedelta(0):
            raise ValueError("FakeClock cannot go backwards")
        self._now += delta

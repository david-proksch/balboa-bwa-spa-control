"""The source of "now" for the whole application.

Nothing in this code base calls ``datetime.now()`` directly. Every component
that needs the time is given a :class:`Clock`, so tests can substitute a clock
they control and run a simulated weekend in milliseconds
(see ``tests/fakes/clock.py``).

All instants are timezone-aware and in UTC. Conversion to local time happens
only where a rule is written in wall-clock terms (the scheduler) or where a
person reads it (the web UI).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Anything that can say what time it is."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock, backed by the operating system."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        return datetime.now(UTC)

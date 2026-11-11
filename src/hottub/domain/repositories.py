"""Interfaces to stored data.

Pattern: Repository (Fowler, PoEAA; Evans). Each interface here describes a
collection in the domain's own terms. The application services depend on
these; ``hottub.persistence`` implements them over SQLite, and
``tests/fakes/repositories.py`` implements them in memory. No SQL appears
outside ``hottub.persistence``.

The methods are synchronous on purpose. The database is a local SQLite file
and every call is a single small statement, so the event loop is held for
well under a millisecond, and the code stays free of the locking questions
that come with threads.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Protocol

from hottub.domain.alerts import Alert, Severity
from hottub.domain.audit import CommandRecord
from hottub.domain.history import Device, RunInterval, TemperatureSample
from hottub.domain.schedule import PlannedUse
from hottub.domain.spa_state import PumpSpeed, SpaFault
from hottub.domain.temperature import TemperatureBand


class SettingsRepository(Protocol):
    """The owner's settings, which must survive a restart."""

    def load_band(self) -> TemperatureBand | None:
        """Return the stored floor and ceiling, or ``None`` if never set."""
        ...

    def save_band(self, band: TemperatureBand) -> None:
        """Store the floor and ceiling, replacing any earlier values."""
        ...


class CommandLog(Protocol):
    """The audit trail of commands sent to the spa.

    Records are never changed; old ones are deleted to bound the file size.
    """

    def append(self, record: CommandRecord) -> None:
        """Add a record."""
        ...

    def recent(self, limit: int) -> Sequence[CommandRecord]:
        """Return up to ``limit`` records, newest first."""
        ...

    def delete_before(self, instant: datetime) -> None:
        """Delete records issued before ``instant``."""
        ...


class RunIntervalRepository(Protocol):
    """When the heater and pumps ran.

    At most one interval per device is *open*, meaning the device was
    running when last seen. An open interval's end is moved forward as the
    device is confirmed still running, so the stored end is always a time
    the device really was running (see :class:`~hottub.domain.RunInterval`).
    """

    def start(self, device: Device, speed: PumpSpeed | None, at: datetime) -> None:
        """Open an interval for ``device`` starting, and so far ending, at ``at``."""
        ...

    def extend_open(self, until: datetime) -> None:
        """Move the end of every open interval forward to ``until``.

        An interval already ending later is left alone: ends never move back.
        """
        ...

    def finish(self, device: Device, at: datetime) -> None:
        """Close the open interval of ``device``, ending it at ``at``.

        Does nothing if the device has no open interval.
        """
        ...

    def finish_all(self) -> None:
        """Close every open interval where its end currently stands."""
        ...

    def overlapping(self, start: datetime, end: datetime) -> Sequence[RunInterval]:
        """Return the intervals, open or closed, that overlap the window."""
        ...

    def delete_before(self, instant: datetime) -> None:
        """Delete closed intervals that ended before ``instant``."""
        ...


class TemperatureSampleRepository(Protocol):
    """The once-a-minute temperature record."""

    def add(self, sample: TemperatureSample) -> None:
        """Store a sample."""
        ...

    def between(self, start: datetime, end: datetime) -> Sequence[TemperatureSample]:
        """Return the samples taken from ``start`` to ``end``, oldest first."""
        ...

    def earliest(self) -> datetime | None:
        """Return when the oldest stored sample was taken, if there is one."""
        ...

    def delete_before(self, instant: datetime) -> None:
        """Delete samples taken before ``instant``."""
        ...


class PlanRepository(Protocol):
    """What the owner has asked the scheduler for, and the scheduler's own notes.

    Holds the planned uses (at most one per calendar day), the weekend
    keep-ready switch, and one thing the scheduler must remember across a
    restart: whether it owes the spa a return to Rest mode.
    """

    def planned_use(self, day: date) -> PlannedUse | None:
        """Return the use planned for ``day``, if there is one."""
        ...

    def planned_uses_between(self, first_day: date, last_day: date) -> Sequence[PlannedUse]:
        """Return the uses planned from ``first_day`` to ``last_day``, earliest first."""
        ...

    def save_planned_use(self, use: PlannedUse) -> None:
        """Store ``use``, replacing any other plan for the same day."""
        ...

    def mark_heating_started(self, day: date, at: datetime) -> None:
        """Record that heating for the use on ``day`` began at ``at``.

        Does nothing if there is no such plan or its start is already
        recorded: the first start is the one that counts.
        """
        ...

    def delete_planned_use(self, day: date) -> None:
        """Remove the plan for ``day``; does nothing if there is none."""
        ...

    def delete_planned_uses_before(self, day: date) -> None:
        """Remove the plans for days earlier than ``day``."""
        ...

    def weekend_ready(self) -> bool:
        """Return whether weekend keep-ready is switched on (off until set)."""
        ...

    def set_weekend_ready(self, enabled: bool) -> None:
        """Switch weekend keep-ready on or off."""
        ...

    def use_duration(self) -> timedelta | None:
        """Return how long a planned use holds the ceiling, if the owner has chosen."""
        ...

    def set_use_duration(self, duration: timedelta) -> None:
        """Store how long a planned use holds the ceiling."""
        ...

    def rest_to_restore(self) -> bool:
        """Return whether the scheduler switched the spa from Rest to Ready.

        If so, it owes the spa a switch back to Rest once it no longer
        needs heat.
        """
        ...

    def set_rest_to_restore(self, owed: bool) -> None:
        """Record whether a return to Rest mode is owed."""
        ...


class AlertRepository(Protocol):
    """The watchdog's alerts, active and past, and one note of its own.

    The note is the newest entry of the spa's fault log that the watchdog
    has already dealt with, so that a restart does not raise it again.
    """

    def add(self, alert: Alert) -> Alert:
        """Store a new alert and return it with its number filled in."""
        ...

    def get(self, alert_id: int) -> Alert | None:
        """Return the alert with that number, if there is one."""
        ...

    def active(self) -> Sequence[Alert]:
        """Return the alerts that have not cleared, oldest first."""
        ...

    def escalate(self, alert_id: int, severity: Severity, message: str) -> None:
        """Change an alert's severity and message, when matters get worse."""
        ...

    def clear(self, alert_id: int, at: datetime) -> None:
        """Mark an alert as over at ``at``. Does nothing to one already cleared."""
        ...

    def raised_since(self, instant: datetime) -> Sequence[Alert]:
        """Return every alert raised at or after ``instant``, newest first."""
        ...

    def delete_cleared_before(self, instant: datetime) -> None:
        """Delete alerts that cleared before ``instant``. Active ones are kept."""
        ...

    def last_fault_seen(self) -> SpaFault | None:
        """Return the newest fault-log entry already dealt with, if any."""
        ...

    def remember_fault(self, fault: SpaFault) -> None:
        """Note ``fault`` as dealt with."""
        ...

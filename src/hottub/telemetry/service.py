"""Answers questions about the tub's history.

Pattern: Service Layer (Fowler, PoEAA), read side. The arithmetic is in
``hottub.domain.history``; this class fetches the right records for it and
supplies the current time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from hottub.domain import (
    Clock,
    Device,
    HeatingRate,
    RunIntervalRepository,
    RuntimeTotals,
    Temperature,
    TemperatureSample,
    TemperatureSampleRepository,
    measure_heating_rate,
    total_runtime,
)
from hottub.domain.history import MINIMUM_RUN

#: How far back heating runs are used to measure the heating rate. Long
#: enough to hold several full heat-ups, short enough to follow the seasons.
HEATING_RATE_HISTORY = timedelta(days=30)


@dataclass(frozen=True)
class RuntimeWindow:
    """Running time over the last ``days`` days."""

    days: int
    totals: RuntimeTotals


@dataclass(frozen=True)
class RuntimeReport:
    """Running time over several windows, all ending at the same moment.

    Attributes:
        as_of: The moment the windows end (UTC).
        recording_since: When the oldest surviving record was made, or
            ``None`` if nothing has been recorded. A window reaching back
            before this is incomplete, and the page should say so.
        windows: One entry per window asked for, in the order asked.
    """

    as_of: datetime
    recording_since: datetime | None
    windows: tuple[RuntimeWindow, ...]


class TelemetryService:
    """Reads the recorded history.

    Args:
        intervals: Where run intervals are kept.
        samples: Where temperature samples are kept.
        clock: Source of the current time.
        seed_heating_rate_f_per_hour: The heating rate assumed until enough
            heating is on record to measure it.
    """

    def __init__(
        self,
        intervals: RunIntervalRepository,
        samples: TemperatureSampleRepository,
        clock: Clock,
        seed_heating_rate_f_per_hour: float,
    ) -> None:
        """Create the service."""
        self._intervals = intervals
        self._samples = samples
        self._clock = clock
        self._seed_rate = seed_heating_rate_f_per_hour

    def runtime(self, days: Iterable[int]) -> RuntimeReport:
        """Return how long the heater and pumps ran over the last days.

        A day here is 24 hours, so "1 day" means the 24 hours up to now,
        not since midnight.

        Args:
            days: The window lengths wanted, e.g. ``[1, 7, 14, 30]``.
        """
        wanted = tuple(days)
        now = self._clock.now()
        # One query for the longest window serves the shorter ones too.
        longest = now - timedelta(days=max(wanted, default=0))
        intervals = self._intervals.overlapping(longest, now)
        return RuntimeReport(
            as_of=now,
            recording_since=self._samples.earliest(),
            windows=tuple(
                RuntimeWindow(
                    days=length,
                    totals=total_runtime(intervals, now - timedelta(days=length), now),
                )
                for length in wanted
            ),
        )

    def latest_water(self, within: timedelta) -> Temperature | None:
        """Return the newest water temperature recorded in the last ``within``.

        For when the spa reports no reading at the moment: it does that
        for some seconds now and then, and for hours in Rest mode. Returns
        ``None`` if no reading was recorded in that time.
        """
        now = self._clock.now()
        for sample in reversed(self._samples.between(now - within, now)):
            if sample.water is not None:
                return sample.water
        return None

    def heating_rate(self) -> HeatingRate:
        """Return how fast the heater warms the water.

        Measured from the last 30 days of heating when there is enough of
        it, otherwise the configured starting assumption (see
        :func:`hottub.domain.measure_heating_rate` for what "enough" means).
        """
        now = self._clock.now()
        runs = [
            interval
            for interval in self._intervals.overlapping(now - HEATING_RATE_HISTORY, now)
            if interval.device is Device.HEATER
            and interval.ended_at - interval.started_at >= MINIMUM_RUN
        ]
        # Only the samples taken during those runs are needed, which is a
        # small part of a month of one-a-minute samples.
        samples: list[TemperatureSample] = []
        for run in runs:
            samples.extend(self._samples.between(run.started_at, run.ended_at))
        measured = measure_heating_rate(runs, samples)
        return measured or HeatingRate(f_per_hour=self._seed_rate, measured=False)

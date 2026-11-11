"""History: what ran when, how warm the water was, and what follows from it.

The spa only reports its present state. Everything about the past, such as
"how long did the heater run this week?", comes from records this
application keeps while it is watching. This module defines those records
and the arithmetic over them. It is pure: the functions take records and
return numbers, so they are tested with hand-written histories.

Runtime is kept as *intervals* (a device ran from here to there), not as
periodic samples. An interval is written when a device starts and ends when
it stops, so an hour of heating is one row, and a total is a sum of
overlaps with the window asked about.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from hottub.domain.spa_state import PumpSpeed
from hottub.domain.temperature import Temperature


class Device(Enum):
    """A part of the tub whose running time is recorded."""

    HEATER = "heater"
    PUMP1 = "pump1"
    PUMP2 = "pump2"


@dataclass(frozen=True)
class RunInterval:
    """A stretch of time during which a device was seen running.

    Attributes:
        device: What ran.
        speed: For pump 1, the speed it ran at; a change of speed ends one
            interval and starts another. ``None`` for the other devices.
        started_at: When it was first seen running (UTC).
        ended_at: The last moment it was *confirmed* running (UTC). For a
            device still running this trails the present by up to a minute;
            it is never a guess about the future, so a crash or an outage
            cannot inflate the total.
    """

    device: Device
    speed: PumpSpeed | None
    started_at: datetime
    ended_at: datetime

    def overlap(self, window_start: datetime, window_end: datetime) -> timedelta:
        """Return how much of this interval lies inside the window."""
        start = max(self.started_at, window_start)
        end = min(self.ended_at, window_end)
        return max(end - start, timedelta(0))


@dataclass(frozen=True)
class TemperatureSample:
    """The temperatures at one moment; one is recorded each minute.

    Attributes:
        at: When the sample was taken (UTC).
        water: Measured water temperature, or ``None`` when the spa had no
            reading (normal in Rest mode between filter cycles).
        set_temperature: What the spa was set to hold.
    """

    at: datetime
    water: Temperature | None
    set_temperature: Temperature


@dataclass(frozen=True)
class RuntimeTotals:
    """How long each device ran within one window of time.

    Attributes:
        window_start: Start of the window (UTC).
        window_end: End of the window (UTC).
        heater: Time the heater was heating.
        pump1_low: Time pump 1 ran on low, including when the spa ran it by
            itself to heat or filter.
        pump1_high: Time pump 1 ran on high.
        pump2: Time pump 2 ran.
    """

    window_start: datetime
    window_end: datetime
    heater: timedelta
    pump1_low: timedelta
    pump1_high: timedelta
    pump2: timedelta

    @property
    def pump1(self) -> timedelta:
        """Time pump 1 ran at either speed."""
        return self.pump1_low + self.pump1_high


def total_runtime(
    intervals: Iterable[RunInterval], window_start: datetime, window_end: datetime
) -> RuntimeTotals:
    """Add up how long each device ran within the window.

    An interval that straddles an edge of the window counts only for the
    part inside it.
    """
    heater = pump1_low = pump1_high = pump2 = timedelta(0)
    for interval in intervals:
        inside = interval.overlap(window_start, window_end)
        if interval.device is Device.HEATER:
            heater += inside
        elif interval.device is Device.PUMP2:
            pump2 += inside
        elif interval.speed is PumpSpeed.HIGH:
            pump1_high += inside
        else:
            pump1_low += inside
    return RuntimeTotals(window_start, window_end, heater, pump1_low, pump1_high, pump2)


@dataclass(frozen=True)
class HeatingRate:
    """How fast the heater warms the water.

    Attributes:
        f_per_hour: Degrees Fahrenheit gained per hour of heating.
        measured: True if worked out from this tub's history, False if it
            is the configured starting assumption.
        based_on: How much heating time the measurement rests on; zero when
            not measured.
    """

    f_per_hour: float
    measured: bool
    based_on: timedelta = timedelta(0)


#: A heating run shorter than this says little: the spa reports whole
#: degrees, so over a short run the rounding error swamps the rise.
MINIMUM_RUN = timedelta(minutes=30)

#: The measurement is trusted once it rests on this much heating in all.
MINIMUM_EVIDENCE = timedelta(hours=2)

#: A measured rate outside these bounds (°F per hour) is not believed. A
#: 240 V heater in a tub this size manages a few degrees an hour; far less
#: means the cover was off or the readings were bad, far more is impossible.
PLAUSIBLE_RATE_F_PER_HOUR = (1.0, 15.0)


def measure_heating_rate(
    intervals: Iterable[RunInterval], samples: Sequence[TemperatureSample]
) -> HeatingRate | None:
    """Work out the heating rate from recorded history.

    For each long enough run of the heater, take the first and last water
    readings recorded during it. The rate is the total rise over the total
    time between those readings, across all runs. Summing before dividing
    weights long runs more, and evens out the whole-degree rounding.

    Runs during which the water did not rise are included. Leaving them out
    would flatter the heater, and an optimistic rate makes the tub late.

    Args:
        intervals: Run intervals; only the heater's are used.
        samples: Temperature samples in time order.

    Returns:
        The measured rate, or ``None`` if there is not yet enough heating
        on record or the result is not plausible.
    """
    times = [sample.at for sample in samples]
    rise_f = 0.0
    duration = timedelta(0)
    for interval in intervals:
        if interval.device is not Device.HEATER:
            continue
        during = samples[
            bisect_left(times, interval.started_at) : bisect_right(times, interval.ended_at)
        ]
        readings = [sample for sample in during if sample.water is not None]
        if len(readings) < 2:
            continue
        first, last = readings[0], readings[-1]
        span = last.at - first.at
        if span < MINIMUM_RUN:
            continue
        assert first.water is not None and last.water is not None  # filtered above
        rise_f += last.water.degrees_above(first.water)
        duration += span

    if duration < MINIMUM_EVIDENCE:
        return None
    rate = rise_f / (duration.total_seconds() / 3600)
    lowest, highest = PLAUSIBLE_RATE_F_PER_HOUR
    if not lowest <= rate <= highest:
        return None
    return HeatingRate(f_per_hour=rate, measured=True, based_on=duration)

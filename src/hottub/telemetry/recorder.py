"""Writes the tub's history as it happens.

Pattern: Observer (GoF). The recorder subscribes to the spa's state changes
and so learns the moment a device starts or stops. Changes alone are not
enough, though: a heater that runs for two hours produces no event in the
middle, and a lost connection produces none at all. So the application also
calls :meth:`TelemetryRecorder.tick` once a minute, which

* moves the end of every open interval forward to the last time the spa was
  heard from, while it is being heard from;
* closes the intervals once the spa has gone quiet; and
* records a temperature sample.

The rule that keeps the totals honest
-------------------------------------
An interval's stored end is only ever a moment at which the spa itself
reported the device running. Nothing is extrapolated. If the connection
drops, the program crashes or the computer sleeps, the interval simply ends
at the last confirmed moment, and running time during the gap is not
counted: the record under-reports rather than invents. A device that keeps
running through a gap gets a new interval when the spa is heard again.

A running device's interval can therefore trail the present by up to a
minute (the tick period), and so can the totals worked out from it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from hottub.domain import (
    Clock,
    Device,
    PumpSpeed,
    RunIntervalRepository,
    SpaState,
    SpaStateChanged,
    TemperatureSample,
    TemperatureSampleRepository,
    Unsubscribe,
)
from hottub.spa import SpaPort

_LOGGER = logging.getLogger(__name__)

#: How long the spa may be silent before its last report stops being
#: trusted. Longer than the Wi-Fi module's ordinary stalls of about 20
#: seconds (docs/balboa-notes.md), and the same as the watchdog's threshold.
DEFAULT_STALE_AFTER = timedelta(seconds=90)

#: How often the application calls :meth:`TelemetryRecorder.tick`.
DEFAULT_TICK_PERIOD = timedelta(seconds=60)


class TelemetryRecorder:
    """Records run intervals and temperature samples from one spa.

    The recorder must be the only writer of the interval repository: it
    keeps its own note of which intervals are open instead of asking the
    database on every report.

    Args:
        spa: The spa to watch.
        intervals: Where run intervals are kept.
        samples: Where temperature samples are kept.
        clock: Source of the current time.
        stale_after: Silence longer than this ends the open intervals.
        tick_period: How often :meth:`tick` will be called.
    """

    def __init__(
        self,
        spa: SpaPort,
        intervals: RunIntervalRepository,
        samples: TemperatureSampleRepository,
        clock: Clock,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        tick_period: timedelta = DEFAULT_TICK_PERIOD,
    ) -> None:
        """Create a recorder; nothing is recorded until :meth:`start`."""
        self._spa = spa
        self._intervals = intervals
        self._samples = samples
        self._clock = clock
        self._stale_after = stale_after
        # The recorder looks at the spa once per tick. Two looks that both
        # found it fresh can have seen reports this far apart at most;
        # anything further apart means a look was missed (the program was
        # not running, or the computer slept) and the record has a hole.
        self._longest_step = stale_after + tick_period

        #: Devices with an open interval, and for pump 1 the speed recorded.
        self._open: dict[Device, PumpSpeed | None] = {}
        #: The latest report the open intervals have been extended to.
        self._confirmed_at: datetime | None = None
        self._unsubscribe: Unsubscribe | None = None

    def start(self) -> None:
        """Begin recording.

        Any interval left open by a previous run of the program (a crash, a
        power cut) is closed first, at the end it had reached.
        """
        self._intervals.finish_all()
        self._open.clear()
        self._confirmed_at = None
        if self._unsubscribe is None:
            self._unsubscribe = self._spa.subscribe(self._on_state_changed)

    def stop(self) -> None:
        """Stop recording, closing the open intervals as of now."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._bring_up_to_date()
        self._close_all()

    def tick(self) -> None:
        """Bring the record up to date; call once a minute.

        Extends or closes the open intervals as described in the module
        documentation and, while the spa is being heard from, stores a
        temperature sample.
        """
        state = self._bring_up_to_date()
        if state is None:
            self._close_all()
            return
        self._samples.add(
            TemperatureSample(
                at=self._clock.now(),
                water=state.water_temperature,
                set_temperature=state.set_temperature,
            )
        )

    # --- Internals -------------------------------------------------------------

    def _on_state_changed(self, event: SpaStateChanged) -> None:
        """Take in a change the moment the spa reports it."""
        try:
            self._observe(event.current, event.current.observed_at)
        except Exception:
            # A listener must not raise (see SpaStateListener). Whatever was
            # missed is put right by the next tick.
            _LOGGER.exception("Could not record a change of spa state")

    def _bring_up_to_date(self) -> SpaState | None:
        """Take in the spa's latest report, if it is fresh.

        Returns:
            The spa's state if the spa is currently being heard from,
            otherwise ``None``.
        """
        state = self._spa.state
        heard_at = self._spa.last_message_at
        if state is None or heard_at is None:
            return None
        silence = self._clock.now() - heard_at
        if not self._spa.connected or silence > self._stale_after:
            # Credit the time up to the last report before giving up: the
            # devices were running until then.
            if self._follows_without_a_gap(heard_at):
                self._intervals.extend_open(heard_at)
            return None
        self._observe(state, heard_at)
        return state

    def _observe(self, state: SpaState, at: datetime) -> None:
        """Bring the intervals into line with ``state``, reported at ``at``."""
        if self._follows_without_a_gap(at):
            if self._open:
                self._intervals.extend_open(at)
        else:
            # The record has a hole before this report. What ran meanwhile
            # is unknown, so the old intervals end where they stood.
            self._close_all()

        running = _running_devices(state)
        for device in list(self._open):
            if device not in running or running[device] is not self._open[device]:
                self._intervals.finish(device, at)
                del self._open[device]
        for device, speed in running.items():
            if device not in self._open:
                self._intervals.start(device, speed, at)
                self._open[device] = speed
        self._confirmed_at = at

    def _follows_without_a_gap(self, at: datetime) -> bool:
        """Return whether a report at ``at`` continues the record so far."""
        return self._confirmed_at is None or at - self._confirmed_at <= self._longest_step

    def _close_all(self) -> None:
        if self._open:
            _LOGGER.info(
                "Closing the run intervals of %s",
                ", ".join(sorted(device.value for device in self._open)),
            )
        self._intervals.finish_all()
        self._open.clear()
        self._confirmed_at = None


def _running_devices(state: SpaState) -> dict[Device, PumpSpeed | None]:
    """Return the devices running in ``state``, with pump 1's speed.

    The heater counts only while it is actually heating, not while it waits
    for water flow.
    """
    running: dict[Device, PumpSpeed | None] = {}
    if state.is_heating:
        running[Device.HEATER] = None
    if state.pump1 is not PumpSpeed.OFF:
        running[Device.PUMP1] = state.pump1
    if state.pump2_on:
        running[Device.PUMP2] = None
    return running

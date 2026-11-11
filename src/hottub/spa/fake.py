"""A simulated hot tub.

:class:`FakeSpaAdapter` implements :class:`~hottub.spa.port.SpaPort` without
any hardware. It exists for two reasons:

* **Tests.** Scheduler, telemetry and watchdog behaviour is tested against it
  with a controllable clock, so "does the tub reach 102 °F by 6 PM?" is a
  unit test, not an afternoon by the tub.
* **Development.** The web app can run on a laptop with no spa in reach.

It is shipped in ``src`` rather than ``tests`` because the application itself
can be started in simulated mode.

What it models, and how faithfully
----------------------------------
The aim is the behaviour that our own logic depends on, not thermodynamics:

* Water warms at a constant rate while heating and cools at a constant rate
  otherwise, never below ambient. (A real tub cools faster the hotter it is;
  constant rates keep test arithmetic exact.)
* In Ready mode a thermostat with hysteresis runs the heater. In Rest mode
  the heater stays off. (A real tub in Rest still heats during filter
  cycles; filter cycles are not simulated.)
* Pump 1 runs on low whenever the heater is on and cannot be switched off
  while it is, as on the real controller (``docs/balboa-notes.md``).
* Pumps switched on by a command switch themselves off after a timeout.
* Temperatures are reported in whole degrees, like the real spa.

Simulated time only moves when :meth:`FakeSpaAdapter.tick` is called or a
command is sent, and it moves to whatever the injected clock says. Nothing
here sleeps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from hottub.domain import (
    SPA_MAXIMUM_F,
    SPA_MINIMUM_F,
    Clock,
    CommandRejectedError,
    HeaterState,
    HeatMode,
    PumpSpeed,
    SpaFault,
    SpaState,
    SpaStateChanged,
    SpaStateListener,
    SpaUnavailableError,
    Temperature,
    Unsubscribe,
)
from hottub.spa.port import SpaPort

#: Longest stretch of simulated time advanced in one step. Small enough that
#: the thermostat reacts within a minute, large enough that simulating a week
#: is about ten thousand cheap steps.
_MAX_STEP = timedelta(minutes=1)


@dataclass(frozen=True)
class FakeSpaSettings:
    """Physical characteristics of the simulated tub.

    Attributes:
        heating_rate_f_per_hour: Temperature rise while the heater runs.
        cooling_rate_f_per_hour: Temperature fall while it does not.
        ambient: The water never cools below this.
        hysteresis_f: The heater starts once the water is this far below the
            set temperature, and stops when it reaches it.
        pump_timeout: How long a pump runs after being switched on by a
            command before the tub switches it off.
    """

    heating_rate_f_per_hour: float = 5.0
    cooling_rate_f_per_hour: float = 0.5
    ambient: Temperature = Temperature(50.0)
    hysteresis_f: float = 1.0
    pump_timeout: timedelta = timedelta(minutes=15)


class FakeSpaAdapter(SpaPort):
    """An in-memory spa that follows the injected clock.

    Besides the :class:`SpaPort` interface it offers a few methods for tests
    to stage situations the watchdog must catch: :meth:`drop_connection`,
    :meth:`fail_heater` and :meth:`log_fault`.

    Args:
        clock: Source of simulated time.
        water: Starting water temperature.
        set_temperature: Starting set temperature.
        heat_mode: Starting heat mode.
        settings: Physical characteristics; defaults suit most tests.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        water: Temperature = Temperature(99.0),
        set_temperature: Temperature = Temperature(99.0),
        heat_mode: HeatMode = HeatMode.READY,
        settings: FakeSpaSettings | None = None,
    ) -> None:
        """Create a disconnected simulated spa; call :meth:`connect` to use it."""
        self._clock = clock
        self._settings = settings or FakeSpaSettings()

        # The simulation keeps exact values; only the reported state is rounded.
        self._water_f = water.fahrenheit
        self._set_f = set_temperature.fahrenheit
        self._heat_mode = heat_mode
        self._heater_on = False
        self._heater_failed = False

        # What was last *requested*; the reported speed also depends on the heater.
        self._pump1_requested = PumpSpeed.OFF
        self._pump1_off_at: datetime | None = None
        self._pump2_on = False
        self._pump2_off_at: datetime | None = None

        self._simulated_until = clock.now()
        self._connected = False
        self._state: SpaState | None = None
        self._last_message_at: datetime | None = None
        self._listeners: list[SpaStateListener] = []
        self._faults: list[SpaFault] = []

    # --- SpaPort: connection -------------------------------------------------

    async def connect(self) -> None:
        """Open the simulated connection and report the current state."""
        self.restore_connection()

    async def disconnect(self) -> None:
        """Close the simulated connection."""
        self._connected = False

    @property
    def connected(self) -> bool:
        """Whether the simulated connection is open."""
        return self._connected

    @property
    def last_message_at(self) -> datetime | None:
        """When the simulated spa last reported, or ``None`` if never."""
        return self._last_message_at

    # --- SpaPort: reading ----------------------------------------------------

    @property
    def state(self) -> SpaState | None:
        """The last reported state; goes stale while disconnected."""
        return self._state

    def subscribe(self, listener: SpaStateListener) -> Unsubscribe:
        """Register ``listener`` for state changes; returns its remover."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def latest_fault(self) -> SpaFault | None:
        """Return the newest fault staged with :meth:`log_fault`, if any."""
        self._require_connection()
        return self._faults[-1] if self._faults else None

    # --- SpaPort: commands ---------------------------------------------------

    async def set_temperature(self, temperature: Temperature) -> None:
        """Set the temperature the simulated spa holds."""
        self._catch_up()
        whole = temperature.to_whole_degree().fahrenheit
        if not SPA_MINIMUM_F <= whole <= SPA_MAXIMUM_F:
            raise CommandRejectedError(
                f"Set temperature {temperature} is outside {SPA_MINIMUM_F:g}-{SPA_MAXIMUM_F:g} °F"
            )
        self._set_f = whole
        self._report()

    async def set_pump1(self, speed: PumpSpeed) -> None:
        """Request a speed for pump 1 and (re)start its switch-off timer."""
        self._catch_up()
        self._pump1_requested = speed
        self._pump1_off_at = (
            None if speed is PumpSpeed.OFF else self._simulated_until + self._settings.pump_timeout
        )
        self._report()

    async def set_pump2(self, on: bool) -> None:
        """Switch pump 2 and (re)start its switch-off timer."""
        self._catch_up()
        self._pump2_on = on
        self._pump2_off_at = self._simulated_until + self._settings.pump_timeout if on else None
        self._report()

    async def set_heat_mode(self, mode: HeatMode) -> None:
        """Switch the simulated spa between Ready and Rest."""
        self._catch_up()
        self._heat_mode = mode
        self._report()

    # --- Driving the simulation ----------------------------------------------

    def tick(self) -> None:
        """Advance the simulation to the clock's current time and report.

        Tests call this after moving their fake clock. In development mode
        the application calls it about once a second, which stands in for the
        real spa's continuous status messages.
        """
        self._simulate_to(self._clock.now())
        self._report()

    # --- Staging faults (tests and demos) ------------------------------------

    def drop_connection(self) -> None:
        """Simulate losing the connection without anyone asking for it.

        The tub carries on heating and cooling; it just cannot be heard.
        """
        self._connected = False

    def restore_connection(self) -> None:
        """Simulate the connection coming back, and report the current state."""
        self._connected = True
        self.tick()

    def fail_heater(self) -> None:
        """Make the heater produce no heat while still reporting "heating".

        This is what a failed element or a tripped breaker on the heater
        circuit looks like from the outside.
        """
        self._heater_failed = True

    def repair_heater(self) -> None:
        """Undo :meth:`fail_heater`."""
        self._heater_failed = False

    def log_fault(self, fault: SpaFault) -> None:
        """Add an entry to the simulated fault log."""
        self._faults.append(fault)

    # --- Internals -----------------------------------------------------------

    def _require_connection(self) -> None:
        """Raise the same error a real adapter raises when the spa is unreachable."""
        if not self._connected:
            raise SpaUnavailableError("The simulated spa is disconnected")

    def _catch_up(self) -> None:
        """Prepare for a command: must be connected and simulated up to now."""
        self._require_connection()
        self._simulate_to(self._clock.now())

    def _simulate_to(self, instant: datetime) -> None:
        """Advance the physical simulation to ``instant`` in small steps."""
        while self._simulated_until < instant:
            step = min(_MAX_STEP, instant - self._simulated_until)
            self._apply_thermostat()
            self._apply_heat_flow(step)
            self._simulated_until += step
            self._expire_pump_timers()

    def _apply_thermostat(self) -> None:
        """Decide whether the heater should be running right now."""
        if self._heat_mode is HeatMode.REST:
            self._heater_on = False
        elif self._water_f <= self._set_f - self._settings.hysteresis_f:
            self._heater_on = True
        elif self._water_f >= self._set_f:
            self._heater_on = False
        # Between the two thresholds the heater keeps doing what it was doing.

    def _apply_heat_flow(self, step: timedelta) -> None:
        """Warm or cool the water for one step."""
        hours = step.total_seconds() / 3600
        if self._heater_on and not self._heater_failed:
            warmed = self._water_f + self._settings.heating_rate_f_per_hour * hours
            self._water_f = min(self._set_f, warmed)
        elif self._water_f > self._settings.ambient.fahrenheit:
            cooled = self._water_f - self._settings.cooling_rate_f_per_hour * hours
            self._water_f = max(self._settings.ambient.fahrenheit, cooled)

    def _expire_pump_timers(self) -> None:
        """Switch off any pump whose timeout has passed."""
        if self._pump1_off_at is not None and self._simulated_until >= self._pump1_off_at:
            self._pump1_requested = PumpSpeed.OFF
            self._pump1_off_at = None
        if self._pump2_off_at is not None and self._simulated_until >= self._pump2_off_at:
            self._pump2_on = False
            self._pump2_off_at = None

    def _actual_pump1_speed(self) -> PumpSpeed:
        """Combine the requested speed with the heater's need for flow."""
        if self._pump1_requested is PumpSpeed.HIGH:
            return PumpSpeed.HIGH
        if self._heater_on or self._pump1_requested is PumpSpeed.LOW:
            return PumpSpeed.LOW
        return PumpSpeed.OFF

    def _report(self) -> None:
        """Publish the current state, as the real spa's status message would.

        Does nothing while disconnected. Listeners are notified only when the
        readings differ from the previous report.
        """
        if not self._connected:
            return
        # A command or a finished step may have changed what the thermostat wants.
        self._apply_thermostat()
        snapshot = SpaState(
            observed_at=self._simulated_until,
            water_temperature=Temperature(self._water_f).to_whole_degree(),
            set_temperature=Temperature(self._set_f),
            heat_mode=self._heat_mode,
            heater=HeaterState.HEATING if self._heater_on else HeaterState.OFF,
            pump1=self._actual_pump1_speed(),
            pump2_on=self._pump2_on,
        )
        previous = self._state
        self._state = snapshot
        self._last_message_at = snapshot.observed_at
        if previous is None or not snapshot.same_readings_as(previous):
            event = SpaStateChanged(previous=previous, current=snapshot)
            # Iterate over a copy: a listener may unsubscribe while being called.
            for listener in list(self._listeners):
                listener(event)

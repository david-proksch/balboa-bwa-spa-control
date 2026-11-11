"""Alerts: what can be wrong with the tub, and the rules that notice it.

Patterns
--------
* **Strategy** (GoF): each rule is an :class:`AlertCheck`. The watchdog
  (``hottub.watchdog``) holds a list of them and treats them alike, so a
  new rule is a new class and nothing else changes.
* **Port** (Cockburn): :class:`Notifier` is how an alert reaches the
  owner's phone. The watchdog does not know that Pushover exists.

How a check answers
-------------------
A check is shown an :class:`Observation` once a minute and gives a
:class:`Verdict` with one of three standings:

``PROBLEM``
    The condition has held long enough: an alert should be active.
``OK``
    Positively fine: an active alert should be cleared.
``UNKNOWN``
    Cannot tell, or not yet sure. Nothing is raised and nothing is cleared.

The third standing is what keeps the watchdog from crying wolf. A water
temperature that the spa does not vouch for is unknown, not cold; a
condition that has held for ten of its thirty minutes is not yet a
problem, and an alert already raised is not withdrawn merely because the
evidence went away.

Checks remember since when their condition has held. That memory is not
stored: after a restart every condition must hold for its full duration
again before an alert is raised. The alerts themselves are stored
(:class:`~hottub.domain.repositories.AlertRepository`).

All of this is plain arithmetic on values handed in; nothing here reads a
clock or talks to the spa.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from hottub.domain.errors import HotTubError
from hottub.domain.spa_state import HeaterState, HeatMode, PumpSpeed, SpaFault, SpaState
from hottub.domain.temperature import Temperature, TemperatureBand


class Severity(Enum):
    """How much an alert matters."""

    WARNING = "warning"
    """Shown on the web page only."""

    CRITICAL = "critical"
    """Shown on the web page and pushed to the owner's phone."""


class AlertKind(Enum):
    """The things the watchdog can notice."""

    STALE_DATA = "stale_data"
    WATER_TOO_COLD = "water_too_cold"
    WATER_TOO_HOT = "water_too_hot"
    NOT_HEATING = "not_heating"
    SPA_FAULT = "spa_fault"

    @property
    def title(self) -> str:
        """A few words for a heading or a notification title."""
        return _TITLES[self]

    @property
    def needs_acknowledging(self) -> bool:
        """Whether only the owner can clear an alert of this kind.

        True for spa faults: the controller's fault log is a history, so
        the spa never says that a fault is over.
        """
        return self is AlertKind.SPA_FAULT


_TITLES = {
    AlertKind.STALE_DATA: "No word from the spa",
    AlertKind.WATER_TOO_COLD: "Water too cold",
    AlertKind.WATER_TOO_HOT: "Water too hot",
    AlertKind.NOT_HEATING: "Not heating",
    AlertKind.SPA_FAULT: "Spa fault",
}


@dataclass(frozen=True)
class Alert:
    """Something the watchdog noticed, from when it was raised until it cleared.

    Attributes:
        kind: What was noticed.
        severity: How much it matters. Can rise while the alert is active.
        message: A sentence for the owner, written when the alert was
            raised (or last made more severe).
        raised_at: When it was raised, timezone-aware UTC.
        cleared_at: When it cleared or was acknowledged; ``None`` while it
            is active.
        id: Its number in the store; ``None`` until stored.
    """

    kind: AlertKind
    severity: Severity
    message: str
    raised_at: datetime
    cleared_at: datetime | None = None
    id: int | None = None

    @property
    def active(self) -> bool:
        """Whether the alert still stands."""
        return self.cleared_at is None


class AlertNotFoundError(HotTubError, LookupError):
    """There is no alert with the number given."""


class AlertNotAcknowledgeableError(HotTubError):
    """The alert cannot be acknowledged: it is over, or it clears by itself."""


@dataclass(frozen=True)
class WatchdogThresholds:
    """Every figure the checks use. The defaults are explained in PLAN.md, section 4.

    Attributes:
        stale_after: Silence from the spa for this long is a warning. Well
            above the 20 seconds for which the Wi-Fi module routinely goes
            quiet (``docs/balboa-notes.md``, "Stalls").
        stale_critical_after: Silence for this long is critical.
        cold_margin_f: The water is too cold when more than this far below
            the floor.
        hot_margin_f: The water is too hot when more than this far above
            the ceiling.
        out_of_band_for: How long too cold or too hot must last.
        absolute_maximum: Water at or above this is too hot at once.
        heat_call_gap_f: A spa in Ready is taken to be calling for heat
            when the set temperature is more than this far above the water.
        expected_rise_f: While heat is called for, the water should gain
            this much...
        rise_within: ...within this time.
        fault_check_every: How often the spa is asked for its fault log.
    """

    stale_after: timedelta = timedelta(seconds=90)
    stale_critical_after: timedelta = timedelta(minutes=15)
    cold_margin_f: float = 3.0
    hot_margin_f: float = 2.0
    out_of_band_for: timedelta = timedelta(minutes=30)
    absolute_maximum: Temperature = Temperature(104.0)
    heat_call_gap_f: float = 2.0
    expected_rise_f: float = 1.0
    rise_within: timedelta = timedelta(minutes=45)
    fault_check_every: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        """Refuse figures that make no sense.

        Raises:
            ValueError: If a duration or margin is not positive, or the
                critical silence is shorter than the warning one.
        """
        durations = (
            self.stale_after,
            self.stale_critical_after,
            self.out_of_band_for,
            self.rise_within,
            self.fault_check_every,
        )
        margins = (
            self.cold_margin_f,
            self.hot_margin_f,
            self.heat_call_gap_f,
            self.expected_rise_f,
        )
        if any(duration <= timedelta(0) for duration in durations):
            raise ValueError("every watchdog duration must be greater than zero")
        if any(margin <= 0 for margin in margins):
            raise ValueError("every watchdog margin must be greater than zero")
        if self.stale_critical_after < self.stale_after:
            raise ValueError("stale data cannot become critical before it becomes a warning")


@dataclass(frozen=True)
class Observation:
    """What the watchdog can see at one instant.

    Attributes:
        now: The instant, timezone-aware UTC.
        state: The spa's last reported state, or ``None`` if it has never
            reported.
        band: The floor and ceiling in force.
        last_message_at: When the spa was last heard from, if ever.
        watching_since: When the watchdog started. Stands in for
            ``last_message_at`` while the spa has never been heard from,
            so that a tub that is off when the service starts is noticed.
    """

    now: datetime
    state: SpaState | None
    band: TemperatureBand
    last_message_at: datetime | None
    watching_since: datetime

    @property
    def silent_for(self) -> timedelta:
        """How long it is since the spa was heard from."""
        return self.now - (self.last_message_at or self.watching_since)

    @property
    def valid_water(self) -> Temperature | None:
        """The water temperature, if there is a reading that can be trusted.

        The sensor sits in the plumbing, so it reads the tub's water only
        while water moves past it. In Ready mode the spa circulates often
        enough; in Rest mode it does not, so a reading there counts only
        while a pump is running. Otherwise the temperature is unknown,
        which is different from wrong.
        """
        state = self.state
        if state is None or state.water_temperature is None:
            return None
        pump_running = state.pump1 is not PumpSpeed.OFF or state.pump2_on
        if state.heat_mode is HeatMode.REST and not pump_running:
            return None
        return state.water_temperature

    @property
    def heater_running(self) -> bool:
        """Whether the spa says its heater is on, or about to be."""
        return self.state is not None and self.state.heater is not HeaterState.OFF


class Standing(Enum):
    """The three answers a check can give (see the module docstring)."""

    OK = "ok"
    PROBLEM = "problem"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Verdict:
    """A check's answer.

    Attributes:
        standing: Fine, wrong, or cannot tell.
        severity: How much it matters; set only for a problem.
        message: A sentence for the owner; set only for a problem.
    """

    standing: Standing
    severity: Severity | None = None
    message: str = ""

    @classmethod
    def ok(cls) -> Verdict:
        """Return "positively fine"."""
        return cls(Standing.OK)

    @classmethod
    def unknown(cls) -> Verdict:
        """Return "cannot tell, or not yet sure"."""
        return cls(Standing.UNKNOWN)

    @classmethod
    def problem(cls, severity: Severity, message: str) -> Verdict:
        """Return "an alert should be active"."""
        return cls(Standing.PROBLEM, severity, message)


class AlertCheck(ABC):
    """One rule of the watchdog."""

    @property
    @abstractmethod
    def kind(self) -> AlertKind:
        """The kind of alert this rule raises and clears."""

    @abstractmethod
    def evaluate(self, observation: Observation) -> Verdict:
        """Judge the tub as observed. Called once a minute, in time order."""

    def suspend(self) -> None:  # noqa: B027 - an optional hook, deliberately not abstract
        """Forget how long the condition has held.

        Called instead of :meth:`evaluate` while the spa's data is stale.
        The default does nothing, for rules that keep no memory.
        """


class _Stopwatch:
    """Remembers since when a condition has held without a break."""

    def __init__(self) -> None:
        self._since: datetime | None = None

    def running_for(self, now: datetime) -> timedelta:
        """Return how long the condition has held, starting now if it had not."""
        if self._since is None:
            self._since = now
        return now - self._since

    def restart(self, now: datetime) -> None:
        self._since = now

    def stop(self) -> None:
        self._since = None


def _minutes(duration: timedelta) -> str:
    return f"{duration.total_seconds() / 60:g} minutes"


class StaleDataCheck(AlertCheck):
    """The spa has gone quiet.

    A warning after a short silence, critical after a long one: a tub that
    stays unreachable may be without power, and then nothing is heating or
    filtering it.
    """

    def __init__(self, thresholds: WatchdogThresholds) -> None:
        """Take the lengths of silence from ``thresholds``."""
        self._thresholds = thresholds

    @property
    def kind(self) -> AlertKind:
        """Stale data."""
        return AlertKind.STALE_DATA

    def evaluate(self, observation: Observation) -> Verdict:
        """Compare the silence with the two thresholds."""
        silence = observation.silent_for
        if silence < self._thresholds.stale_after:
            return Verdict.ok()
        if silence < self._thresholds.stale_critical_after:
            return Verdict.problem(
                Severity.WARNING,
                "The spa has stopped reporting. Its temperature and state are unknown.",
            )
        return Verdict.problem(
            Severity.CRITICAL,
            f"The spa has not reported for {_minutes(self._thresholds.stale_critical_after)}. "
            "It may be without power or off the network.",
        )


class WaterTooColdCheck(AlertCheck):
    """The water is well below the floor and nothing is warming it.

    Raised when a trusted reading has been more than the margin below the
    floor for the full duration **with the heater off**. While the heater
    runs the rule waits: water on its way up from a low temperature (a
    refilled tub, a raised floor) is not a fault, and a heater that runs
    without warming the water is :class:`NotHeatingCheck`'s business.

    Cleared when the water is back at or above the floor. Between the two
    limits nothing changes, so a reading that hovers at the limit does not
    raise and clear in turn.
    """

    def __init__(self, thresholds: WatchdogThresholds) -> None:
        """Take the margin and the duration from ``thresholds``."""
        self._thresholds = thresholds
        self._cold = _Stopwatch()

    @property
    def kind(self) -> AlertKind:
        """Water too cold."""
        return AlertKind.WATER_TOO_COLD

    def evaluate(self, observation: Observation) -> Verdict:
        """Judge the water against the floor."""
        water = observation.valid_water
        if water is None:
            self._cold.stop()
            return Verdict.unknown()
        floor = observation.band.floor
        if water.fahrenheit >= floor.fahrenheit:
            self._cold.stop()
            return Verdict.ok()
        too_cold = water.fahrenheit < floor.fahrenheit - self._thresholds.cold_margin_f
        if not too_cold or observation.heater_running:
            self._cold.stop()
            return Verdict.unknown()
        if self._cold.running_for(observation.now) < self._thresholds.out_of_band_for:
            return Verdict.unknown()
        return Verdict.problem(
            Severity.CRITICAL,
            f"The water is at {water}, more than {self._thresholds.cold_margin_f:g} °F below "
            f"the floor of {floor}, and the heater is off.",
        )

    def suspend(self) -> None:
        """Start the count again when data returns."""
        self._cold.stop()


class WaterTooHotCheck(AlertCheck):
    """The water is dangerously hot, or well above the ceiling and not coming down.

    Two conditions:

    * At or above the absolute maximum: raised at once.
    * More than the margin above the ceiling for the full duration, unless
      the tub is merely cooling down. It is cooling down when the heater is
      off and the water is no warmer than when it first exceeded the limit;
      that is what a tub looks like after its ceiling was lowered, and it
      can take a day. A heater that runs above the limit, or water that
      keeps getting warmer with the heater off, is not cooling down.

    Cleared when the water is back at or below the ceiling.
    """

    def __init__(self, thresholds: WatchdogThresholds) -> None:
        """Take the margin, the duration and the maximum from ``thresholds``."""
        self._thresholds = thresholds
        self._hot = _Stopwatch()
        self._first_excess: Temperature | None = None

    @property
    def kind(self) -> AlertKind:
        """Water too hot."""
        return AlertKind.WATER_TOO_HOT

    def evaluate(self, observation: Observation) -> Verdict:
        """Judge the water against the ceiling and the absolute maximum."""
        water = observation.valid_water
        if water is None:
            self.suspend()
            return Verdict.unknown()
        maximum = self._thresholds.absolute_maximum
        if water.fahrenheit >= maximum.fahrenheit:
            return Verdict.problem(
                Severity.CRITICAL,
                f"The water is at {water}, at or above the safety limit of {maximum}.",
            )
        ceiling = observation.band.ceiling
        if water.fahrenheit <= ceiling.fahrenheit:
            self.suspend()
            return Verdict.ok()
        if water.fahrenheit <= ceiling.fahrenheit + self._thresholds.hot_margin_f:
            self.suspend()
            return Verdict.unknown()

        if self._first_excess is None:
            self._first_excess = water
        cooling_down = (
            not observation.heater_running and water.fahrenheit <= self._first_excess.fahrenheit
        )
        if cooling_down:
            self._hot.stop()
            return Verdict.unknown()
        if self._hot.running_for(observation.now) < self._thresholds.out_of_band_for:
            return Verdict.unknown()
        return Verdict.problem(
            Severity.CRITICAL,
            f"The water is at {water}, more than {self._thresholds.hot_margin_f:g} °F above "
            f"the ceiling of {ceiling}, and it is not cooling down.",
        )

    def suspend(self) -> None:
        """Start the count again when data returns."""
        self._hot.stop()
        self._first_excess = None


class NotHeatingCheck(AlertCheck):
    """The spa is calling for heat and the water is not getting warmer.

    This is what a failed heating element or a tripped breaker on the
    heater circuit looks like: the controller reports "heating" and nothing
    happens. The spa calls for heat when it says its heater is on, or when
    it is in Ready mode with the set temperature well above the water.

    The water must gain the expected rise within the allowed time, measured
    again from each rise. Cleared when it does, or when heat is no longer
    called for.
    """

    def __init__(self, thresholds: WatchdogThresholds) -> None:
        """Take the rise and the time allowed for it from ``thresholds``."""
        self._thresholds = thresholds
        self._waiting = _Stopwatch()
        self._baseline: Temperature | None = None

    @property
    def kind(self) -> AlertKind:
        """Not heating."""
        return AlertKind.NOT_HEATING

    def evaluate(self, observation: Observation) -> Verdict:
        """Judge whether a call for heat is being answered."""
        water = observation.valid_water
        if water is None or observation.state is None:
            self.suspend()
            return Verdict.unknown()
        if not self._heat_called_for(observation.state, water, observation.heater_running):
            self.suspend()
            return Verdict.ok()

        if self._baseline is None:
            self._baseline = water
            self._waiting.restart(observation.now)
            return Verdict.unknown()
        if water.fahrenheit >= self._baseline.fahrenheit + self._thresholds.expected_rise_f:
            self._baseline = water
            self._waiting.restart(observation.now)
            return Verdict.ok()
        if self._waiting.running_for(observation.now) < self._thresholds.rise_within:
            return Verdict.unknown()
        return Verdict.problem(
            Severity.CRITICAL,
            f"The spa is calling for heat, but the water has not risen above "
            f"{self._baseline} in {_minutes(self._thresholds.rise_within)}.",
        )

    def suspend(self) -> None:
        """Start the count again when data returns."""
        self._waiting.stop()
        self._baseline = None

    def _heat_called_for(self, state: SpaState, water: Temperature, heater_running: bool) -> bool:
        gap = state.set_temperature.fahrenheit - water.fahrenheit
        wants_heat = state.heat_mode is HeatMode.READY and gap > self._thresholds.heat_call_gap_f
        return heater_running or wants_heat


#: Fault-log entries that are part of normal operation. They are recorded
#: as warnings, not pushed to the phone. 19 is "Priming Mode", which the
#: controller logs every time it is powered up.
ROUTINE_FAULT_CODES = frozenset({19})


def fault_alert(fault: SpaFault, noticed_at: datetime) -> Alert:
    """Turn a new entry in the spa's fault log into an alert."""
    severity = Severity.WARNING if fault.code in ROUTINE_FAULT_CODES else Severity.CRITICAL
    return Alert(
        kind=AlertKind.SPA_FAULT,
        severity=severity,
        message=f"The spa logged: {fault.message} (code {fault.code}).",
        raised_at=noticed_at,
    )


def standard_checks(thresholds: WatchdogThresholds) -> list[AlertCheck]:
    """Return the rules that judge the spa's reported state.

    :class:`StaleDataCheck` is not among them: the watchdog applies it
    first and by itself, because its answer decides whether these are
    consulted at all. Spa faults are not a rule of this kind either; they
    come from the fault log (:func:`fault_alert`).
    """
    return [
        WaterTooColdCheck(thresholds),
        WaterTooHotCheck(thresholds),
        NotHeatingCheck(thresholds),
    ]


# --- Telling the owner -------------------------------------------------------


@dataclass(frozen=True)
class Notification:
    """A message for the owner's phone.

    Attributes:
        title: A few words.
        message: A sentence or two.
    """

    title: str
    message: str


class NotificationError(HotTubError):
    """A notification could not be delivered.

    Attributes:
        permanent: True if trying again cannot help (the keys are wrong,
            say); False for a failure that may pass (no network).
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        """Record the reason and whether a retry could help."""
        super().__init__(message)
        self.permanent = permanent


class Notifier(ABC):
    """Delivers notifications to the owner."""

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Whether notifications go anywhere at all."""

    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """Deliver one notification, once.

        Raises:
            NotificationError: If it could not be delivered.
        """

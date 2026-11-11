"""What the web pages show, worked out before any HTML is written.

Pattern: Presentation Model (Fowler). Each ``...View`` class holds exactly
the text and flags one piece of a page displays, already formatted: "99",
"Heating", "3 minutes ago". The functions here build them from what the
services return; the templates then only place the values and never
compute or decide anything. That keeps the display rules where they can be
tested without rendering a page, and keeps the templates readable by
someone who knows HTML but not Python.

Times are shown on the owner's wall clock, in the 12-hour form used by
``hottub.scheduler.wording``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from hottub.control import SpaStatus
from hottub.domain import (
    Alert,
    HeaterState,
    HeatMode,
    PlannedHeating,
    PumpSpeed,
    Severity,
    Temperature,
)
from hottub.scheduler import Outlook, WeekendReady
from hottub.scheduler.wording import clock_time
from hottub.telemetry import RuntimeReport

#: Shown where there is no value: no reading, or a spa never heard from.
NO_VALUE = "—"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_HEATER_TEXT = {
    HeaterState.OFF: "Off",
    HeaterState.HEATING: "Heating",
    HeaterState.WAITING: "Waiting for water flow",
}


# --- Small formatting helpers ------------------------------------------------


def degrees(temperature: Temperature | None) -> str:
    """Format a temperature as a bare number, e.g. ``"99"`` or ``"99.5"``."""
    return NO_VALUE if temperature is None else f"{temperature.fahrenheit:g}"


def time_of_day(value: time) -> str:
    """Format a wall-clock time, e.g. ``"3:00 PM"``."""
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def date_and_time(instant: datetime, zone: tzinfo) -> str:
    """Format an instant with its date, e.g. ``"Nov 11, 4:23 PM"``."""
    local = instant.astimezone(zone)
    return f"{_MONTHS[local.month - 1]} {local.day}, {time_of_day(local.time())}"


def ago(seconds: float) -> str:
    """Say how long ago something was, roughly: ``"3 minutes ago"``."""
    if seconds < 90:
        return "a moment ago"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} minutes ago"
    hours = round(minutes / 60)
    if hours < 48:
        return f"{hours} hours ago"
    return f"{round(hours / 24)} days ago"


def hours(duration: timedelta) -> str:
    """Format a running time in hours to one decimal, e.g. ``"3.5"``."""
    return f"{duration.total_seconds() / 3600:.1f}"


# --- The tub -----------------------------------------------------------------


@dataclass(frozen=True)
class TubView:
    """The tub as it is now: the reading at the top of the dashboard, and the controls.

    Attributes:
        known: False if the spa has never reported; then only ``problem``,
            ``floor`` and ``ceiling`` mean anything.
        water: Water temperature as a number, or a dash.
        target: The spa's set temperature, likewise.
        floor: The floor, as a number.
        ceiling: The ceiling, as a number.
        heater: What the heater is doing, in words.
        heating: Whether the heater is producing heat (for emphasis).
        heat_mode: ``"ready"`` or ``"rest"``; ``None`` if not known.
        pump1: ``"off"``, ``"low"`` or ``"high"``; ``None`` if not known.
        pump2: ``"off"`` or ``"on"``; ``None`` if not known.
        problem: A sentence when the values above are not current,
            otherwise ``None``.
    """

    known: bool
    water: str
    target: str
    floor: str
    ceiling: str
    heater: str
    heating: bool
    heat_mode: str | None
    pump1: str | None
    pump2: str | None
    problem: str | None


#: The Wi-Fi module's routine silences are shorter than this (seconds), so
#: only a longer one is worth telling the owner about.
_QUIET_IS_NORMAL_S = 60.0


def tub_view(status: SpaStatus) -> TubView:
    """Describe the tub for the dashboard."""
    state = status.state
    floor, ceiling = degrees(status.band.floor), degrees(status.band.ceiling)
    if state is None:
        return TubView(
            known=False,
            water=NO_VALUE,
            target=NO_VALUE,
            floor=floor,
            ceiling=ceiling,
            heater=NO_VALUE,
            heating=False,
            heat_mode=None,
            pump1=None,
            pump2=None,
            problem="The spa has not been reached yet. Still trying.",
        )

    problem = None
    silent_for = status.seconds_since_last_message
    if not status.connected or (silent_for is not None and silent_for > _QUIET_IS_NORMAL_S):
        heard = "" if silent_for is None else f" It was last heard from {ago(silent_for)}."
        problem = f"The spa cannot be reached, so these values may be out of date.{heard}"
    return TubView(
        known=True,
        water=degrees(state.water_temperature),
        target=degrees(state.set_temperature),
        floor=floor,
        ceiling=ceiling,
        heater=_HEATER_TEXT[state.heater],
        heating=state.is_heating,
        heat_mode=state.heat_mode.value,
        pump1=state.pump1.value,
        pump2="on" if state.pump2_on else "off",
        problem=problem,
    )


#: The choices offered for each control, as (value sent, label shown).
PUMP1_CHOICES = tuple((speed.value, speed.value.capitalize()) for speed in PumpSpeed)
PUMP2_CHOICES = (("off", "Off"), ("on", "On"))
HEAT_MODE_CHOICES = tuple((mode.value, mode.value.capitalize()) for mode in HeatMode)


# --- Plans -------------------------------------------------------------------


@dataclass(frozen=True)
class PlanDayView:
    """One day's row in the plan card.

    Attributes:
        key: ``"today"`` or ``"tomorrow"``; part of the form's address.
        label: The same, capitalised, for display.
        day: The calendar day in words, e.g. ``"Wed, Nov 11"``.
        use_time: The planned time as ``"HH:MM"``, or ``""`` when nothing
            is planned.
        choices: The times offered in the list, as (``"HH:MM"``, label)
            pairs: every quarter hour, plus the planned time if it is not
            on a quarter hour (a plan made through the API can be).
        summary: The timetable in a sentence; ``None`` when nothing is
            planned.
        late: Whether the tub will not be warm by the time it was wanted.
    """

    key: str
    label: str
    day: str
    use_time: str
    choices: tuple[tuple[str, str], ...]
    summary: str | None
    late: bool


#: How far apart the times in the list are.
TIME_STEP_MINUTES = 15

#: Every quarter hour of the day, as (``"HH:MM"``, ``"7:15 PM"``).
#: A list rather than the browser's own time field: that field wants
#: hour, minutes and AM/PM entered one by one, refuses "19" in 12-hour
#: form and shows the hour as invalid until all three are in.
TIME_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (f"{minutes // 60:02d}:{minutes % 60:02d}", time_of_day(time(minutes // 60, minutes % 60)))
    for minutes in range(0, 24 * 60, TIME_STEP_MINUTES)
)


def _choices_including(planned: time) -> tuple[tuple[str, str], ...]:
    value = f"{planned:%H:%M}"
    if any(choice == value for choice, _ in TIME_CHOICES):
        return TIME_CHOICES
    return tuple(sorted((*TIME_CHOICES, (value, time_of_day(planned)))))


def plan_day_view(
    key: str, day: date, planned: PlannedHeating | None, now: datetime, zone: tzinfo
) -> PlanDayView:
    """Describe what is planned for ``day``."""
    day_text = f"{_WEEKDAYS[day.weekday()]}, {_MONTHS[day.month - 1]} {day.day}"
    if planned is None:
        return PlanDayView(key, key.capitalize(), day_text, "", TIME_CHOICES, None, late=False)

    def at(instant: datetime) -> str:
        return clock_time(instant, now, zone)

    if planned.use.heating_started_at is not None:
        start = f"Heating since {at(planned.use.heating_started_at)}"
    else:
        start = f"Heater starts at about {at(planned.heater_start)}"
    if planned.late:
        ready = f"warm at about {at(planned.expected_ready_at)}, later than wanted"
    else:
        ready = f"ready by {at(planned.ready_at)}"
    summary = f"{start}; {ready}; kept warm until {at(planned.ends_at)}."
    return PlanDayView(
        key=key,
        label=key.capitalize(),
        day=day_text,
        use_time=f"{planned.use.use_time:%H:%M}",
        choices=_choices_including(planned.use.use_time),
        summary=summary,
        late=planned.late,
    )


@dataclass(frozen=True)
class WeekendView:
    """The weekend keep-ready switch.

    Attributes:
        enabled: Whether it is on.
        description: When it applies, in a sentence.
    """

    enabled: bool
    description: str


def weekend_view(weekend: WeekendReady) -> WeekendView:
    """Describe the weekend keep-ready switch."""
    return WeekendView(
        enabled=weekend.enabled,
        description=(
            f"Keeps the tub at the ceiling from Friday {time_of_day(weekend.start_time)} "
            f"to Sunday midnight, without heating between "
            f"{time_of_day(weekend.blackout.start)} and {time_of_day(weekend.blackout.end)}."
        ),
    )


@dataclass(frozen=True)
class OutlookView:
    """What the scheduler is doing and will do next, in sentences.

    Attributes:
        current: What it wants now.
        upcoming: What it will do next; ``None`` if nothing is due.
        warnings: Things the owner should know.
    """

    current: str
    upcoming: str | None
    warnings: tuple[str, ...]


def outlook_view(outlook: Outlook) -> OutlookView:
    """Describe the scheduler's outlook."""
    return OutlookView(outlook.current_text, outlook.next_text, outlook.warnings)


# --- Running time ------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeRowView:
    """One window of the running-time table.

    Attributes:
        label: The window, e.g. ``"7 days"``.
        heater: Heater hours.
        pump1: Pump 1 hours at either speed.
        pump1_high: The part of those on high.
        pump2: Pump 2 hours.
        partial: True if records do not go back as far as the window.
    """

    label: str
    heater: str
    pump1: str
    pump1_high: str
    pump2: str
    partial: bool


@dataclass(frozen=True)
class RuntimeView:
    """The running-time table.

    Attributes:
        rows: One per window, shortest first.
        note: Says since when records exist, if any window is partial.
    """

    rows: tuple[RuntimeRowView, ...]
    note: str | None


def runtime_view(report: RuntimeReport, zone: tzinfo) -> RuntimeView:
    """Describe the running times for the dashboard's table."""
    since = report.recording_since
    rows = []
    for window in report.windows:
        totals = window.totals
        rows.append(
            RuntimeRowView(
                label="24 hours" if window.days == 1 else f"{window.days} days",
                heater=hours(totals.heater),
                pump1=hours(totals.pump1),
                pump1_high=hours(totals.pump1_high),
                pump2=hours(totals.pump2),
                partial=since is None or since > totals.window_start,
            )
        )
    note = None
    if since is None:
        note = "Nothing has been recorded yet."
    elif any(row.partial for row in rows):
        note = (
            f"* Records begin {date_and_time(since, zone)}, so this covers less than the full time."
        )
    return RuntimeView(tuple(rows), note)


# --- Alerts ------------------------------------------------------------------


@dataclass(frozen=True)
class AlertView:
    """One alert, for the banner and the history.

    Attributes:
        id: Its number, for the acknowledge button.
        title: A few words.
        message: A sentence.
        critical: Whether it is critical (otherwise a warning).
        active: Whether it still stands.
        raised: When it was raised.
        cleared: When it cleared; ``None`` while active.
        can_acknowledge: Whether the owner can clear it with a button.
    """

    id: int
    title: str
    message: str
    critical: bool
    active: bool
    raised: str
    cleared: str | None
    can_acknowledge: bool


def alert_views(alerts: Sequence[Alert], zone: tzinfo) -> list[AlertView]:
    """Describe alerts, in the order given."""
    views = []
    for alert in alerts:
        assert alert.id is not None
        views.append(
            AlertView(
                id=alert.id,
                title=alert.kind.title,
                message=alert.message,
                critical=alert.severity is Severity.CRITICAL,
                active=alert.active,
                raised=date_and_time(alert.raised_at, zone),
                cleared=None if alert.cleared_at is None else date_and_time(alert.cleared_at, zone),
                can_acknowledge=alert.active and alert.kind.needs_acknowledging,
            )
        )
    return views

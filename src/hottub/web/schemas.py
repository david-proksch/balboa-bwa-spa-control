"""The shapes of the JSON API's requests and responses.

These Pydantic models are the API's contract and are what the generated
reference at ``/hot-tub/api/docs`` shows. They are deliberately separate
from the domain types: the API can stay stable while the domain is
refactored, and the reverse.

Temperatures are plain numbers in degrees Fahrenheit, with the unit in the
field name. Times are ISO 8601 in UTC.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from hottub.control import SpaStatus
from hottub.domain import (
    Alert,
    CommandRecord,
    Decision,
    HeaterState,
    HeatingRate,
    HeatMode,
    PlannedHeating,
    PumpSpeed,
    Reason,
)
from hottub.scheduler import Outlook, WeekendReady
from hottub.telemetry import RuntimeReport, RuntimeWindow

OnOff = Literal["on", "off"]


class TemperatureRequest(BaseModel):
    """Body of ``PUT /temperature/ceiling`` and ``PUT /temperature/floor``."""

    fahrenheit: float = Field(description="Whole degrees; fractions are rounded.", examples=[99])


class Pump1Request(BaseModel):
    """Body of ``PUT /pumps/1``."""

    speed: PumpSpeed


class Pump2Request(BaseModel):
    """Body of ``PUT /pumps/2``."""

    state: OnOff


class HeatModeRequest(BaseModel):
    """Body of ``PUT /heat-mode``."""

    mode: HeatMode


class StatusResponse(BaseModel):
    """The spa's last known state and how fresh it is.

    The fields that come from the spa are null until it has reported once.
    They are the *last known* values: when ``connected`` is false or
    ``seconds_since_last_message`` is large, they are out of date.
    """

    connected: bool = Field(description="Whether the connection to the spa is open.")
    last_message_at: datetime | None = Field(description="When the spa was last heard from.")
    seconds_since_last_message: float | None = Field(
        description="Age of the data. Up to about 20 is normal for this spa's Wi-Fi module."
    )
    water_temperature_f: float | None = Field(
        description="Null when the spa has no reading, as in Rest mode between filter cycles."
    )
    target_temperature_f: float | None = Field(
        description="The set temperature the spa is holding now."
    )
    ceiling_f: float = Field(description="Temperature the tub is heated to when it is to be ready.")
    floor_f: float = Field(description="Temperature the tub may fall to when idle.")
    heat_mode: HeatMode | None
    heater: HeaterState | None
    pump1: PumpSpeed | None
    pump2: OnOff | None

    @classmethod
    def from_status(cls, status: SpaStatus) -> StatusResponse:
        """Build the response from the control service's status."""
        state = status.state
        water = None if state is None else state.water_temperature
        return cls(
            connected=status.connected,
            last_message_at=status.last_message_at,
            seconds_since_last_message=status.seconds_since_last_message,
            water_temperature_f=None if water is None else water.fahrenheit,
            target_temperature_f=None if state is None else state.set_temperature.fahrenheit,
            ceiling_f=status.band.ceiling.fahrenheit,
            floor_f=status.band.floor.fahrenheit,
            heat_mode=None if state is None else state.heat_mode,
            heater=None if state is None else state.heater,
            pump1=None if state is None else state.pump1,
            pump2=None if state is None else ("on" if state.pump2_on else "off"),
        )


class CommandResponse(BaseModel):
    """One entry of the audit log."""

    issued_at: datetime
    actor: Literal["user", "scheduler"]
    command: str = Field(examples=["set_pump1"])
    argument: str = Field(examples=["high"])
    succeeded: bool = Field(
        description="False if the spa could not be reached or refused the command."
    )
    result: str = Field(description="What the spa reported afterwards, or the error.")

    @classmethod
    def from_record(cls, record: CommandRecord) -> CommandResponse:
        """Build the response from an audit record."""
        return cls(
            issued_at=record.issued_at,
            actor=record.actor.value,
            command=record.command,
            argument=record.argument,
            succeeded=record.succeeded,
            result=record.result,
        )


class AlertResponse(BaseModel):
    """One watchdog alert."""

    id: int = Field(description="The alert's number, for acknowledging it.")
    kind: Literal["stale_data", "water_too_cold", "water_too_hot", "not_heating", "spa_fault"]
    title: str = Field(examples=["Water too cold"])
    severity: Literal["warning", "critical"] = Field(
        description="Critical alerts are also pushed to the owner's phone."
    )
    message: str
    raised_at: datetime
    cleared_at: datetime | None = Field(description="Null while the alert is active.")
    needs_acknowledging: bool = Field(
        description="True if the alert stays until acknowledged; the others clear by themselves."
    )

    @classmethod
    def from_alert(cls, alert: Alert) -> AlertResponse:
        """Build the response from a stored alert."""
        assert alert.id is not None
        return cls(
            id=alert.id,
            kind=alert.kind.value,
            title=alert.kind.title,
            severity=alert.severity.value,
            message=alert.message,
            raised_at=alert.raised_at,
            cleared_at=alert.cleared_at,
            needs_acknowledging=alert.kind.needs_acknowledging,
        )


class AlertsResponse(BaseModel):
    """Answer of ``GET /alerts``."""

    active: list[AlertResponse] = Field(description="The alerts that stand now, oldest first.")
    history: list[AlertResponse] = Field(
        description="With `?days=`: every alert raised in that time, newest first, "
        "including the active ones. Empty otherwise."
    )
    notifications_configured: bool = Field(
        description="Whether critical alerts are pushed to a phone."
    )


class RuntimeWindowResponse(BaseModel):
    """Running time over one window; part of ``GET /runtime``."""

    days: int = Field(description="Length of the window; it ends at `as_of`.")
    heater_hours: float = Field(description="Time the heater was heating.")
    pump1_hours: float = Field(description="Time pump 1 ran at either speed.")
    pump1_low_hours: float = Field(
        description="Time pump 1 ran on low, including when the spa ran it to heat or filter."
    )
    pump1_high_hours: float
    pump2_hours: float

    @classmethod
    def from_window(cls, window: RuntimeWindow) -> RuntimeWindowResponse:
        """Convert the service's answer, rounding to a hundredth of an hour."""
        totals = window.totals
        return cls(
            days=window.days,
            heater_hours=_hours(totals.heater),
            pump1_hours=_hours(totals.pump1),
            pump1_low_hours=_hours(totals.pump1_low),
            pump1_high_hours=_hours(totals.pump1_high),
            pump2_hours=_hours(totals.pump2),
        )


class RuntimeResponse(BaseModel):
    """Body of ``GET /runtime``."""

    as_of: datetime = Field(description="The moment every window ends.")
    recording_since: datetime | None = Field(
        description=(
            "When the oldest record was made; null if there is none. A window that "
            "reaches back before this is incomplete."
        )
    )
    windows: list[RuntimeWindowResponse]

    @classmethod
    def from_report(cls, report: RuntimeReport) -> RuntimeResponse:
        """Convert the service's answer."""
        return cls(
            as_of=report.as_of,
            recording_since=report.recording_since,
            windows=[RuntimeWindowResponse.from_window(window) for window in report.windows],
        )


class HeatingRateResponse(BaseModel):
    """Body of ``GET /heating-rate``."""

    f_per_hour: float = Field(description="Degrees Fahrenheit gained per hour of heating.")
    measured: bool = Field(
        description="True if worked out from this tub's history, false if it is the assumption."
    )
    based_on_hours: float = Field(
        description="Heating time the measurement rests on; zero if not measured."
    )

    @classmethod
    def from_rate(cls, rate: HeatingRate) -> HeatingRateResponse:
        """Convert the service's answer."""
        return cls(
            f_per_hour=round(rate.f_per_hour, 2),
            measured=rate.measured,
            based_on_hours=_hours(rate.based_on),
        )


def _hours(duration: timedelta) -> float:
    return round(duration.total_seconds() / 3600, 2)


class PlanRequest(BaseModel):
    """Body of ``PUT /plan/today`` and ``PUT /plan/tomorrow``."""

    use_time: time = Field(
        description="When you mean to get in, as wall-clock time.", examples=["18:00"]
    )


class PlanResponse(BaseModel):
    """A planned use and the timetable worked out for it."""

    day: date = Field(description="The calendar day of the use, local.")
    use_time: time = Field(description="The planned time, wall clock.")
    use_at: datetime = Field(description="The same moment in UTC.")
    ready_at: datetime = Field(description="When the water should be at the ceiling.")
    heater_start: datetime = Field(
        description=(
            "When heating begins. An estimate that moves earlier as the water cools, "
            "until `heating_started` is true."
        )
    )
    heating_started: bool = Field(description="Whether heating for this use has begun.")
    hold_until: datetime = Field(description="When the tub stops being held at the ceiling.")
    expected_ready_at: datetime = Field(
        description="When the water is expected to reach the ceiling, judged from now."
    )
    late: bool = Field(description="True if that is after `ready_at`.")

    @classmethod
    def from_planned(cls, planned: PlannedHeating) -> PlanResponse:
        """Convert the scheduler's timetable."""
        return cls(
            day=planned.use.day,
            use_time=planned.use.use_time,
            use_at=planned.use_at,
            ready_at=planned.ready_at,
            heater_start=planned.heater_start,
            heating_started=planned.use.heating_started_at is not None,
            hold_until=planned.ends_at,
            expected_ready_at=planned.expected_ready_at,
            late=planned.late,
        )


class HoldTime(BaseModel):
    """Body and answer of ``/plan/hold``."""

    minutes: int = Field(
        gt=0, description="How long after a planned use time the tub is kept at the ceiling."
    )


class WeekendRequest(BaseModel):
    """Body of ``PUT /plan/weekend``."""

    enabled: bool


class WeekendResponse(BaseModel):
    """The weekend keep-ready switch and its times (wall clock)."""

    enabled: bool
    start_time: time = Field(description="When on Friday it begins. It ends Sunday at midnight.")
    blackout_start: time = Field(description="Start of the daily hours with no heating.")
    blackout_end: time = Field(description="End of the daily hours with no heating.")

    @classmethod
    def from_weekend(cls, weekend: WeekendReady) -> WeekendResponse:
        """Convert the scheduler's answer."""
        return cls(
            enabled=weekend.enabled,
            start_time=weekend.start_time,
            blackout_start=weekend.blackout.start,
            blackout_end=weekend.blackout.end,
        )


class ScheduledStateResponse(BaseModel):
    """What the scheduler wants at one moment; part of ``GET /plan/next-action``."""

    target_temperature_f: float = Field(description="The set temperature wanted.")
    reason: Reason
    description: str = Field(description="The same in plain language, in local time.")

    @classmethod
    def from_decision(cls, decision: Decision, description: str) -> ScheduledStateResponse:
        """Convert a decision and its wording."""
        return cls(
            target_temperature_f=decision.target.fahrenheit,
            reason=decision.reason,
            description=description,
        )


class UpcomingChangeResponse(ScheduledStateResponse):
    """The next change of set temperature; part of ``GET /plan/next-action``."""

    at: datetime = Field(description="When. An estimate for the start of heating.")


class NextActionResponse(BaseModel):
    """Body of ``GET /plan/next-action``."""

    as_of: datetime
    current: ScheduledStateResponse
    next: UpcomingChangeResponse | None = Field(
        description="Null if nothing changes within the next eight days."
    )
    overrides_blackout: bool = Field(
        description="True while a planned use is heating during the weekend's no-heating hours."
    )
    warnings: list[str] = Field(
        description="For example a planned use that cannot be ready in time."
    )

    @classmethod
    def from_outlook(cls, outlook: Outlook) -> NextActionResponse:
        """Convert the scheduler's forecast."""
        upcoming = outlook.next_change
        return cls(
            as_of=outlook.as_of,
            current=ScheduledStateResponse.from_decision(outlook.current, outlook.current_text),
            next=None
            if upcoming is None
            else UpcomingChangeResponse(
                at=upcoming.at,
                target_temperature_f=upcoming.decision.target.fahrenheit,
                reason=upcoming.decision.reason,
                description=outlook.next_text or "",
            ),
            overrides_blackout=(
                outlook.current.reason is Reason.PLANNED_USE
                and outlook.current.instead_of is Reason.WEEKEND_BLACKOUT
            ),
            warnings=list(outlook.warnings),
        )


class HealthResponse(BaseModel):
    """Body of ``GET /healthz``."""

    status: Literal["ok"] = "ok"
    spa_connected: bool


class ErrorResponse(BaseModel):
    """Body of every error response."""

    detail: str

"""What the pages show, before any HTML: the presentation models."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from hottub.control import SpaStatus
from hottub.domain import (
    Alert,
    AlertKind,
    BlackoutWindow,
    HeaterState,
    HeatMode,
    PlannedHeating,
    PlannedUse,
    PumpSpeed,
    RuntimeTotals,
    Severity,
    SpaState,
    Temperature,
    TemperatureBand,
)
from hottub.scheduler import WeekendReady
from hottub.telemetry import RuntimeReport, RuntimeWindow
from hottub.web import views

ZONE = ZoneInfo("America/Detroit")
NOON = datetime(2026, 1, 6, 17, 0, tzinfo=UTC)  # Tuesday 12:00 in Detroit
BAND = TemperatureBand(Temperature(85), Temperature(99))


def status(
    *,
    water: float | None = 97,
    heater: HeaterState = HeaterState.OFF,
    connected: bool = True,
    silent_for: float = 0.2,
) -> SpaStatus:
    state = SpaState(
        observed_at=NOON,
        water_temperature=None if water is None else Temperature(water),
        set_temperature=Temperature(99),
        heat_mode=HeatMode.READY,
        heater=heater,
        pump1=PumpSpeed.LOW,
        pump2_on=False,
    )
    return SpaStatus(state, BAND, connected, NOON, silent_for)


class TestFormatting:
    @pytest.mark.parametrize(
        ("value", "text"), [(Temperature(99), "99"), (Temperature(99.5), "99.5"), (None, "—")]
    )
    def test_degrees(self, value: Temperature | None, text: str) -> None:
        assert views.degrees(value) == text

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (time(0, 0), "12:00 AM"),
            (time(1, 5), "1:05 AM"),
            (time(12, 0), "12:00 PM"),
            (time(15, 0), "3:00 PM"),
            (time(23, 59), "11:59 PM"),
        ],
    )
    def test_time_of_day(self, value: time, text: str) -> None:
        assert views.time_of_day(value) == text

    def test_date_and_time_is_on_the_owners_clock(self) -> None:
        # 03:30 UTC on the 7th is 10:30 PM on the 6th in Detroit.
        instant = datetime(2026, 1, 7, 3, 30, tzinfo=UTC)

        assert views.date_and_time(instant, ZONE) == "Jan 6, 10:30 PM"

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [
            (5, "a moment ago"),
            (200, "3 minutes ago"),
            (3 * 3600, "3 hours ago"),
            (3 * 86400, "3 days ago"),
        ],
    )
    def test_ago(self, seconds: float, text: str) -> None:
        assert views.ago(seconds) == text

    def test_hours_have_one_decimal(self) -> None:
        assert views.hours(timedelta(hours=3, minutes=27)) == "3.5"
        assert views.hours(timedelta(0)) == "0.0"


class TestTubView:
    def test_shows_the_tub_as_reported(self) -> None:
        tub = views.tub_view(status(heater=HeaterState.HEATING))

        assert (tub.water, tub.target, tub.floor, tub.ceiling) == ("97", "99", "85", "99")
        assert (tub.heater, tub.heating) == ("Heating", True)
        assert (tub.heat_mode, tub.pump1, tub.pump2) == ("ready", "low", "off")
        assert tub.problem is None

    def test_no_reading_is_a_dash(self) -> None:
        assert views.tub_view(status(water=None)).water == "—"

    def test_the_wifi_modules_short_silences_are_not_mentioned(self) -> None:
        assert views.tub_view(status(silent_for=25)).problem is None

    def test_a_longer_silence_is_said_plainly(self) -> None:
        tub = views.tub_view(status(silent_for=300))

        assert tub.problem == (
            "The spa cannot be reached, so these values may be out of date. "
            "It was last heard from 5 minutes ago."
        )
        assert tub.water == "97"  # the last known value is still shown

    def test_a_closed_connection_is_said_at_once(self) -> None:
        assert views.tub_view(status(connected=False, silent_for=3)).problem is not None

    def test_a_spa_never_heard_from(self) -> None:
        tub = views.tub_view(SpaStatus(None, BAND, False, None, None))

        assert not tub.known
        assert (tub.water, tub.pump1, tub.heat_mode) == ("—", None, None)
        assert (tub.floor, tub.ceiling) == ("85", "99")
        assert tub.problem == "The spa has not been reached yet. Still trying."


def planned(*, started: bool = False, late: bool = False) -> PlannedHeating:
    def local(hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 1, 6, 5, 0, tzinfo=UTC) + timedelta(hours=hour, minutes=minute)

    use = PlannedUse(date(2026, 1, 6), time(18, 0), NOON, local(13, 12) if started else None)
    return PlannedHeating(
        use=use,
        use_at=local(18),
        ready_at=local(17),
        heater_start=local(13, 15),
        ends_at=local(21),
        expected_ready_at=local(17, 40) if late else local(16, 45),
    )


class TestPlanDayView:
    def test_a_day_with_nothing_planned(self) -> None:
        day = views.plan_day_view("tomorrow", date(2026, 1, 7), None, NOON, ZONE)

        assert (day.label, day.day, day.use_time, day.summary) == (
            "Tomorrow",
            "Wed, Jan 7",
            "",
            None,
        )

    def test_a_planned_day_gives_the_timetable(self) -> None:
        day = views.plan_day_view("today", date(2026, 1, 6), planned(), NOON, ZONE)

        assert day.use_time == "18:00"
        assert day.summary == (
            "Heater starts at about 1:15 PM; ready by 5:00 PM; kept warm until 9:00 PM."
        )
        assert not day.late

    def test_once_heating_has_begun_it_says_since_when(self) -> None:
        day = views.plan_day_view("today", date(2026, 1, 6), planned(started=True), NOON, ZONE)

        assert day.summary is not None
        assert day.summary.startswith("Heating since 1:12 PM;")

    def test_a_late_plan_says_when_it_will_be_warm(self) -> None:
        day = views.plan_day_view("today", date(2026, 1, 6), planned(late=True), NOON, ZONE)

        assert day.late
        assert day.summary is not None
        assert "warm at about 5:40 PM, later than wanted" in day.summary


def test_the_weekend_switch_says_when_it_applies() -> None:
    weekend = views.weekend_view(
        WeekendReady(True, time(15, 0), BlackoutWindow(time(1, 0), time(12, 0)))
    )

    assert weekend.enabled
    assert weekend.description == (
        "Keeps the tub at the ceiling from Friday 3:00 PM to Sunday midnight, "
        "without heating between 1:00 AM and 12:00 PM."
    )


class TestRuntimeView:
    def report(self, recording_since: datetime | None) -> RuntimeReport:
        def window(days: int) -> RuntimeWindow:
            totals = RuntimeTotals(
                window_start=NOON - timedelta(days=days),
                window_end=NOON,
                heater=timedelta(hours=days),
                pump1_low=timedelta(hours=2),
                pump1_high=timedelta(minutes=30),
                pump2=timedelta(minutes=6),
            )
            return RuntimeWindow(days, totals)

        return RuntimeReport(NOON, recording_since, (window(1), window(7)))

    def test_one_row_per_window(self) -> None:
        runtime = views.runtime_view(self.report(NOON - timedelta(days=30)), ZONE)

        assert [row.label for row in runtime.rows] == ["24 hours", "7 days"]
        first = runtime.rows[0]
        assert (first.heater, first.pump1, first.pump1_high, first.pump2) == (
            "1.0",
            "2.5",
            "0.5",
            "0.1",
        )
        assert not any(row.partial for row in runtime.rows)
        assert runtime.note is None

    def test_a_window_reaching_back_before_the_records_is_marked(self) -> None:
        runtime = views.runtime_view(self.report(NOON - timedelta(days=3)), ZONE)

        assert [row.partial for row in runtime.rows] == [False, True]
        assert runtime.note is not None
        assert "Records begin Jan 3, 12:00 PM" in runtime.note

    def test_with_nothing_recorded_it_says_so(self) -> None:
        runtime = views.runtime_view(self.report(None), ZONE)

        assert all(row.partial for row in runtime.rows)
        assert runtime.note == "Nothing has been recorded yet."


class TestAlertViews:
    def test_an_active_fault_can_be_acknowledged(self) -> None:
        alert = Alert(AlertKind.SPA_FAULT, Severity.CRITICAL, "The spa logged: X.", NOON, id=4)

        (view,) = views.alert_views([alert], ZONE)

        assert (view.id, view.title, view.critical, view.active) == (4, "Spa fault", True, True)
        assert (view.raised, view.cleared) == ("Jan 6, 12:00 PM", None)
        assert view.can_acknowledge

    def test_an_alert_that_clears_by_itself_has_no_button(self) -> None:
        alert = Alert(AlertKind.STALE_DATA, Severity.WARNING, "Quiet.", NOON, id=1)

        (view,) = views.alert_views([alert], ZONE)

        assert not view.critical
        assert not view.can_acknowledge

    def test_a_fault_already_acknowledged_has_no_button(self) -> None:
        over = NOON + timedelta(hours=2)
        alert = Alert(AlertKind.SPA_FAULT, Severity.CRITICAL, "X.", NOON, over, id=2)

        (view,) = views.alert_views([alert], ZONE)

        assert (view.active, view.cleared, view.can_acknowledge) == (False, "Jan 6, 2:00 PM", False)


class TestTimeChoices:
    def test_every_quarter_hour_in_twelve_hour_form(self) -> None:
        choices = views.TIME_CHOICES

        assert len(choices) == 96
        assert choices[0] == ("00:00", "12:00 AM")
        assert ("07:00", "7:00 AM") in choices
        assert ("19:00", "7:00 PM") in choices
        assert ("19:15", "7:15 PM") in choices
        assert choices[-1] == ("23:45", "11:45 PM")

    def test_a_planned_time_off_the_quarter_hour_is_still_offered(self) -> None:
        odd = PlannedUse(date(2026, 1, 6), time(18, 10), NOON)
        heating = PlannedHeating(odd, NOON, NOON, NOON, NOON, NOON)

        day = views.plan_day_view("today", date(2026, 1, 6), heating, NOON, ZONE)

        values = [value for value, _ in day.choices]
        assert "18:10" in values
        assert values == sorted(values)

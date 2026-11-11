"""Runtime reports and the heating rate, over synthetic and simulated histories."""

from __future__ import annotations

from datetime import timedelta

import pytest

from hottub.domain import Device, PumpSpeed, RunInterval, Temperature, TemperatureSample
from hottub.telemetry import TelemetryService
from tests.fakes.clock import FakeClock
from tests.fakes.repositories import (
    InMemoryRunIntervalRepository,
    InMemoryTemperatureSampleRepository,
)
from tests.unit.telemetry.rig import HOUR, MINUTE, NOON, SEED_RATE, Rig

DAY = timedelta(days=1)


def service(
    intervals: list[RunInterval], samples: list[TemperatureSample] | None = None
) -> TelemetryService:
    """Return a service for which it is NOON, over the given history."""
    return TelemetryService(
        InMemoryRunIntervalRepository(intervals),
        InMemoryTemperatureSampleRepository(samples or []),
        FakeClock(NOON),
        SEED_RATE,
    )


def sample(at_offset: timedelta, water: float) -> TemperatureSample:
    return TemperatureSample(NOON + at_offset, Temperature(water), Temperature(99.0))


class TestRuntime:
    def test_reports_each_window_in_the_order_asked(self) -> None:
        report = service([]).runtime([30, 1, 7])

        assert report.as_of == NOON
        assert [window.days for window in report.windows] == [30, 1, 7]
        assert report.windows[1].totals.window_start == NOON - DAY
        assert report.windows[1].totals.window_end == NOON

    def test_counts_an_interval_in_every_window_that_reaches_it(self) -> None:
        history = [
            RunInterval(Device.HEATER, None, NOON - 3 * HOUR, NOON - 1 * HOUR),
            RunInterval(Device.HEATER, None, NOON - 3 * DAY, NOON - 3 * DAY + HOUR),
            RunInterval(Device.HEATER, None, NOON - 10 * DAY, NOON - 10 * DAY + 4 * HOUR),
            RunInterval(Device.HEATER, None, NOON - 20 * DAY, NOON - 20 * DAY + 8 * HOUR),
        ]

        one, seven, fourteen, thirty = service(history).runtime([1, 7, 14, 30]).windows

        assert one.totals.heater == 2 * HOUR
        assert seven.totals.heater == 3 * HOUR
        assert fourteen.totals.heater == 7 * HOUR
        assert thirty.totals.heater == 15 * HOUR

    def test_an_interval_across_the_start_of_a_window_counts_in_part(self) -> None:
        history = [
            # Began 25 hours ago and ran for three hours.
            RunInterval(Device.HEATER, None, NOON - 25 * HOUR, NOON - 22 * HOUR),
            # Straddles the start of the seven-day window by an hour each way.
            RunInterval(Device.PUMP2, None, NOON - 7 * DAY - HOUR, NOON - 7 * DAY + HOUR),
        ]

        one, seven, fourteen = service(history).runtime([1, 7, 14]).windows

        assert one.totals.heater == 2 * HOUR
        assert seven.totals.heater == 3 * HOUR
        assert one.totals.pump2 == timedelta(0)
        assert seven.totals.pump2 == 1 * HOUR
        assert fourteen.totals.pump2 == 2 * HOUR

    def test_splits_pump_1_by_speed(self) -> None:
        history = [
            RunInterval(Device.PUMP1, PumpSpeed.LOW, NOON - 5 * HOUR, NOON - 3 * HOUR),
            RunInterval(Device.PUMP1, PumpSpeed.HIGH, NOON - 3 * HOUR, NOON - 2.5 * HOUR),
        ]

        [day] = service(history).runtime([1]).windows

        assert day.totals.pump1_low == 2 * HOUR
        assert day.totals.pump1_high == 30 * MINUTE
        assert day.totals.pump1 == 2.5 * HOUR

    def test_says_how_far_back_the_record_goes(self) -> None:
        assert service([]).runtime([1]).recording_since is None

        recorded = service([], [sample(-2 * DAY, 99.0), sample(-1 * DAY, 99.0)])
        assert recorded.runtime([1]).recording_since == NOON - 2 * DAY

    async def test_includes_a_device_that_is_running_now(self) -> None:
        rig = Rig()
        await rig.start()
        await rig.spa.set_pump2(True)
        rig.run(10)

        [day] = rig.service.runtime([1]).windows

        assert day.totals.pump2 == 10 * MINUTE

    async def test_an_outage_does_not_inflate_the_total(self) -> None:
        rig = Rig(water=85.0)  # the heater runs throughout
        await rig.start()
        rig.run(20)
        rig.spa.drop_connection()
        rig.run(60)
        rig.spa.restore_connection()
        rig.run(20)

        [day] = rig.service.runtime([1]).windows

        assert day.totals.heater == 40 * MINUTE


class TestHeatingRate:
    def test_is_the_assumption_until_there_is_history(self) -> None:
        rate = service([]).heating_rate()

        assert rate.f_per_hour == SEED_RATE
        assert not rate.measured
        assert rate.based_on == timedelta(0)

    async def test_is_measured_once_the_tub_has_heated_for_long_enough(self) -> None:
        rig = Rig(water=85.0)  # the simulated tub gains 5 °F an hour
        await rig.start()

        rig.run(60)
        assert not rig.service.heating_rate().measured

        rig.run(150)  # reaches 99 °F after 168 minutes in all
        rate = rig.service.heating_rate()

        assert rate.measured
        assert rate.f_per_hour == pytest.approx(5.0, abs=0.1)
        assert rate.based_on == pytest.approx(168 * MINUTE, abs=2 * MINUTE)

    async def test_is_not_thrown_off_by_short_top_ups(self) -> None:
        rig = Rig(water=85.0)
        await rig.start()
        rig.run(180)  # the long first heat-up is over
        measured = rig.service.heating_rate()

        # Two days of holding the temperature: many short runs of the heater.
        rig.run(2 * 24 * 60)

        assert len(rig.intervals_of(Device.HEATER)) > 10
        assert rig.service.heating_rate() == measured

    def test_forgets_heating_from_more_than_thirty_days_ago(self) -> None:
        began = -31 * DAY
        history = [RunInterval(Device.HEATER, None, NOON + began, NOON + began + 3 * HOUR)]
        samples = [sample(began + n * MINUTE, 85.0 + n / 12) for n in range(181)]

        assert not service(history, samples).heating_rate().measured


class TestLatestWater:
    def no_reading(self, at_offset: timedelta) -> TemperatureSample:
        return TemperatureSample(NOON + at_offset, None, Temperature(99.0))

    def test_is_the_newest_reading_on_record(self) -> None:
        telemetry = service([], [sample(-3 * MINUTE, 97.0), sample(-MINUTE, 98.0)])

        assert telemetry.latest_water(HOUR) == Temperature(98.0)

    def test_looks_past_samples_taken_while_the_spa_had_no_reading(self) -> None:
        telemetry = service([], [sample(-2 * MINUTE, 98.0), self.no_reading(-MINUTE)])

        assert telemetry.latest_water(HOUR) == Temperature(98.0)

    def test_a_reading_older_than_asked_for_does_not_count(self) -> None:
        telemetry = service([], [sample(-2 * HOUR, 98.0), self.no_reading(-MINUTE)])

        assert telemetry.latest_water(HOUR) is None
        assert telemetry.latest_water(3 * HOUR) == Temperature(98.0)

    def test_is_none_when_nothing_is_on_record(self) -> None:
        assert service([]).latest_water(HOUR) is None

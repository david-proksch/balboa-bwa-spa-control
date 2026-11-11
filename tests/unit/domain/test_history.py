"""Runtime totals and the heating-rate measurement, on hand-written histories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hottub.domain import (
    Device,
    PumpSpeed,
    RunInterval,
    Temperature,
    TemperatureSample,
    measure_heating_rate,
    total_runtime,
)

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def ran(
    device: Device, start: datetime, duration: timedelta, speed: PumpSpeed | None = None
) -> RunInterval:
    return RunInterval(device, speed, start, start + duration)


def readings(
    start: datetime, duration: timedelta, from_f: float, to_f: float
) -> list[TemperatureSample]:
    """One sample a minute, rising evenly, in whole degrees as the spa reports."""
    minutes = int(duration / MINUTE)
    return [
        TemperatureSample(
            at=start + minute * MINUTE,
            water=Temperature(from_f + (to_f - from_f) * minute / minutes).to_whole_degree(),
            set_temperature=Temperature(99.0),
        )
        for minute in range(minutes + 1)
    ]


class TestOverlap:
    WINDOW = (NOON, NOON + 10 * HOUR)

    @pytest.mark.parametrize(
        ("start", "duration", "expected"),
        [
            (NOON + 2 * HOUR, 3 * HOUR, 3 * HOUR),  # wholly inside
            (NOON - 2 * HOUR, 3 * HOUR, 1 * HOUR),  # across the start
            (NOON + 9 * HOUR, 3 * HOUR, 1 * HOUR),  # across the end
            (NOON - 1 * HOUR, 12 * HOUR, 10 * HOUR),  # across both
            (NOON - 5 * HOUR, 3 * HOUR, 0 * HOUR),  # before
            (NOON + 11 * HOUR, 3 * HOUR, 0 * HOUR),  # after
            (NOON - 3 * HOUR, 3 * HOUR, 0 * HOUR),  # ends as the window starts
        ],
    )
    def test_counts_only_the_part_inside_the_window(
        self, start: datetime, duration: timedelta, expected: timedelta
    ) -> None:
        assert ran(Device.HEATER, start, duration).overlap(*self.WINDOW) == expected


class TestTotalRuntime:
    def test_adds_up_each_device_separately(self) -> None:
        totals = total_runtime(
            [
                ran(Device.HEATER, NOON, 2 * HOUR),
                ran(Device.HEATER, NOON + 5 * HOUR, 1 * HOUR),
                ran(Device.PUMP1, NOON, 3 * HOUR, PumpSpeed.LOW),
                ran(Device.PUMP1, NOON + 3 * HOUR, 30 * MINUTE, PumpSpeed.HIGH),
                ran(Device.PUMP2, NOON + 3 * HOUR, 20 * MINUTE),
            ],
            NOON,
            NOON + 24 * HOUR,
        )

        assert totals.heater == 3 * HOUR
        assert totals.pump1_low == 3 * HOUR
        assert totals.pump1_high == 30 * MINUTE
        assert totals.pump1 == 3 * HOUR + 30 * MINUTE
        assert totals.pump2 == 20 * MINUTE

    def test_clips_intervals_at_both_edges_of_the_window(self) -> None:
        totals = total_runtime(
            [
                ran(Device.HEATER, NOON - 1 * HOUR, 3 * HOUR),  # 2 h inside
                ran(Device.HEATER, NOON + 23 * HOUR, 3 * HOUR),  # 1 h inside
                ran(Device.PUMP2, NOON - 2 * HOUR, 1 * HOUR),  # outside
            ],
            NOON,
            NOON + 24 * HOUR,
        )

        assert totals.heater == 3 * HOUR
        assert totals.pump2 == timedelta(0)

    def test_is_zero_with_no_history(self) -> None:
        totals = total_runtime([], NOON, NOON + HOUR)

        assert (totals.heater, totals.pump1, totals.pump2) == (timedelta(0),) * 3


class TestMeasureHeatingRate:
    def test_is_the_rise_divided_by_the_time(self) -> None:
        rate = measure_heating_rate(
            [ran(Device.HEATER, NOON, 3 * HOUR)], readings(NOON, 3 * HOUR, 85, 97)
        )

        assert rate is not None
        assert rate.measured
        assert rate.f_per_hour == pytest.approx(4.0)
        assert rate.based_on == 3 * HOUR

    def test_needs_two_hours_of_heating_in_all(self) -> None:
        rate = measure_heating_rate(
            [ran(Device.HEATER, NOON, 90 * MINUTE)], readings(NOON, 90 * MINUTE, 85, 91)
        )

        assert rate is None

    def test_adds_several_runs_together(self) -> None:
        later = NOON + 24 * HOUR
        rate = measure_heating_rate(
            [ran(Device.HEATER, NOON, 90 * MINUTE), ran(Device.HEATER, later, 90 * MINUTE)],
            readings(NOON, 90 * MINUTE, 85, 91) + readings(later, 90 * MINUTE, 90, 96),
        )

        assert rate is not None
        assert rate.f_per_hour == pytest.approx(4.0)
        assert rate.based_on == 3 * HOUR

    def test_ignores_runs_too_short_to_measure(self) -> None:
        # Twelve runs of twenty minutes are four hours of heating, but each
        # rise is about one degree and the spa reports whole degrees.
        starts = [NOON + n * HOUR for n in range(12)]
        rate = measure_heating_rate(
            [ran(Device.HEATER, start, 20 * MINUTE) for start in starts],
            [s for start in starts for s in readings(start, 20 * MINUTE, 98, 99)],
        )

        assert rate is None

    def test_counts_a_run_during_which_the_water_did_not_warm(self) -> None:
        later = NOON + 24 * HOUR
        rate = measure_heating_rate(
            [ran(Device.HEATER, NOON, 2 * HOUR), ran(Device.HEATER, later, 2 * HOUR)],
            readings(NOON, 2 * HOUR, 85, 93) + readings(later, 2 * HOUR, 90, 90),
        )

        assert rate is not None
        assert rate.f_per_hour == pytest.approx(2.0)

    @pytest.mark.parametrize(("from_f", "to_f"), [(85, 86), (60, 104)])
    def test_does_not_believe_an_implausible_rate(self, from_f: float, to_f: float) -> None:
        rate = measure_heating_rate(
            [ran(Device.HEATER, NOON, 2 * HOUR)], readings(NOON, 2 * HOUR, from_f, to_f)
        )

        assert rate is None

    def test_uses_only_the_readings_taken_while_heating(self) -> None:
        # The tub cools for an hour, heats for three, then cools again.
        samples = (
            readings(NOON - HOUR, 59 * MINUTE, 90, 85)
            + readings(NOON, 3 * HOUR, 85, 97)
            + readings(NOON + 3 * HOUR + MINUTE, HOUR, 97, 90)
        )

        rate = measure_heating_rate([ran(Device.HEATER, NOON, 3 * HOUR)], samples)

        assert rate is not None
        assert rate.f_per_hour == pytest.approx(4.0)

    def test_skips_minutes_without_a_water_reading(self) -> None:
        samples = readings(NOON, 3 * HOUR, 85, 97)
        blank = [
            TemperatureSample(sample.at, None, sample.set_temperature) for sample in samples[:30]
        ]

        rate = measure_heating_rate([ran(Device.HEATER, NOON, 3 * HOUR)], blank + samples[30:])

        assert rate is not None
        assert rate.f_per_hour == pytest.approx(4.0)
        assert rate.based_on == 150 * MINUTE

    def test_ignores_the_pumps(self) -> None:
        rate = measure_heating_rate(
            [ran(Device.PUMP1, NOON, 3 * HOUR, PumpSpeed.LOW)], readings(NOON, 3 * HOUR, 85, 97)
        )

        assert rate is None

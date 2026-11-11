"""The scheduling rules, checked against tables of instants.

Every time below is wall-clock time in Detroit unless it says UTC. January
2026 is used for the ordinary cases (the 9th is a Friday); the daylight
saving cases use 8 March 2026 (clocks go forward at 02:00) and 1 November
2026 (clocks go back at 02:00).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from hottub.domain import (
    BlackoutWindow,
    PlannedUse,
    PlannedUsePolicy,
    Reason,
    Schedule,
    Situation,
    Temperature,
    TemperatureBand,
    WeekendReadyPolicy,
    heat_up_time,
    local_instant,
)

ZONE = ZoneInfo("America/Detroit")
BAND = TemperatureBand(floor=Temperature(85), ceiling=Temperature(99))
BLACKOUT = BlackoutWindow(time(1, 0), time(12, 0))
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
WEEK = timedelta(days=8)


def local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Return the UTC instant of a wall-clock time in Detroit in 2026."""
    return local_instant(date(2026, month, day), time(hour, minute), ZONE)


def situation(
    now: datetime,
    *,
    water: float | None = 85,
    uses: tuple[PlannedUse, ...] = (),
    weekend_ready: bool = False,
    rate: float = 4.0,
) -> Situation:
    return Situation(
        now=now,
        zone=ZONE,
        band=BAND,
        water=None if water is None else Temperature(water),
        heating_rate_f_per_hour=rate,
        planned_uses=uses,
        weekend_ready=weekend_ready,
    )


def use(month: int, day: int, hour: int, minute: int = 0) -> PlannedUse:
    return PlannedUse(date(2026, month, day), time(hour, minute), created_at=local(1, 1, 0))


def schedule() -> Schedule:
    return Schedule([PlannedUsePolicy(), WeekendReadyPolicy(time(15, 0), BLACKOUT)])


class TestLocalInstant:
    def test_follows_the_offset_of_the_day(self) -> None:
        assert local(1, 5, 12) == datetime(2026, 1, 5, 17, 0, tzinfo=UTC)  # winter, UTC-5
        assert local(7, 5, 12) == datetime(2026, 7, 5, 16, 0, tzinfo=UTC)  # summer, UTC-4

    def test_a_time_the_clocks_skip_is_taken_an_hour_later(self) -> None:
        # 02:30 does not exist on 8 March; it means 03:30 summer time.
        assert local(3, 8, 2, 30) == local(3, 8, 3, 30)

    def test_a_time_that_happens_twice_means_the_first(self) -> None:
        # 01:30 on 1 November happens at 05:30 and 06:30 UTC.
        assert local(11, 1, 1, 30) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


class TestBlackoutWindow:
    @pytest.mark.parametrize(
        ("hour", "minute", "inside"),
        [
            (0, 59, False),
            (1, 0, True),
            (6, 0, True),
            (11, 59, True),
            (12, 0, False),
            (23, 0, False),
        ],
    )
    def test_start_is_inside_and_end_is_outside(self, hour: int, minute: int, inside: bool) -> None:
        assert BLACKOUT.contains(local(1, 10, hour, minute), ZONE) is inside

    @pytest.mark.parametrize(
        ("hour", "inside"), [(21, False), (22, True), (0, True), (5, True), (6, False), (12, False)]
    )
    def test_a_window_may_cross_midnight(self, hour: int, inside: bool) -> None:
        overnight = BlackoutWindow(time(22, 0), time(6, 0))

        assert overnight.contains(local(1, 10, hour), ZONE) is inside

    def test_equal_start_and_end_is_never(self) -> None:
        never = BlackoutWindow(time(3, 0), time(3, 0))

        assert not any(never.contains(local(1, 10, hour), ZONE) for hour in range(24))

    def test_is_judged_on_the_owners_clock_not_utc(self) -> None:
        # 03:00 UTC is 22:00 the evening before in Detroit.
        assert not BLACKOUT.contains(datetime(2026, 1, 10, 3, 0, tzinfo=UTC), ZONE)


class TestHeatUpTime:
    def test_is_the_gap_divided_by_the_rate(self) -> None:
        assert heat_up_time(Temperature(85), Temperature(99), 4.0) == timedelta(hours=3.5)

    def test_is_zero_for_water_already_warm_enough(self) -> None:
        assert heat_up_time(Temperature(100), Temperature(99), 4.0) == timedelta(0)

    def test_refuses_a_rate_of_zero(self) -> None:
        with pytest.raises(ValueError, match="greater than zero"):
            heat_up_time(Temperature(85), Temperature(99), 0.0)


class TestPlannedUsePolicy:
    """Use at 18:00, so ready at 17:00. From 85 °F at 4 °F/h heating takes 3.5 h;
    with the 15 minute margin the heater starts at 13:15. The hold ends at 21:00.
    """

    policy = PlannedUsePolicy()
    six_pm = use(1, 6, 18)

    @pytest.mark.parametrize(
        ("hour", "minute", "applies"),
        [
            (8, 0, False),
            (13, 14, False),
            (13, 15, True),
            (17, 0, True),
            (20, 59, True),
            (21, 0, False),
            (23, 0, False),
        ],
    )
    def test_applies_from_heater_start_to_the_end_of_the_use(
        self, hour: int, minute: int, applies: bool
    ) -> None:
        decision = self.policy.decide(situation(local(1, 6, hour, minute), uses=(self.six_pm,)))

        assert (decision is not None) is applies
        if decision is not None:
            assert decision.target == BAND.ceiling
            assert decision.reason is Reason.PLANNED_USE
            assert decision.heating_wanted

    def test_works_out_the_timetable(self) -> None:
        planned = self.policy.heating_for(self.six_pm, situation(local(1, 6, 8)))

        assert planned.use_at == local(1, 6, 18)
        assert planned.ready_at == local(1, 6, 17)
        assert planned.heater_start == local(1, 6, 13, 15)
        assert planned.ends_at == local(1, 6, 21)
        assert planned.expected_ready_at == local(1, 6, 16, 45)
        assert not planned.late

    @pytest.mark.parametrize(
        ("water", "start"),
        [
            (85, (13, 15)),
            (91, (14, 45)),  # 8 °F to gain: 2 h
            (99, (16, 45)),  # nothing to gain: the margin only
            (103, (16, 45)),  # warmer than the ceiling is not negative time
            (81, (12, 15)),  # colder than the floor: start earlier still
            (None, (13, 15)),  # no reading: assume the floor
        ],
    )
    def test_warmer_water_starts_later(self, water: float | None, start: tuple[int, int]) -> None:
        planned = self.policy.heating_for(self.six_pm, situation(local(1, 6, 8), water=water))

        assert planned.heater_start == local(1, 6, *start)

    def test_a_slower_heater_starts_earlier(self) -> None:
        planned = self.policy.heating_for(self.six_pm, situation(local(1, 6, 8), rate=2.0))

        assert planned.heater_start == local(1, 6, 9, 45)

    def test_once_heating_has_begun_the_start_no_longer_moves(self) -> None:
        # Without the recorded start, water at 97 °F would put the start
        # at 16:15 and the policy would stop applying at 15:00.
        begun = use(1, 6, 18)
        begun = PlannedUse(begun.day, begun.use_time, begun.created_at, local(1, 6, 13, 15))
        now = situation(local(1, 6, 15), water=97, uses=(begun,))

        assert self.policy.heating_for(begun, now).heater_start == local(1, 6, 13, 15)
        assert self.policy.decide(now) is not None

    def test_a_use_too_soon_to_meet_is_late(self) -> None:
        # At 16:00 with 14 °F to gain, warm at 19:30: after the 17:00 wanted.
        now = situation(local(1, 6, 16), uses=(self.six_pm,))
        decision = self.policy.decide(now)

        assert decision is not None
        assert decision.planned is not None
        assert decision.planned.late
        assert decision.planned.expected_ready_at == local(1, 6, 19, 30)

    @pytest.mark.parametrize(("water", "late"), [(99, False), (98, False), (97, True)])
    def test_warm_water_is_never_late(self, water: float, late: bool) -> None:
        # 18:30, during the use: the ready time of 17:00 is long past, and
        # the thermostat lets held water drop a degree before it heats.
        begun = PlannedUse(date(2026, 1, 6), time(18, 0), local(1, 1, 0), local(1, 6, 13, 15))
        planned = self.policy.heating_for(begun, situation(local(1, 6, 18, 30), water=water))

        assert planned.late is late

    def test_the_hold_is_as_long_as_the_owner_chose(self) -> None:
        short = replace(situation(local(1, 6, 19, 30), uses=(self.six_pm,)), use_duration=HOUR)

        assert self.policy.heating_for(self.six_pm, short).ends_at == local(1, 6, 19)
        assert self.policy.decide(short) is None

    def test_a_late_evening_use_runs_past_midnight(self) -> None:
        late_evening = use(1, 6, 23, 30)

        assert self.policy.decide(situation(local(1, 7, 2, 29), uses=(late_evening,))) is not None
        assert self.policy.decide(situation(local(1, 7, 2, 30), uses=(late_evening,))) is None

    def test_the_earlier_of_two_overlapping_uses_is_reported(self) -> None:
        uses = (use(1, 7, 0, 30), use(1, 6, 23, 0))
        decision = self.policy.decide(situation(local(1, 6, 23, 45), uses=uses))

        assert decision is not None
        assert decision.planned is not None
        assert decision.planned.use.day == date(2026, 1, 6)

    def test_heating_time_is_real_time_when_the_clocks_go_forward(self) -> None:
        # Use at 08:00 on the morning the clocks skip 02:00 to 03:00.
        planned = self.policy.heating_for(use(3, 8, 8), situation(local(3, 7, 12)))

        assert planned.use_at == datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
        assert planned.heater_start == planned.use_at - timedelta(hours=4, minutes=45)
        # 4 h 45 min of real time before 08:00 is 02:15 by the old clock.
        assert planned.heater_start == datetime(2026, 3, 8, 7, 15, tzinfo=UTC)

    def test_heating_time_is_real_time_when_the_clocks_go_back(self) -> None:
        planned = self.policy.heating_for(use(11, 1, 8), situation(local(10, 31, 12)))

        assert planned.use_at == datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
        assert planned.heater_start == planned.use_at - timedelta(hours=4, minutes=45)


class TestWeekendReadyPolicy:
    policy = WeekendReadyPolicy(time(15, 0), BLACKOUT)

    @pytest.mark.parametrize(
        ("day", "hour", "minute", "expected"),
        [
            (8, 20, 0, None),  # Thursday evening
            (9, 2, 0, None),  # Friday in the small hours: not the weekend yet
            (9, 14, 59, None),
            (9, 15, 0, Reason.WEEKEND_READY),  # Friday afternoon: it begins
            (9, 23, 59, Reason.WEEKEND_READY),
            (10, 0, 59, Reason.WEEKEND_READY),
            (10, 1, 0, Reason.WEEKEND_BLACKOUT),  # Saturday 01:00
            (10, 11, 59, Reason.WEEKEND_BLACKOUT),
            (10, 12, 0, Reason.WEEKEND_READY),  # Saturday noon
            (11, 0, 30, Reason.WEEKEND_READY),
            (11, 1, 0, Reason.WEEKEND_BLACKOUT),  # Sunday 01:00
            (11, 12, 0, Reason.WEEKEND_READY),
            (11, 23, 59, Reason.WEEKEND_READY),
            (12, 0, 0, None),  # Monday 00:00: it is over
            (12, 13, 0, None),
        ],
    )
    def test_weekend_and_blackout_edges(
        self, day: int, hour: int, minute: int, expected: Reason | None
    ) -> None:
        decision = self.policy.decide(situation(local(1, day, hour, minute), weekend_ready=True))

        assert (None if decision is None else decision.reason) is expected
        if decision is not None:
            heating = expected is Reason.WEEKEND_READY
            assert decision.target == (BAND.ceiling if heating else BAND.floor)
            assert decision.heating_wanted is heating

    def test_does_not_apply_when_switched_off(self) -> None:
        assert self.policy.decide(situation(local(1, 10, 14), weekend_ready=False)) is None

    @pytest.mark.parametrize(
        ("instant", "expected"),
        [
            # Sunday 8 March: 01:59 EST is followed by 03:00 EDT.
            (datetime(2026, 3, 8, 5, 30, tzinfo=UTC), Reason.WEEKEND_READY),  # 00:30 EST
            (datetime(2026, 3, 8, 6, 0, tzinfo=UTC), Reason.WEEKEND_BLACKOUT),  # 01:00 EST
            (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), Reason.WEEKEND_BLACKOUT),  # 03:00 EDT
            (datetime(2026, 3, 8, 15, 59, tzinfo=UTC), Reason.WEEKEND_BLACKOUT),  # 11:59 EDT
            (datetime(2026, 3, 8, 16, 0, tzinfo=UTC), Reason.WEEKEND_READY),  # 12:00 EDT
            (datetime(2026, 3, 9, 3, 59, tzinfo=UTC), Reason.WEEKEND_READY),  # Sun 23:59 EDT
            (datetime(2026, 3, 9, 4, 0, tzinfo=UTC), None),  # Monday 00:00 EDT
            # Sunday 1 November: 01:59 EDT is followed by 01:00 EST.
            (datetime(2026, 11, 1, 4, 59, tzinfo=UTC), Reason.WEEKEND_READY),  # 00:59 EDT
            (datetime(2026, 11, 1, 5, 0, tzinfo=UTC), Reason.WEEKEND_BLACKOUT),  # 01:00 EDT
            (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), Reason.WEEKEND_BLACKOUT),  # 01:30 EST
            (datetime(2026, 11, 1, 16, 59, tzinfo=UTC), Reason.WEEKEND_BLACKOUT),  # 11:59 EST
            (datetime(2026, 11, 1, 17, 0, tzinfo=UTC), Reason.WEEKEND_READY),  # 12:00 EST
            (datetime(2026, 11, 2, 4, 59, tzinfo=UTC), Reason.WEEKEND_READY),  # Sun 23:59 EST
            (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), None),  # Monday 00:00 EST
        ],
    )
    def test_follows_the_wall_clock_across_daylight_saving(
        self, instant: datetime, expected: Reason | None
    ) -> None:
        decision = self.policy.decide(situation(instant, weekend_ready=True))

        assert (None if decision is None else decision.reason) is expected


class TestSchedule:
    def test_is_idle_at_the_floor_when_nothing_is_planned(self) -> None:
        decision = schedule().decide(situation(local(1, 6, 12)))

        assert (decision.target, decision.reason) == (BAND.floor, Reason.IDLE)
        assert not decision.heating_wanted
        assert decision.instead_of is None

    def test_a_planned_use_overrides_the_blackout(self) -> None:
        # Saturday 08:00 use: heating from 03:15, inside the blackout.
        now = situation(local(1, 10, 4), uses=(use(1, 10, 8),), weekend_ready=True)
        decision = schedule().decide(now)

        assert (decision.target, decision.reason) == (BAND.ceiling, Reason.PLANNED_USE)
        assert decision.instead_of is Reason.WEEKEND_BLACKOUT

    def test_the_blackout_returns_when_the_planned_use_is_over(self) -> None:
        now = situation(local(1, 10, 11, 30), uses=(use(1, 10, 8),), weekend_ready=True)

        assert schedule().decide(now).reason is Reason.WEEKEND_BLACKOUT

    def test_a_planned_use_on_a_weekday_would_otherwise_be_idle(self) -> None:
        decision = schedule().decide(situation(local(1, 6, 17), uses=(use(1, 6, 18),)))

        assert decision.instead_of is Reason.IDLE


class TestNextChange:
    @pytest.mark.parametrize(
        ("now", "at", "reason"),
        [
            (local(1, 7, 9), local(1, 9, 15), Reason.WEEKEND_READY),  # Wednesday
            (local(1, 9, 16), local(1, 10, 1), Reason.WEEKEND_BLACKOUT),  # Friday
            (local(1, 10, 2), local(1, 10, 12), Reason.WEEKEND_READY),  # Saturday
            (local(1, 10, 13), local(1, 11, 1), Reason.WEEKEND_BLACKOUT),
            (local(1, 11, 13), local(1, 12, 0), Reason.IDLE),  # Sunday
            (local(1, 12, 0), local(1, 16, 15), Reason.WEEKEND_READY),  # Monday
        ],
    )
    def test_walks_through_a_weekend(self, now: datetime, at: datetime, reason: Reason) -> None:
        change = schedule().next_change(situation(now, weekend_ready=True), WEEK)

        assert change is not None
        assert (change.at, change.decision.reason) == (at, reason)

    def test_is_the_start_of_heating_for_a_planned_use(self) -> None:
        change = schedule().next_change(situation(local(1, 6, 8), uses=(use(1, 6, 18),)), WEEK)

        assert change is not None
        assert change.at == local(1, 6, 13, 15)
        assert change.decision.target == BAND.ceiling
        assert change.decision.planned is not None

    def test_is_the_end_of_the_use_while_heating(self) -> None:
        change = schedule().next_change(situation(local(1, 6, 15), uses=(use(1, 6, 18),)), WEEK)

        assert change is not None
        assert (change.at, change.decision.reason) == (local(1, 6, 21), Reason.IDLE)

    def test_skips_a_change_of_reason_that_changes_nothing_at_the_tub(self) -> None:
        # Saturday 13:00, weekend ready; a use at 18:00 starts and ends
        # while the weekend holds the ceiling anyway.
        now = situation(local(1, 10, 13), water=99, uses=(use(1, 10, 18),), weekend_ready=True)
        change = schedule().next_change(now, WEEK)

        assert change is not None
        assert (change.at, change.decision.reason) == (local(1, 11, 1), Reason.WEEKEND_BLACKOUT)

    def test_a_planned_use_in_the_blackout_is_the_next_change(self) -> None:
        now = situation(local(1, 10, 2), uses=(use(1, 10, 8),), weekend_ready=True)
        change = schedule().next_change(now, WEEK)

        assert change is not None
        assert (change.at, change.decision.reason) == (local(1, 10, 3, 15), Reason.PLANNED_USE)

    def test_is_none_when_nothing_is_planned(self) -> None:
        assert schedule().next_change(situation(local(1, 6, 8)), WEEK) is None

    def test_does_not_look_past_the_horizon(self) -> None:
        now = situation(local(1, 6, 8), uses=(use(1, 6, 18),))

        assert schedule().next_change(now, 2 * HOUR) is None

    def test_finds_the_weekend_across_a_clock_change(self) -> None:
        # Wednesday 4 March (EST); the Friday is before the change, and the
        # Sunday blackout ends at noon summer time.
        friday = schedule().next_change(situation(local(3, 4, 9), weekend_ready=True), WEEK)
        sunday_noon = schedule().next_change(situation(local(3, 8, 3), weekend_ready=True), WEEK)

        assert friday is not None
        assert friday.at == local(3, 6, 15)
        assert sunday_noon is not None
        assert sunday_noon.at == datetime(2026, 3, 8, 16, 0, tzinfo=UTC)

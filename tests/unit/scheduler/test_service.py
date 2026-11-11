"""The scheduler acting on a simulated tub, minute by minute.

Times are wall-clock time in Detroit. Each test starts on Tuesday
6 January 2026 at 08:00 unless it says otherwise; Friday is the 9th.

The simulated tub gains 5 °F an hour while heating; the scheduler is told
4 °F an hour, as a cautious starting assumption would be.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from hottub.control import ControlService
from hottub.domain import (
    Actor,
    BlackoutWindow,
    CommandRejectedError,
    HeatingRate,
    HeatMode,
    InvalidPlanError,
    PlannedUsePolicy,
    Reason,
    SpaState,
    Temperature,
    TemperatureBand,
    WeekendReadyPolicy,
    local_instant,
)
from hottub.scheduler import SchedulerService
from hottub.spa import FakeSpaAdapter
from tests.fakes.clock import FakeClock
from tests.fakes.repositories import (
    InMemoryCommandLog,
    InMemoryPlanRepository,
    InMemorySettingsRepository,
)

ZONE = ZoneInfo("America/Detroit")
BAND = TemperatureBand(floor=Temperature(85), ceiling=Temperature(99))
TUESDAY = date(2026, 1, 6)
WEDNESDAY = date(2026, 1, 7)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """Return the UTC instant of a wall-clock time in Detroit in January 2026."""
    return local_instant(date(2026, 1, day), time(hour, minute), ZONE)


class RefusingSpa(FakeSpaAdapter):
    """A simulated spa that can be told to refuse a command or to report no reading."""

    refuses = False
    no_reading = False

    @property
    def state(self) -> SpaState | None:
        state = super().state
        if state is not None and self.no_reading:
            return replace(state, water_temperature=None)
        return state

    async def set_temperature(self, temperature: Temperature) -> None:
        if self.refuses:
            raise CommandRejectedError("The spa did not take the temperature")
        await super().set_temperature(temperature)


class Rig:
    """A scheduler wired to a simulated tub, with everything exposed.

    The tub starts idle: water and set temperature both at the floor.
    """

    def __init__(
        self,
        *,
        start: datetime | None = None,
        water: float = 85,
        set_temperature: float = 85,
        heat_mode: HeatMode = HeatMode.READY,
        enabled: bool = True,
    ) -> None:
        self.clock = FakeClock(start or local(6, 8))
        self.spa = RefusingSpa(
            self.clock,
            water=Temperature(water),
            set_temperature=Temperature(set_temperature),
            heat_mode=heat_mode,
        )
        self.log = InMemoryCommandLog()
        self.control = ControlService(
            self.spa, InMemorySettingsRepository(), self.log, self.clock, BAND
        )
        self.plans = InMemoryPlanRepository()
        self.recorded_water: Temperature | None = None
        """What the history last recorded, for when the spa has no reading."""
        self.enabled = enabled
        self.scheduler = self.new_scheduler()

    def new_scheduler(self) -> SchedulerService:
        """Return another scheduler over the same plans, as after a restart."""
        return SchedulerService(
            self.control,
            self.plans,
            lambda: HeatingRate(4.0, measured=False),
            self.clock,
            ZONE,
            PlannedUsePolicy(),
            WeekendReadyPolicy(time(15, 0), BlackoutWindow(time(1, 0), time(12, 0))),
            recent_water=lambda: self.recorded_water,
            enabled=self.enabled,
        )

    @property
    def state(self) -> SpaState:
        assert self.spa.state is not None
        return self.spa.state

    @property
    def is_warm(self) -> bool:
        """Whether the water is at the ceiling, give or take the thermostat's degree.

        Like a real tub, the simulated one lets the water fall a degree
        below the set temperature before it heats again.
        """
        water = self.state.water_temperature
        return water is not None and water.fahrenheit >= BAND.ceiling.fahrenheit - 1

    def heat_mode(self) -> HeatMode:
        """Return the tub's heat mode as it is this moment."""
        return self.state.heat_mode

    @property
    def sent(self) -> list[tuple[datetime, str]]:
        """Every command sent so far: when, and what was asked for."""
        return [(record.issued_at, record.argument) for record in self.log.records]

    async def run_until(self, instant: datetime) -> None:
        """Let time pass up to ``instant``, with a pass of the scheduler every minute."""
        while self.clock.now() < instant:
            self.clock.advance(minutes=1)
            if self.spa.connected:
                self.spa.tick()
            await self.scheduler.reconcile()


@pytest.fixture
async def rig() -> Rig:
    rig = Rig()
    await rig.spa.connect()
    return rig


class TestIdle:
    async def test_lowers_a_tub_left_at_the_ceiling_to_the_floor(self) -> None:
        rig = Rig(water=99, set_temperature=99)
        await rig.spa.connect()

        await rig.scheduler.reconcile()

        assert rig.state.set_temperature == BAND.floor
        assert [record.actor for record in rig.log.records] == [Actor.SCHEDULER]

    async def test_sends_nothing_when_the_tub_is_already_as_wanted(self, rig: Rig) -> None:
        await rig.run_until(local(6, 12))

        assert rig.sent == []

    async def test_puts_right_a_change_made_at_the_tubs_own_panel(self, rig: Rig) -> None:
        await rig.spa.set_temperature(Temperature(102))

        await rig.run_until(local(6, 8, 1))

        assert rig.state.set_temperature == BAND.floor

    async def test_follows_a_change_of_the_floor(self, rig: Rig) -> None:
        await rig.control.set_floor(Temperature(88), Actor.USER)

        await rig.scheduler.reconcile()

        assert rig.state.set_temperature == Temperature(88)


class TestPlannedUse:
    async def test_the_tub_is_warm_an_hour_before_the_use(self, rig: Rig) -> None:
        planned = await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        assert planned.ready_at == local(6, 17)
        assert not planned.late

        await rig.run_until(local(6, 17))

        assert rig.is_warm

    async def test_heats_once_and_lets_go_once(self, rig: Rig) -> None:
        # The tub hovers a degree under the floor while idle, so the start
        # is worked out from 84 or 85 °F: between 13:00 and 13:15.
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))

        await rig.run_until(local(6, 23))

        (up_at, up), (down_at, down) = rig.sent
        assert (up, down) == ("99 °F", "85 °F")
        assert local(6, 13) <= up_at <= local(6, 13, 15)
        assert down_at == local(6, 21)

    async def test_the_start_is_recorded_when_heating_begins(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        await rig.run_until(local(6, 12))
        assert rig.plans.planned_use(TUESDAY).heating_started_at is None  # type: ignore[union-attr]

        await rig.run_until(local(6, 15))

        started = rig.plans.planned_use(TUESDAY).heating_started_at  # type: ignore[union-attr]
        assert started is not None
        assert rig.sent == [(started, "99 °F")]

    async def test_a_use_planned_too_late_heats_at_once_and_says_so(self, rig: Rig) -> None:
        planned = await rig.scheduler.plan_use(TUESDAY, time(9, 0))

        assert planned.late
        assert planned.use.heating_started_at == local(6, 8)
        assert rig.state.set_temperature == BAND.ceiling
        assert rig.scheduler.outlook().warnings == (
            "The tub cannot be ready by 8:00 AM for the planned use at 9:00 AM: "
            "it is expected to be warm at about 11:30 AM.",
        )

    async def test_a_use_that_would_already_be_over_is_refused(self, rig: Rig) -> None:
        # At 08:00 a use at 05:00 ended at 08:00.
        with pytest.raises(InvalidPlanError, match="already be over"):
            await rig.scheduler.plan_use(TUESDAY, time(5, 0))

        assert rig.plans.planned_use(TUESDAY) is None

    async def test_a_use_still_under_way_is_accepted(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(5, 1))

        assert rig.state.set_temperature == BAND.ceiling

    async def test_a_new_plan_for_the_day_replaces_the_old(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        planned = await rig.scheduler.plan_use(TUESDAY, time(21, 0))

        assert planned.use_at == local(6, 21)
        assert rig.scheduler.planned_heating(TUESDAY) == planned

    async def test_cancelling_lets_the_tub_cool_again(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        await rig.run_until(local(6, 15))
        assert rig.state.set_temperature == BAND.ceiling

        await rig.scheduler.cancel_use(TUESDAY)

        assert rig.state.set_temperature == BAND.floor
        assert rig.scheduler.planned_heating(TUESDAY) is None

    async def test_tomorrow_mornings_use_heats_through_the_night(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(WEDNESDAY, time(6, 30))

        await rig.run_until(local(7, 5, 30))

        assert rig.is_warm
        assert local(7, 1, 30) <= rig.sent[0][0] <= local(7, 1, 45)

    async def test_the_hold_time_is_three_hours_until_the_owner_chooses(self, rig: Rig) -> None:
        planned = await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        assert rig.scheduler.hold_time() == timedelta(hours=3)
        assert planned.ends_at == local(6, 21)

        await rig.scheduler.set_hold_time(timedelta(minutes=90))

        # The plan already made follows the new choice.
        again = rig.scheduler.planned_heating(TUESDAY)
        assert again is not None
        assert again.ends_at == local(6, 19, 30)
        assert rig.new_scheduler().hold_time() == timedelta(minutes=90)

    async def test_a_shorter_hold_lets_the_tub_go_at_once(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        await rig.run_until(local(6, 20))
        assert rig.state.set_temperature == BAND.ceiling

        await rig.scheduler.set_hold_time(timedelta(hours=1))

        assert rig.state.set_temperature == BAND.floor

    @pytest.mark.parametrize("minutes", [0, 29, 721])
    async def test_a_hold_time_out_of_range_is_refused(self, rig: Rig, minutes: int) -> None:
        with pytest.raises(InvalidPlanError, match="between 30 minutes and 12 hours"):
            await rig.scheduler.set_hold_time(timedelta(minutes=minutes))

        assert rig.scheduler.hold_time() == timedelta(hours=3)

    async def test_old_plans_are_forgotten_on_request(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        await rig.scheduler.plan_use(WEDNESDAY, time(18, 0))

        rig.scheduler.forget_plans_before(WEDNESDAY)

        assert rig.scheduler.planned_heating(TUESDAY) is None
        assert rig.scheduler.planned_heating(WEDNESDAY) is not None

    async def test_today_is_the_owners_day_not_utcs(self) -> None:
        # 22:00 on Tuesday in Detroit is already Wednesday in UTC.
        rig = Rig(start=local(6, 22))

        assert rig.scheduler.today() == TUESDAY


class TestWhenTheSpaHasNoReading:
    """The spa reports no water temperature for some seconds now and then."""

    async def test_a_momentary_gap_does_not_start_heating_hours_early(self) -> None:
        # Seen on the real tub: at 99 °F with a use at 20:00, the heater
        # is due at 18:45. A pass made while the reading was missing took
        # the water for 85 °F and began heating at 17:02.
        rig = Rig(start=local(6, 17), water=99)
        await rig.spa.connect()
        await rig.scheduler.plan_use(TUESDAY, time(20, 0))
        rig.recorded_water = Temperature(99)
        rig.spa.no_reading = True

        planned = await rig.scheduler.plan_use(WEDNESDAY, time(20, 0))  # triggers a pass

        assert rig.sent == []
        assert planned.use.heating_started_at is None
        today = rig.scheduler.planned_heating(TUESDAY)
        assert today is not None
        assert today.heater_start == local(6, 18, 45)
        assert today.use.heating_started_at is None

    async def test_with_nothing_recorded_lately_the_floor_is_assumed(self) -> None:
        # In Rest mode the spa can go hours without a reading. Then the
        # coldest the tub should be is the safe assumption.
        rig = Rig(start=local(6, 17), water=99)
        await rig.spa.connect()
        rig.spa.no_reading = True

        planned = await rig.scheduler.plan_use(TUESDAY, time(20, 0))

        assert planned.use.heating_started_at == local(6, 17)
        assert rig.state.set_temperature == BAND.ceiling

    async def test_the_spas_own_reading_is_preferred_to_the_record(self, rig: Rig) -> None:
        rig.recorded_water = Temperature(99)  # an older reading; the spa says 85 °F

        planned = await rig.scheduler.plan_use(TUESDAY, time(18, 0))

        assert planned.heater_start == local(6, 13, 15)


class TestWeekend:
    async def test_holds_the_ceiling_except_in_the_no_heating_hours(self) -> None:
        rig = Rig(start=local(9, 12))  # Friday noon
        await rig.spa.connect()
        await rig.scheduler.set_weekend_ready(True)

        await rig.run_until(local(12, 6))  # Monday morning

        assert rig.sent == [
            (local(9, 15), "99 °F"),
            (local(10, 1), "85 °F"),
            (local(10, 12), "99 °F"),
            (local(11, 1), "85 °F"),
            (local(11, 12), "99 °F"),
            (local(12, 0), "85 °F"),
        ]

    async def test_a_planned_use_heats_through_the_no_heating_hours(self) -> None:
        rig = Rig(start=local(10, 2))  # Saturday 02:00
        await rig.spa.connect()
        await rig.scheduler.set_weekend_ready(True)
        await rig.scheduler.plan_use(date(2026, 1, 10), time(8, 0))

        await rig.run_until(local(10, 7))
        assert rig.is_warm
        assert rig.scheduler.outlook().current.instead_of is Reason.WEEKEND_BLACKOUT

        await rig.run_until(local(10, 11, 1))  # the use is over; noon is not here yet
        assert rig.state.set_temperature == BAND.floor

    async def test_switching_it_on_mid_weekend_heats_at_once(self) -> None:
        rig = Rig(start=local(10, 14))  # Saturday afternoon
        await rig.spa.connect()

        weekend = await rig.scheduler.set_weekend_ready(True)

        assert weekend.enabled
        assert (weekend.start_time, weekend.blackout.start) == (time(15, 0), time(1, 0))
        assert rig.state.set_temperature == BAND.ceiling

    async def test_switching_it_off_lets_the_tub_cool(self) -> None:
        rig = Rig(start=local(10, 14))
        await rig.spa.connect()
        await rig.scheduler.set_weekend_ready(True)

        await rig.scheduler.set_weekend_ready(False)

        assert not rig.scheduler.weekend_ready().enabled
        assert rig.state.set_temperature == BAND.floor


class TestRestMode:
    """In Rest the spa would not heat, so the scheduler borrows Ready and gives it back."""

    async def test_switches_to_ready_to_heat_and_back_to_rest_after(self) -> None:
        rig = Rig(heat_mode=HeatMode.REST)
        await rig.spa.connect()
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))

        await rig.run_until(local(6, 17))
        assert rig.heat_mode() is HeatMode.READY
        assert rig.is_warm
        assert rig.plans.rest_to_restore()

        await rig.run_until(local(6, 21))
        assert rig.heat_mode() is HeatMode.REST
        assert not rig.plans.rest_to_restore()

    async def test_returns_to_rest_even_after_a_restart(self) -> None:
        rig = Rig(heat_mode=HeatMode.REST)
        await rig.spa.connect()
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        await rig.run_until(local(6, 17))

        rig.scheduler = rig.new_scheduler()
        await rig.run_until(local(6, 21))

        assert rig.state.heat_mode is HeatMode.REST

    async def test_a_tub_the_owner_keeps_in_ready_is_left_in_ready(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))

        await rig.run_until(local(6, 22))

        assert rig.state.heat_mode is HeatMode.READY
        assert [what for _, what in rig.sent] == ["99 °F", "85 °F"]

    async def test_a_tub_in_rest_with_nothing_planned_is_left_in_rest(self) -> None:
        rig = Rig(heat_mode=HeatMode.REST)
        await rig.spa.connect()

        await rig.run_until(local(6, 12))

        assert rig.state.heat_mode is HeatMode.REST
        assert rig.sent == []

    async def test_does_not_force_rest_on_an_owner_who_chose_it_back(self) -> None:
        # The owner switches to Rest by hand after the scheduler borrowed
        # Ready; when the use is over there is nothing left to give back.
        rig = Rig(heat_mode=HeatMode.REST)
        await rig.spa.connect()
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        await rig.run_until(local(6, 20, 59))
        await rig.control.set_heat_mode(HeatMode.REST, Actor.USER)

        await rig.run_until(local(6, 21, 5))

        assert rig.state.heat_mode is HeatMode.REST
        assert not rig.plans.rest_to_restore()
        assert [r.argument for r in rig.log.records if r.actor is Actor.SCHEDULER][-1] == "85 °F"


class TestWhenTheSpaIsTrouble:
    async def test_does_nothing_before_the_spa_has_ever_reported(self) -> None:
        rig = Rig(water=99, set_temperature=99)  # never connected

        await rig.scheduler.reconcile()

        assert rig.sent == []

    async def test_does_nothing_while_the_connection_is_down(self) -> None:
        rig = Rig(water=99, set_temperature=99)
        await rig.spa.connect()
        rig.spa.drop_connection()

        await rig.run_until(local(6, 9))

        assert rig.sent == []

    async def test_catches_up_when_the_connection_returns(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))
        rig.spa.drop_connection()
        await rig.run_until(local(6, 14))
        assert rig.sent == []

        rig.spa.restore_connection()
        await rig.run_until(local(6, 14, 1))

        assert rig.sent == [(local(6, 14, 1), "99 °F")]

    async def test_waits_longer_and_longer_after_a_refusal(self) -> None:
        rig = Rig(water=99, set_temperature=99)
        await rig.spa.connect()
        rig.spa.refuses = True

        await rig.scheduler.reconcile()
        await rig.run_until(local(6, 9))

        waits = [1, 2, 4, 8, 15, 15, 15]
        attempts = [local(6, 8) + timedelta(minutes=sum(waits[:n])) for n in range(len(waits) + 1)]
        assert [at for at, _ in rig.sent] == attempts
        assert not any(record.succeeded for record in rig.log.records)

    async def test_starts_afresh_once_the_spa_obeys(self) -> None:
        rig = Rig(water=99, set_temperature=99)
        await rig.spa.connect()
        rig.spa.refuses = True
        await rig.scheduler.reconcile()
        await rig.run_until(local(6, 8, 10))  # tried at :00, :01, :03, :07

        rig.spa.refuses = False
        await rig.run_until(local(6, 8, 15))

        assert rig.state.set_temperature == BAND.floor
        assert rig.sent[-1][0] == local(6, 8, 15)

    async def test_a_change_of_plan_is_tried_at_once(self) -> None:
        rig = Rig(water=99, set_temperature=99)
        await rig.spa.connect()
        rig.spa.refuses = True
        await rig.scheduler.reconcile()
        await rig.run_until(local(6, 8, 10))  # next try is due at :15
        rig.spa.refuses = False

        await rig.scheduler.plan_use(TUESDAY, time(21, 0))

        assert rig.state.set_temperature == BAND.floor


class TestSwitchedOff:
    async def test_stores_plans_but_sends_nothing(self) -> None:
        rig = Rig(water=99, set_temperature=99, enabled=False)
        await rig.spa.connect()

        planned = await rig.scheduler.plan_use(TUESDAY, time(9, 0))
        await rig.scheduler.set_weekend_ready(True)
        await rig.run_until(local(6, 10))

        assert rig.sent == []
        assert planned.use.heating_started_at is None
        assert rig.scheduler.planned_heating(TUESDAY) is not None

    async def test_the_outlook_says_so(self) -> None:
        rig = Rig(enabled=False)
        await rig.spa.connect()

        (warning,) = rig.scheduler.outlook().warnings

        assert "switched off" in warning


class TestOutlook:
    async def test_when_idle_with_nothing_planned(self, rig: Rig) -> None:
        outlook = rig.scheduler.outlook()

        assert outlook.as_of == local(6, 8)
        assert outlook.current.reason is Reason.IDLE
        assert outlook.current_text == "Idle: the tub may cool to 85 °F."
        assert outlook.next_change is None
        assert outlook.next_text is None
        assert outlook.warnings == ()

    async def test_before_heating_for_a_planned_use(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))

        outlook = rig.scheduler.outlook()

        assert outlook.next_change is not None
        assert outlook.next_change.at == local(6, 13, 15)
        assert outlook.next_text == (
            "Heater will start at about 1:15 PM to be ready by 5:00 PM for use at 6:00 PM."
        )

    async def test_while_heating_and_then_holding(self, rig: Rig) -> None:
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))

        await rig.run_until(local(6, 15))
        heating = rig.scheduler.outlook()
        await rig.run_until(local(6, 18, 30))
        holding = rig.scheduler.outlook()

        assert heating.current_text == "Heating to 99 °F for the planned use at 6:00 PM."
        assert heating.next_text == "The tub goes back to 85 °F at 9:00 PM."
        assert holding.current_text == "Holding 99 °F for the planned use until 9:00 PM."
        assert heating.warnings == holding.warnings == ()

    async def test_for_the_weekend(self) -> None:
        rig = Rig(start=local(8, 9))  # Thursday morning
        await rig.spa.connect()
        await rig.scheduler.set_weekend_ready(True)

        thursday = rig.scheduler.outlook()
        await rig.run_until(local(9, 16))
        friday = rig.scheduler.outlook()
        await rig.run_until(local(10, 2))
        saturday = rig.scheduler.outlook()

        assert thursday.next_text == "Heating to 99 °F for the weekend starts at tomorrow 3:00 PM."
        assert friday.current_text == "Keeping the tub at 99 °F for the weekend."
        assert friday.next_text == (
            "Heating pauses at tomorrow 1:00 AM for the weekend's no-heating hours."
        )
        assert saturday.current_text == "Weekend no-heating hours: the tub may cool to 85 °F."
        assert saturday.next_text == "Heating to 99 °F for the weekend starts at 12:00 PM."

    async def test_says_when_a_planned_use_overrides_the_no_heating_hours(self) -> None:
        rig = Rig(start=local(10, 4))  # Saturday 04:00
        await rig.spa.connect()
        await rig.scheduler.set_weekend_ready(True)
        await rig.scheduler.plan_use(date(2026, 1, 10), time(8, 0))

        assert rig.scheduler.outlook().current_text == (
            "Heating to 99 °F for the planned use at 8:00 AM. "
            "This overrides the weekend's no-heating hours."
        )

    async def test_warns_when_the_spa_cannot_be_reached(self, rig: Rig) -> None:
        rig.spa.drop_connection()

        assert rig.scheduler.outlook().warnings == (
            "The spa cannot be reached, so nothing can be carried out right now.",
        )

    async def test_works_before_the_spa_has_ever_reported(self) -> None:
        rig = Rig()  # never connected
        await rig.scheduler.plan_use(TUESDAY, time(18, 0))

        outlook = rig.scheduler.outlook()

        # With no reading the water is taken to be at the floor.
        assert outlook.next_change is not None
        assert outlook.next_change.at == local(6, 13, 15)

"""Behaviour of the simulated spa.

The simulated spa underpins the scheduler, telemetry and watchdog tests, so
its own behaviour is pinned down here first.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hottub.domain import (
    CommandRejectedError,
    HeaterState,
    HeatMode,
    PumpSpeed,
    SpaFault,
    SpaState,
    SpaStateChanged,
    SpaUnavailableError,
    Temperature,
)
from hottub.spa import FakeSpaAdapter
from tests.fakes.clock import FakeClock

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOON)


async def connected_spa(
    clock: FakeClock,
    *,
    water: float = 99,
    set_temperature: float = 99,
    heat_mode: HeatMode = HeatMode.READY,
) -> FakeSpaAdapter:
    """Build a spa with the default physics (heats 5 °F/h, cools 0.5 °F/h) and connect it."""
    spa = FakeSpaAdapter(
        clock,
        water=Temperature(water),
        set_temperature=Temperature(set_temperature),
        heat_mode=heat_mode,
    )
    await spa.connect()
    return spa


def state_of(spa: FakeSpaAdapter) -> SpaState:
    assert spa.state is not None
    return spa.state


class TestConnection:
    async def test_has_no_state_before_connecting(self, clock: FakeClock) -> None:
        spa = FakeSpaAdapter(clock)

        assert not spa.connected
        assert spa.state is None
        assert spa.last_message_at is None

    async def test_reports_its_state_on_connecting(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=99, set_temperature=99)

        assert spa.connected
        assert state_of(spa) == SpaState(
            observed_at=NOON,
            water_temperature=Temperature(99),
            set_temperature=Temperature(99),
            heat_mode=HeatMode.READY,
            heater=HeaterState.OFF,
            pump1=PumpSpeed.OFF,
            pump2_on=False,
        )
        assert spa.last_message_at == NOON

    async def test_commands_fail_while_disconnected(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        spa.drop_connection()

        with pytest.raises(SpaUnavailableError):
            await spa.set_temperature(Temperature(100))
        with pytest.raises(SpaUnavailableError):
            await spa.set_pump1(PumpSpeed.HIGH)
        with pytest.raises(SpaUnavailableError):
            await spa.set_pump2(True)
        with pytest.raises(SpaUnavailableError):
            await spa.set_heat_mode(HeatMode.REST)
        with pytest.raises(SpaUnavailableError):
            await spa.latest_fault()

    async def test_state_goes_stale_while_disconnected(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        spa.drop_connection()

        clock.advance(hours=1)
        spa.tick()

        assert state_of(spa).observed_at == NOON
        assert spa.last_message_at == NOON

    async def test_the_tub_carries_on_while_it_cannot_be_heard(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=90, set_temperature=100)
        spa.drop_connection()

        clock.advance(hours=1)
        spa.restore_connection()

        assert state_of(spa).water_temperature == Temperature(95)
        assert spa.last_message_at == clock.now()


class TestHeating:
    async def test_heats_at_the_configured_rate_until_the_set_temperature(
        self, clock: FakeClock
    ) -> None:
        spa = await connected_spa(clock, water=90, set_temperature=100)
        assert state_of(spa).heater is HeaterState.HEATING

        clock.advance(hours=1)
        spa.tick()
        assert state_of(spa).water_temperature == Temperature(95)
        assert state_of(spa).heater is HeaterState.HEATING

        clock.advance(hours=1, minutes=5)
        spa.tick()
        assert state_of(spa).water_temperature == Temperature(100)
        assert state_of(spa).heater is HeaterState.OFF

    async def test_cools_when_the_set_temperature_is_lower(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=99, set_temperature=99)
        await spa.set_temperature(Temperature(80))

        clock.advance(hours=4)
        spa.tick()

        assert state_of(spa).water_temperature == Temperature(97)
        assert state_of(spa).heater is HeaterState.OFF

    async def test_never_cools_below_ambient(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=51, set_temperature=50, heat_mode=HeatMode.REST)

        clock.advance(hours=24)
        spa.tick()

        assert state_of(spa).water_temperature == Temperature(50)

    async def test_holds_the_set_temperature_over_days(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=99, set_temperature=99)

        for _ in range(48):
            clock.advance(hours=1)
            spa.tick()
            water = state_of(spa).water_temperature
            assert water is not None
            assert Temperature(98) <= water <= Temperature(99)

    async def test_raising_the_set_temperature_starts_the_heater_at_once(
        self, clock: FakeClock
    ) -> None:
        spa = await connected_spa(clock, water=99, set_temperature=99)

        await spa.set_temperature(Temperature(102))

        assert state_of(spa).set_temperature == Temperature(102)
        assert state_of(spa).heater is HeaterState.HEATING

    async def test_rest_mode_does_not_heat(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=90, set_temperature=100, heat_mode=HeatMode.REST)

        clock.advance(hours=2)
        spa.tick()

        assert state_of(spa).heater is HeaterState.OFF
        assert state_of(spa).water_temperature == Temperature(89)

    async def test_switching_to_ready_resumes_heating(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=90, set_temperature=100, heat_mode=HeatMode.REST)

        await spa.set_heat_mode(HeatMode.READY)

        assert state_of(spa).heat_mode is HeatMode.READY
        assert state_of(spa).heater is HeaterState.HEATING

    async def test_a_failed_heater_claims_to_heat_but_the_water_does_not_warm(
        self, clock: FakeClock
    ) -> None:
        spa = await connected_spa(clock, water=90, set_temperature=100)
        spa.fail_heater()

        clock.advance(hours=2)
        spa.tick()

        # With no heat going in, the water cools as if the heater were off.
        assert state_of(spa).heater is HeaterState.HEATING
        assert state_of(spa).water_temperature == Temperature(89)

        spa.repair_heater()
        clock.advance(hours=1)
        spa.tick()
        assert state_of(spa).water_temperature == Temperature(94)


class TestSetTemperature:
    @pytest.mark.parametrize("fahrenheit", [49, 105])
    async def test_rejects_temperatures_the_controller_cannot_hold(
        self, clock: FakeClock, fahrenheit: float
    ) -> None:
        spa = await connected_spa(clock)

        with pytest.raises(CommandRejectedError):
            await spa.set_temperature(Temperature(fahrenheit))

        assert state_of(spa).set_temperature == Temperature(99)

    @pytest.mark.parametrize("fahrenheit", [50, 104])
    async def test_accepts_both_ends_of_the_controller_range(
        self, clock: FakeClock, fahrenheit: float
    ) -> None:
        spa = await connected_spa(clock)

        await spa.set_temperature(Temperature(fahrenheit))

        assert state_of(spa).set_temperature == Temperature(fahrenheit)

    async def test_rounds_to_whole_degrees(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)

        await spa.set_temperature(Temperature(101.5))

        assert state_of(spa).set_temperature == Temperature(102)


class TestPumps:
    async def test_pump1_runs_low_while_heating(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=90, set_temperature=100)

        assert state_of(spa).pump1 is PumpSpeed.LOW

    async def test_pump1_cannot_be_switched_off_while_heating(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=90, set_temperature=100)

        await spa.set_pump1(PumpSpeed.OFF)

        assert state_of(spa).pump1 is PumpSpeed.LOW

    async def test_pump1_can_run_high_while_heating(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock, water=90, set_temperature=100)

        await spa.set_pump1(PumpSpeed.HIGH)

        assert state_of(spa).pump1 is PumpSpeed.HIGH

    async def test_pump1_follows_requests_when_not_heating(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)

        await spa.set_pump1(PumpSpeed.HIGH)
        assert state_of(spa).pump1 is PumpSpeed.HIGH

        await spa.set_pump1(PumpSpeed.LOW)
        assert state_of(spa).pump1 is PumpSpeed.LOW

        await spa.set_pump1(PumpSpeed.OFF)
        assert state_of(spa).pump1 is PumpSpeed.OFF

    async def test_pump1_switches_itself_off_after_the_timeout(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        await spa.set_pump1(PumpSpeed.HIGH)

        clock.advance(minutes=14)
        spa.tick()
        assert state_of(spa).pump1 is PumpSpeed.HIGH

        clock.advance(minutes=1)
        spa.tick()
        assert state_of(spa).pump1 is PumpSpeed.OFF

    async def test_pump2_switches_on_and_off(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)

        await spa.set_pump2(True)
        assert state_of(spa).pump2_on

        await spa.set_pump2(False)
        assert not state_of(spa).pump2_on

    async def test_pump2_switches_itself_off_after_the_timeout(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        await spa.set_pump2(True)

        clock.advance(minutes=15)
        spa.tick()

        assert not state_of(spa).pump2_on

    async def test_switching_a_pump_on_again_restarts_its_timeout(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        await spa.set_pump2(True)
        clock.advance(minutes=10)
        await spa.set_pump2(True)

        clock.advance(minutes=10)
        spa.tick()

        assert state_of(spa).pump2_on


class TestListeners:
    async def test_notified_when_a_reading_changes(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        events: list[SpaStateChanged] = []
        spa.subscribe(events.append)

        await spa.set_pump2(True)

        assert len(events) == 1
        assert events[0].previous is not None
        assert not events[0].previous.pump2_on
        assert events[0].current.pump2_on

    async def test_first_state_after_connecting_has_no_previous(self, clock: FakeClock) -> None:
        spa = FakeSpaAdapter(clock)
        events: list[SpaStateChanged] = []
        spa.subscribe(events.append)

        await spa.connect()

        assert len(events) == 1
        assert events[0].previous is None

    async def test_not_notified_when_nothing_changes(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        events: list[SpaStateChanged] = []
        spa.subscribe(events.append)

        clock.advance(seconds=1)
        spa.tick()

        assert events == []
        assert spa.last_message_at == clock.now()

    async def test_unsubscribing_stops_notifications(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        events: list[SpaStateChanged] = []
        unsubscribe = spa.subscribe(events.append)

        unsubscribe()
        unsubscribe()  # harmless the second time
        await spa.set_pump2(True)

        assert events == []


class TestFaultLog:
    async def test_is_empty_on_a_healthy_tub(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)

        assert await spa.latest_fault() is None

    async def test_returns_the_newest_entry(self, clock: FakeClock) -> None:
        spa = await connected_spa(clock)
        older = SpaFault(code=16, message="The water flow is low", occurred_at=NOON)
        newer = SpaFault(code=30, message="The heater may be dry", occurred_at=NOON)
        spa.log_fault(older)
        spa.log_fault(newer)

        assert await spa.latest_fault() == newer

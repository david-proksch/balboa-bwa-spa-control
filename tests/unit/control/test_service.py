"""Behaviour of the control use cases, against the simulated spa."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hottub.control import ControlService
from hottub.domain import (
    Actor,
    HeatMode,
    InvalidTemperatureError,
    PumpSpeed,
    SpaState,
    SpaUnavailableError,
    Temperature,
    TemperatureBand,
)
from hottub.spa import FakeSpaAdapter
from tests.fakes.clock import FakeClock
from tests.fakes.repositories import InMemoryCommandLog, InMemorySettingsRepository

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
DEFAULT_BAND = TemperatureBand(floor=Temperature(85), ceiling=Temperature(99))


class Tub:
    """A control service wired to a simulated spa, with everything exposed."""

    def __init__(self, *, water: float = 99, set_temperature: float = 99) -> None:
        self.clock = FakeClock(NOON)
        self.spa = FakeSpaAdapter(
            self.clock, water=Temperature(water), set_temperature=Temperature(set_temperature)
        )
        self.settings = InMemorySettingsRepository()
        self.log = InMemoryCommandLog()
        self.control = ControlService(self.spa, self.settings, self.log, self.clock, DEFAULT_BAND)

    @property
    def state(self) -> SpaState:
        assert self.spa.state is not None
        return self.spa.state


@pytest.fixture
async def tub() -> Tub:
    tub = Tub()
    await tub.spa.connect()
    return tub


class TestStatus:
    async def test_reports_the_state_the_band_and_the_connection(self, tub: Tub) -> None:
        status = tub.control.status()

        assert status.state == tub.state
        assert status.band == DEFAULT_BAND
        assert status.connected

    async def test_reports_how_long_ago_the_spa_was_heard_from(self, tub: Tub) -> None:
        tub.spa.drop_connection()
        tub.clock.advance(seconds=42)

        status = tub.control.status()

        assert not status.connected
        assert status.last_message_at == NOON
        assert status.seconds_since_last_message == 42

    def test_before_the_spa_has_ever_reported_there_is_no_state(self) -> None:
        status = Tub().control.status()

        assert status.state is None
        assert status.last_message_at is None
        assert status.seconds_since_last_message is None
        assert not status.connected


class TestBand:
    async def test_the_default_band_applies_until_one_is_saved(self, tub: Tub) -> None:
        assert tub.control.band == DEFAULT_BAND

        await tub.control.set_floor(Temperature(90), Actor.USER)

        assert tub.control.band == TemperatureBand(Temperature(90), Temperature(99))
        assert tub.settings.load_band() == tub.control.band

    async def test_a_spa_set_to_the_ceiling_follows_the_ceiling(self, tub: Tub) -> None:
        status = await tub.control.set_ceiling(Temperature(97), Actor.USER)

        assert status.band.ceiling == Temperature(97)
        assert tub.state.set_temperature == Temperature(97)
        assert status.state == tub.state

    async def test_a_spa_set_to_the_floor_follows_the_floor(self) -> None:
        tub = Tub(set_temperature=85)
        await tub.spa.connect()

        await tub.control.set_floor(Temperature(88), Actor.USER)

        assert tub.state.set_temperature == Temperature(88)

    async def test_a_spa_set_inside_the_band_is_left_alone(self) -> None:
        tub = Tub(set_temperature=95)
        await tub.spa.connect()

        await tub.control.set_ceiling(Temperature(98), Actor.USER)
        await tub.control.set_floor(Temperature(90), Actor.USER)

        assert tub.state.set_temperature == Temperature(95)
        assert tub.log.records == []

    async def test_a_spa_left_above_a_lowered_ceiling_is_pulled_down_to_it(self) -> None:
        tub = Tub(set_temperature=95)
        await tub.spa.connect()

        await tub.control.set_ceiling(Temperature(93), Actor.USER)

        assert tub.state.set_temperature == Temperature(93)

    async def test_a_spa_left_below_a_raised_floor_is_pulled_up_to_it(self) -> None:
        tub = Tub(set_temperature=90)
        await tub.spa.connect()

        await tub.control.set_floor(Temperature(92), Actor.USER)

        assert tub.state.set_temperature == Temperature(92)

    async def test_fractions_are_rounded_to_whole_degrees(self, tub: Tub) -> None:
        await tub.control.set_ceiling(Temperature(97.5), Actor.USER)

        assert tub.control.band.ceiling == Temperature(98)

    @pytest.mark.parametrize("ceiling", [85, 80, 105])
    async def test_a_ceiling_that_makes_no_valid_band_changes_nothing(
        self, tub: Tub, ceiling: float
    ) -> None:
        with pytest.raises(InvalidTemperatureError):
            await tub.control.set_ceiling(Temperature(ceiling), Actor.USER)

        assert tub.control.band == DEFAULT_BAND
        assert tub.state.set_temperature == Temperature(99)
        assert tub.log.records == []

    @pytest.mark.parametrize("floor", [99, 100, 49])
    async def test_a_floor_that_makes_no_valid_band_changes_nothing(
        self, tub: Tub, floor: float
    ) -> None:
        with pytest.raises(InvalidTemperatureError):
            await tub.control.set_floor(Temperature(floor), Actor.USER)

        assert tub.control.band == DEFAULT_BAND

    async def test_the_band_is_saved_even_when_the_spa_is_disconnected(self, tub: Tub) -> None:
        tub.spa.drop_connection()

        status = await tub.control.set_ceiling(Temperature(97), Actor.USER)

        assert status.band.ceiling == Temperature(97)
        assert tub.state.set_temperature == Temperature(99)  # last known; not updated

    async def test_the_band_is_saved_even_when_the_spa_fails_mid_command(
        self, tub: Tub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fail(_temperature: Temperature) -> None:
            raise SpaUnavailableError("The spa went quiet")

        monkeypatch.setattr(tub.spa, "set_temperature", fail)

        status = await tub.control.set_ceiling(Temperature(97), Actor.USER)

        assert status.band.ceiling == Temperature(97)
        [record] = tub.log.records
        assert not record.succeeded
        assert record.result == "The spa went quiet"


class TestPumpsAndHeatMode:
    async def test_pump_1_runs_at_the_requested_speed(self, tub: Tub) -> None:
        status = await tub.control.set_pump1(PumpSpeed.HIGH, Actor.USER)

        assert status.state is not None
        assert status.state.pump1 is PumpSpeed.HIGH

    async def test_pump_2_switches_on_and_off(self, tub: Tub) -> None:
        assert (await tub.control.set_pump2(True, Actor.USER)).state == tub.state
        assert tub.state.pump2_on

        await tub.control.set_pump2(False, Actor.USER)
        assert not tub.state.pump2_on

    async def test_heat_mode_switches(self, tub: Tub) -> None:
        status = await tub.control.set_heat_mode(HeatMode.REST, Actor.USER)

        assert status.state is not None
        assert status.state.heat_mode is HeatMode.REST

    async def test_a_command_to_an_unreachable_spa_fails(self, tub: Tub) -> None:
        tub.spa.drop_connection()

        with pytest.raises(SpaUnavailableError):
            await tub.control.set_pump2(True, Actor.USER)


class TestAuditLog:
    async def test_every_command_is_recorded_with_who_asked_and_what_happened(
        self, tub: Tub
    ) -> None:
        tub.clock.advance(minutes=5)
        await tub.control.set_pump1(PumpSpeed.LOW, Actor.USER)
        await tub.control.set_heat_mode(HeatMode.REST, Actor.SCHEDULER)

        first, second = tub.log.records
        assert (first.command, first.argument, first.actor) == ("set_pump1", "low", Actor.USER)
        assert first.issued_at == tub.clock.now()
        assert first.succeeded
        assert first.result == "pump 1 is low"
        assert (second.command, second.argument) == ("set_heat_mode", "rest")
        assert second.actor is Actor.SCHEDULER
        assert second.result == "heat mode is rest"

    async def test_a_band_change_records_the_set_temperature_it_sent(self, tub: Tub) -> None:
        await tub.control.set_ceiling(Temperature(97), Actor.USER)

        [record] = tub.log.records
        assert (record.command, record.argument) == ("set_temperature", "97 °F")
        assert record.result == "set temperature is 97 °F"

    async def test_an_overruled_pump_request_is_recorded_as_such(self) -> None:
        tub = Tub(water=90, set_temperature=99)  # cold water, so the heater runs
        await tub.spa.connect()

        status = await tub.control.set_pump1(PumpSpeed.OFF, Actor.USER)

        assert status.state is not None
        assert status.state.pump1 is PumpSpeed.LOW
        [record] = tub.log.records
        assert record.succeeded
        assert record.result == "pump 1 is low; the spa overruled the request"

    async def test_a_failed_command_is_recorded_and_still_raised(self, tub: Tub) -> None:
        tub.spa.drop_connection()

        with pytest.raises(SpaUnavailableError):
            await tub.control.set_pump2(True, Actor.USER)

        [record] = tub.log.records
        assert not record.succeeded
        assert record.result == "The simulated spa is disconnected"

    async def test_recent_commands_come_newest_first_and_limited(self, tub: Tub) -> None:
        for speed in (PumpSpeed.LOW, PumpSpeed.HIGH, PumpSpeed.OFF):
            await tub.control.set_pump1(speed, Actor.USER)

        assert [record.argument for record in tub.control.recent_commands(2)] == ["off", "high"]

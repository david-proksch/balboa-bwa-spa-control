from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hottub.domain import HeaterState, HeatMode, PumpSpeed, SpaState, Temperature

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def a_state() -> SpaState:
    return SpaState(
        observed_at=NOON,
        water_temperature=Temperature(99),
        set_temperature=Temperature(102),
        heat_mode=HeatMode.READY,
        heater=HeaterState.HEATING,
        pump1=PumpSpeed.LOW,
        pump2_on=False,
    )


def test_is_heating_only_when_the_element_is_running() -> None:
    assert a_state().is_heating
    assert not replace(a_state(), heater=HeaterState.WAITING).is_heating
    assert not replace(a_state(), heater=HeaterState.OFF).is_heating


def test_same_readings_ignores_when_the_state_was_observed() -> None:
    later = replace(a_state(), observed_at=NOON + timedelta(seconds=1))

    assert later.same_readings_as(a_state())


def test_same_readings_detects_any_changed_reading() -> None:
    assert not replace(a_state(), pump2_on=True).same_readings_as(a_state())
    assert not replace(a_state(), water_temperature=None).same_readings_as(a_state())

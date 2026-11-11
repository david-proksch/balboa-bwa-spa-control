"""The watchdog's rules, each fed a tub minute by minute.

The band is 85 to 99 °F and the thresholds are the standard ones: too cold
below 82 °F, too hot above 101 °F, each for 30 minutes; 104 °F at once;
1 °F of rise expected within 45 minutes of a call for heat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from hottub.domain import (
    AlertCheck,
    AlertKind,
    HeaterState,
    HeatMode,
    NotHeatingCheck,
    Observation,
    PumpSpeed,
    Severity,
    SpaFault,
    SpaState,
    StaleDataCheck,
    Standing,
    Temperature,
    TemperatureBand,
    Verdict,
    WatchdogThresholds,
    WaterTooColdCheck,
    WaterTooHotCheck,
    fault_alert,
    standard_checks,
)

START = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
BAND = TemperatureBand(floor=Temperature(85), ceiling=Temperature(99))
THRESHOLDS = WatchdogThresholds()
OK, PROBLEM, UNKNOWN = Standing.OK, Standing.PROBLEM, Standing.UNKNOWN


def at(minute: float) -> datetime:
    return START + timedelta(minutes=minute)


def observe(
    minute: float,
    water: float | None = 99,
    *,
    set_to: float = 99,
    mode: HeatMode = HeatMode.READY,
    heater: HeaterState = HeaterState.OFF,
    pump1: PumpSpeed = PumpSpeed.OFF,
    pump2: bool = False,
    band: TemperatureBand = BAND,
) -> Observation:
    """Return what the watchdog sees ``minute`` minutes in, with fresh data."""
    state = SpaState(
        observed_at=at(minute),
        water_temperature=None if water is None else Temperature(water),
        set_temperature=Temperature(set_to),
        heat_mode=mode,
        heater=heater,
        pump1=pump1,
        pump2_on=pump2,
    )
    return Observation(at(minute), state, band, last_message_at=at(minute), watching_since=START)


def hold(check: AlertCheck, first: int, last: int, water: float | None, **tub: Any) -> Verdict:
    """Show ``check`` the same tub every minute from ``first`` to ``last``."""
    verdict = Verdict.unknown()
    for minute in range(first, last + 1):
        verdict = check.evaluate(observe(minute, water, **tub))
    return verdict


HEATING: dict[str, Any] = {"heater": HeaterState.HEATING, "pump1": PumpSpeed.LOW}


class TestValidWater:
    def test_a_reading_in_ready_mode_counts(self) -> None:
        assert observe(0, 97).valid_water == Temperature(97)

    def test_no_reading_is_no_reading(self) -> None:
        assert observe(0, None).valid_water is None

    def test_a_reading_in_rest_mode_counts_only_while_a_pump_runs(self) -> None:
        assert observe(0, 70, mode=HeatMode.REST).valid_water is None
        assert observe(0, 70, mode=HeatMode.REST, pump1=PumpSpeed.LOW).valid_water is not None
        assert observe(0, 70, mode=HeatMode.REST, pump2=True).valid_water is not None

    def test_a_spa_never_heard_from_has_no_reading(self) -> None:
        never = Observation(START, None, BAND, last_message_at=None, watching_since=START)

        assert never.valid_water is None
        assert not never.heater_running


class TestStaleData:
    check = StaleDataCheck(THRESHOLDS)

    def silent_for(self, seconds: float) -> Verdict:
        now = START + timedelta(seconds=seconds)
        return self.check.evaluate(
            Observation(now, None, BAND, last_message_at=START, watching_since=START)
        )

    @pytest.mark.parametrize(
        ("seconds", "standing", "severity"),
        [
            (0, OK, None),
            (25, OK, None),  # the Wi-Fi module's routine silence
            (89, OK, None),
            (90, PROBLEM, Severity.WARNING),
            (14 * 60 + 59, PROBLEM, Severity.WARNING),
            (15 * 60, PROBLEM, Severity.CRITICAL),
            (3600, PROBLEM, Severity.CRITICAL),
        ],
    )
    def test_silence_is_a_warning_and_then_critical(
        self, seconds: float, standing: Standing, severity: Severity | None
    ) -> None:
        verdict = self.silent_for(seconds)

        assert (verdict.standing, verdict.severity) == (standing, severity)

    def test_a_spa_never_heard_from_is_timed_from_the_start_of_the_watch(self) -> None:
        def never_heard(minute: float) -> Verdict:
            return self.check.evaluate(
                Observation(at(minute), None, BAND, last_message_at=None, watching_since=START)
            )

        assert never_heard(1).standing is OK
        assert never_heard(2).severity is Severity.WARNING
        assert never_heard(15).severity is Severity.CRITICAL

    def test_says_what_it_means_for_the_owner(self) -> None:
        assert "unknown" in self.silent_for(120).message
        assert "15 minutes" in self.silent_for(1000).message


class TestWaterTooCold:
    def test_raised_after_thirty_minutes_well_below_the_floor(self) -> None:
        check = WaterTooColdCheck(THRESHOLDS)

        assert hold(check, 0, 29, 81).standing is UNKNOWN
        verdict = check.evaluate(observe(30, 81))

        assert (verdict.standing, verdict.severity) == (PROBLEM, Severity.CRITICAL)
        assert verdict.message == (
            "The water is at 81 °F, more than 3 °F below the floor of 85 °F, and the heater is off."
        )

    @pytest.mark.parametrize(("water", "standing"), [(85, OK), (84, UNKNOWN), (82, UNKNOWN)])
    def test_a_little_below_the_floor_is_not_too_cold(
        self, water: float, standing: Standing
    ) -> None:
        assert hold(WaterTooColdCheck(THRESHOLDS), 0, 120, water).standing is standing

    def test_the_count_starts_again_after_a_warmer_reading(self) -> None:
        check = WaterTooColdCheck(THRESHOLDS)
        hold(check, 0, 29, 81)
        check.evaluate(observe(30, 82))

        assert hold(check, 31, 60, 81).standing is UNKNOWN
        assert check.evaluate(observe(61, 81)).standing is PROBLEM

    def test_waits_while_the_heater_is_running(self) -> None:
        # A refilled tub at 60 °F on its way up is not a fault.
        check = WaterTooColdCheck(THRESHOLDS)

        assert hold(check, 0, 240, 60, **HEATING).standing is UNKNOWN

    def test_an_alert_stays_until_the_water_is_back_at_the_floor(self) -> None:
        check = WaterTooColdCheck(THRESHOLDS)
        assert hold(check, 0, 30, 81).standing is PROBLEM

        # The heater comes on and the water climbs: not over yet.
        assert check.evaluate(observe(31, 81, **HEATING)).standing is UNKNOWN
        assert check.evaluate(observe(60, 84, **HEATING)).standing is UNKNOWN
        assert check.evaluate(observe(70, 85, **HEATING)).standing is OK

    def test_unknown_in_rest_mode_and_the_count_starts_again(self) -> None:
        check = WaterTooColdCheck(THRESHOLDS)
        hold(check, 0, 29, 81)

        assert hold(check, 30, 300, 70, mode=HeatMode.REST).standing is UNKNOWN
        assert hold(check, 301, 330, 81).standing is UNKNOWN
        assert check.evaluate(observe(331, 81)).standing is PROBLEM

    def test_follows_a_change_of_the_floor(self) -> None:
        low = TemperatureBand(Temperature(80), Temperature(99))

        assert hold(WaterTooColdCheck(THRESHOLDS), 0, 60, 81, band=low).standing is OK

    def test_suspending_starts_the_count_again(self) -> None:
        check = WaterTooColdCheck(THRESHOLDS)
        hold(check, 0, 29, 81)

        check.suspend()

        assert check.evaluate(observe(30, 81)).standing is UNKNOWN


class TestWaterTooHot:
    @pytest.mark.parametrize("water", [104, 105])
    def test_the_safety_limit_is_raised_at_once(self, water: float) -> None:
        verdict = WaterTooHotCheck(THRESHOLDS).evaluate(observe(0, water, set_to=104))

        assert (verdict.standing, verdict.severity) == (PROBLEM, Severity.CRITICAL)
        assert "safety limit of 104 °F" in verdict.message

    @pytest.mark.parametrize(("water", "standing"), [(99, OK), (100, UNKNOWN), (101, UNKNOWN)])
    def test_a_little_above_the_ceiling_is_not_too_hot(
        self, water: float, standing: Standing
    ) -> None:
        check = WaterTooHotCheck(THRESHOLDS)

        assert hold(check, 0, 120, water, set_to=104, **HEATING).standing is standing

    def test_raised_after_thirty_minutes_of_heating_above_the_limit(self) -> None:
        check = WaterTooHotCheck(THRESHOLDS)

        assert hold(check, 0, 29, 102, set_to=104, **HEATING).standing is UNKNOWN
        verdict = check.evaluate(observe(30, 102, set_to=104, **HEATING))

        assert (verdict.standing, verdict.severity) == (PROBLEM, Severity.CRITICAL)
        assert "more than 2 °F above the ceiling of 99 °F" in verdict.message

    def test_a_tub_cooling_after_its_ceiling_was_lowered_is_left_alone(self) -> None:
        # Held at 103 °F; the ceiling drops to 99 °F and the water takes
        # hours to follow. The heater is off and nothing gets warmer.
        check = WaterTooHotCheck(THRESHOLDS)

        assert hold(check, 0, 120, 103).standing is UNKNOWN
        assert hold(check, 121, 240, 102).standing is UNKNOWN
        assert hold(check, 241, 300, 99).standing is OK

    def test_water_that_gets_warmer_with_the_heater_off_is_not_cooling(self) -> None:
        check = WaterTooHotCheck(THRESHOLDS)
        hold(check, 0, 10, 102)

        assert hold(check, 11, 40, 103).standing is UNKNOWN
        assert check.evaluate(observe(41, 103)).standing is PROBLEM

    def test_an_alert_stays_until_the_water_is_back_at_the_ceiling(self) -> None:
        check = WaterTooHotCheck(THRESHOLDS)
        assert check.evaluate(observe(0, 104, set_to=104)).standing is PROBLEM

        assert check.evaluate(observe(60, 102)).standing is UNKNOWN
        assert check.evaluate(observe(120, 100)).standing is UNKNOWN
        assert check.evaluate(observe(180, 99)).standing is OK

    def test_unknown_without_a_trusted_reading(self) -> None:
        check = WaterTooHotCheck(THRESHOLDS)

        assert hold(check, 0, 60, None).standing is UNKNOWN
        assert hold(check, 61, 120, 104, mode=HeatMode.REST).standing is UNKNOWN


class TestNotHeating:
    def test_raised_when_a_running_heater_gains_nothing_in_45_minutes(self) -> None:
        check = NotHeatingCheck(THRESHOLDS)

        assert hold(check, 0, 44, 90, **HEATING).standing is UNKNOWN
        verdict = check.evaluate(observe(45, 90, **HEATING))

        assert (verdict.standing, verdict.severity) == (PROBLEM, Severity.CRITICAL)
        assert verdict.message == (
            "The spa is calling for heat, but the water has not risen above 90 °F in 45 minutes."
        )

    def test_water_that_falls_while_heat_is_called_for_is_not_a_rise(self) -> None:
        check = NotHeatingCheck(THRESHOLDS)
        hold(check, 0, 20, 90, **HEATING)

        assert hold(check, 21, 45, 89, **HEATING).standing is PROBLEM

    def test_each_degree_gained_starts_the_count_again(self) -> None:
        # A slow heater: one degree every 40 minutes is slow, not broken.
        check = NotHeatingCheck(THRESHOLDS)
        verdicts = [
            check.evaluate(observe(minute, 90 + minute // 40, **HEATING))
            for minute in range(0, 400)
        ]

        assert PROBLEM not in {verdict.standing for verdict in verdicts}
        assert verdicts[40].standing is OK  # the minute it gained a degree

    def test_a_spa_in_ready_far_below_its_set_temperature_is_calling_for_heat(self) -> None:
        # The heater is reported off, as with a tripped high-limit switch.
        check = NotHeatingCheck(THRESHOLDS)

        assert hold(check, 0, 45, 90, set_to=99).standing is PROBLEM

    @pytest.mark.parametrize("water", [97, 98, 99, 102])
    def test_near_or_above_the_set_temperature_no_heat_is_called_for(self, water: float) -> None:
        assert hold(NotHeatingCheck(THRESHOLDS), 0, 120, water, set_to=99).standing is OK

    def test_a_spa_in_rest_is_not_calling_for_heat(self) -> None:
        check = NotHeatingCheck(THRESHOLDS)
        circulating: dict[str, Any] = {"mode": HeatMode.REST, "pump1": PumpSpeed.LOW}

        assert hold(check, 0, 120, 70, set_to=99, **circulating).standing is OK
        assert hold(check, 121, 240, 70, set_to=99, mode=HeatMode.REST).standing is UNKNOWN

    def test_cleared_when_the_water_rises_after_all(self) -> None:
        check = NotHeatingCheck(THRESHOLDS)
        assert hold(check, 0, 45, 90, **HEATING).standing is PROBLEM

        assert check.evaluate(observe(46, 91, **HEATING)).standing is OK

    def test_cleared_when_heat_is_no_longer_called_for(self) -> None:
        check = NotHeatingCheck(THRESHOLDS)
        assert hold(check, 0, 45, 90, **HEATING).standing is PROBLEM

        assert check.evaluate(observe(46, 90, set_to=85)).standing is OK

    def test_the_heater_waiting_for_flow_counts_as_calling_for_heat(self) -> None:
        check = NotHeatingCheck(THRESHOLDS)

        assert hold(check, 0, 45, 98, heater=HeaterState.WAITING).standing is PROBLEM

    def test_suspending_starts_the_count_again(self) -> None:
        check = NotHeatingCheck(THRESHOLDS)
        hold(check, 0, 44, 90, **HEATING)

        check.suspend()

        assert hold(check, 45, 89, 90, **HEATING).standing is UNKNOWN
        assert check.evaluate(observe(90, 90, **HEATING)).standing is PROBLEM


class TestFaultAlert:
    def test_a_fault_is_critical(self) -> None:
        alert = fault_alert(SpaFault(16, "The water flow is low", at(-3)), noticed_at=at(0))

        assert (alert.kind, alert.severity) == (AlertKind.SPA_FAULT, Severity.CRITICAL)
        assert alert.message == "The spa logged: The water flow is low (code 16)."
        assert alert.raised_at == at(0)
        assert alert.active

    def test_priming_mode_is_routine(self) -> None:
        alert = fault_alert(SpaFault(19, "Priming Mode", at(-3)), noticed_at=at(0))

        assert alert.severity is Severity.WARNING


class TestKindsAndThresholds:
    def test_only_a_spa_fault_needs_acknowledging(self) -> None:
        assert [kind for kind in AlertKind if kind.needs_acknowledging] == [AlertKind.SPA_FAULT]

    def test_every_kind_has_a_title(self) -> None:
        assert all(kind.title for kind in AlertKind)

    def test_the_standard_checks_cover_the_state_rules(self) -> None:
        kinds = {check.kind for check in standard_checks(THRESHOLDS)}

        assert kinds == {
            AlertKind.WATER_TOO_COLD,
            AlertKind.WATER_TOO_HOT,
            AlertKind.NOT_HEATING,
        }

    @pytest.mark.parametrize(
        "figures",
        [
            {"stale_after": timedelta(0)},
            {"cold_margin_f": 0},
            {"expected_rise_f": -1},
            {"stale_after": timedelta(minutes=20)},  # after it would be critical
        ],
    )
    def test_figures_that_make_no_sense_are_refused(self, figures: dict[str, object]) -> None:
        with pytest.raises(ValueError, match=r"watchdog|stale"):
            WatchdogThresholds(**figures)  # type: ignore[arg-type]

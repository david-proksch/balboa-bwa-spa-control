"""The JSON API, end to end: HTTP in, simulated spa and a real database out.

The whole application is assembled by ``hottub.app.build_app`` exactly as in
production, except that the spa is simulated and the clock is the test's.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hottub.app import build_app
from hottub.config import AppConfig
from hottub.domain import (
    Alert,
    AlertKind,
    CommandRejectedError,
    Device,
    HeatMode,
    RunInterval,
    Severity,
    SpaFault,
    Temperature,
    TemperatureSample,
    fault_alert,
)
from hottub.persistence import (
    SqlAlertRepository,
    SqlRunIntervalRepository,
    SqlTemperatureSampleRepository,
    migrate,
    open_database,
)
from hottub.spa import FakeSpaAdapter
from tests.fakes.clock import FakeClock

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
API = "/hot-tub/api/v1"


class StubbornSpa(FakeSpaAdapter):
    """A simulated spa that can be told to refuse a change of heat mode."""

    refuses_heat_mode = False

    async def set_heat_mode(self, mode: HeatMode) -> None:
        if self.refuses_heat_mode:
            raise CommandRejectedError("The spa did not switch to rest")
        await super().set_heat_mode(mode)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOON)


@pytest.fixture
def spa(clock: FakeClock) -> StubbornSpa:
    return StubbornSpa(clock)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(spa_adapter="fake", database_path=tmp_path / "api.db")


@pytest.fixture
def client(config: AppConfig, spa: StubbornSpa, clock: FakeClock) -> Iterator[TestClient]:
    with TestClient(build_app(config, spa=spa, clock=clock)) as client:
        yield client


class TestStatus:
    def test_reports_the_tub(self, client: TestClient) -> None:
        response = client.get(f"{API}/status")

        assert response.status_code == 200
        assert response.json() == {
            "connected": True,
            "last_message_at": "2026-01-05T12:00:00Z",
            "seconds_since_last_message": 0.0,
            "water_temperature_f": 99.0,
            "target_temperature_f": 99.0,
            "ceiling_f": 99.0,
            "floor_f": 85.0,
            "heat_mode": "ready",
            "heater": "off",
            "pump1": "off",
            "pump2": "off",
        }

    def test_shows_stale_data_for_what_it_is(
        self, client: TestClient, spa: StubbornSpa, clock: FakeClock
    ) -> None:
        spa.drop_connection()
        clock.advance(minutes=2)

        body = client.get(f"{API}/status").json()

        assert body["connected"] is False
        assert body["seconds_since_last_message"] == 120.0
        assert body["water_temperature_f"] == 99.0  # last known

    def test_has_nulls_before_the_spa_has_ever_reported(
        self, config: AppConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unreachable = FakeSpaAdapter(clock)
        # "Connecting" leaves it disconnected, like a tub that is switched off.
        monkeypatch.setattr(unreachable, "connect", unreachable.disconnect)

        with TestClient(build_app(config, spa=unreachable, clock=clock)) as client:
            body = client.get(f"{API}/status").json()

        assert body["connected"] is False
        assert body["water_temperature_f"] is None
        assert body["pump2"] is None
        assert body["ceiling_f"] == 99.0

    def test_health_check_answers_even_when_the_spa_is_gone(
        self, client: TestClient, spa: StubbornSpa
    ) -> None:
        spa.drop_connection()

        response = client.get(f"{API}/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "spa_connected": False}


class TestTemperature:
    def test_lowering_the_ceiling_moves_the_spa_with_it(self, client: TestClient) -> None:
        response = client.put(f"{API}/temperature/ceiling", json={"fahrenheit": 97})

        assert response.status_code == 200
        assert response.json()["ceiling_f"] == 97.0
        assert response.json()["target_temperature_f"] == 97.0

    def test_changing_the_floor_leaves_a_spa_at_the_ceiling_alone(self, client: TestClient) -> None:
        response = client.put(f"{API}/temperature/floor", json={"fahrenheit": 90})

        assert response.json()["floor_f"] == 90.0
        assert response.json()["target_temperature_f"] == 99.0

    def test_the_band_survives_a_restart(
        self, config: AppConfig, clock: FakeClock, client: TestClient
    ) -> None:
        client.put(f"{API}/temperature/floor", json={"fahrenheit": 90})

        with TestClient(build_app(config, spa=FakeSpaAdapter(clock), clock=clock)) as restarted:
            assert restarted.get(f"{API}/status").json()["floor_f"] == 90.0

    @pytest.mark.parametrize(
        ("edge", "fahrenheit"), [("ceiling", 85), ("ceiling", 105), ("floor", 99), ("floor", 40)]
    )
    def test_an_impossible_band_is_refused(
        self, client: TestClient, edge: str, fahrenheit: float
    ) -> None:
        response = client.put(f"{API}/temperature/{edge}", json={"fahrenheit": fahrenheit})

        assert response.status_code == 422
        assert "must be" in response.json()["detail"]
        assert client.get(f"{API}/status").json()["ceiling_f"] == 99.0

    def test_a_body_that_is_not_a_number_is_refused(self, client: TestClient) -> None:
        response = client.put(f"{API}/temperature/ceiling", json={"fahrenheit": "hot"})

        assert response.status_code == 422


class TestPumps:
    @pytest.mark.parametrize("speed", ["low", "high", "off"])
    def test_pump_1_takes_each_speed(self, client: TestClient, speed: str) -> None:
        response = client.put(f"{API}/pumps/1", json={"speed": speed})

        assert response.status_code == 200
        assert response.json()["pump1"] == speed

    def test_pump_1_reports_the_actual_speed_when_overruled(
        self, config: AppConfig, clock: FakeClock
    ) -> None:
        heating = FakeSpaAdapter(clock, water=Temperature(90))  # cold: the heater runs

        with TestClient(build_app(config, spa=heating, clock=clock)) as client:
            response = client.put(f"{API}/pumps/1", json={"speed": "off"})

        assert response.status_code == 200
        assert response.json()["pump1"] == "low"

    def test_pump_2_switches_on_and_off(self, client: TestClient) -> None:
        assert client.put(f"{API}/pumps/2", json={"state": "on"}).json()["pump2"] == "on"
        assert client.put(f"{API}/pumps/2", json={"state": "off"}).json()["pump2"] == "off"

    def test_an_unknown_speed_is_refused(self, client: TestClient) -> None:
        assert client.put(f"{API}/pumps/1", json={"speed": "turbo"}).status_code == 422
        assert client.put(f"{API}/pumps/2", json={"state": "low"}).status_code == 422

    def test_a_pump_command_fails_when_the_spa_is_unreachable(
        self, client: TestClient, spa: StubbornSpa
    ) -> None:
        spa.drop_connection()

        response = client.put(f"{API}/pumps/2", json={"state": "on"})

        assert response.status_code == 503
        assert response.json() == {"detail": "The simulated spa is disconnected"}


class TestHeatMode:
    def test_switches_to_rest_and_back(self, client: TestClient) -> None:
        assert client.put(f"{API}/heat-mode", json={"mode": "rest"}).json()["heat_mode"] == "rest"
        assert client.put(f"{API}/heat-mode", json={"mode": "ready"}).json()["heat_mode"] == "ready"

    def test_ready_in_rest_is_not_offered(self, client: TestClient) -> None:
        assert client.put(f"{API}/heat-mode", json={"mode": "ready_in_rest"}).status_code == 422

    def test_a_refusal_by_the_spa_is_a_conflict(self, client: TestClient, spa: StubbornSpa) -> None:
        spa.refuses_heat_mode = True

        response = client.put(f"{API}/heat-mode", json={"mode": "rest"})

        assert response.status_code == 409
        assert response.json() == {"detail": "The spa did not switch to rest"}


class TestCommandLog:
    def test_lists_commands_newest_first(self, client: TestClient, clock: FakeClock) -> None:
        client.put(f"{API}/pumps/1", json={"speed": "high"})
        clock.advance(minutes=1)
        client.put(f"{API}/heat-mode", json={"mode": "rest"})

        response = client.get(f"{API}/commands")

        assert response.status_code == 200
        assert response.json() == [
            {
                "issued_at": "2026-01-05T12:01:00Z",
                "actor": "user",
                "command": "set_heat_mode",
                "argument": "rest",
                "succeeded": True,
                "result": "heat mode is rest",
            },
            {
                "issued_at": "2026-01-05T12:00:00Z",
                "actor": "user",
                "command": "set_pump1",
                "argument": "high",
                "succeeded": True,
                "result": "pump 1 is high",
            },
        ]

    def test_includes_failures(self, client: TestClient, spa: StubbornSpa) -> None:
        spa.drop_connection()
        client.put(f"{API}/pumps/2", json={"state": "on"})

        [entry] = client.get(f"{API}/commands").json()

        assert entry["succeeded"] is False
        assert entry["result"] == "The simulated spa is disconnected"

    def test_honours_the_limit(self, client: TestClient) -> None:
        for speed in ("low", "high", "off"):
            client.put(f"{API}/pumps/1", json={"speed": speed})

        assert len(client.get(f"{API}/commands", params={"limit": 2}).json()) == 2
        assert client.get(f"{API}/commands", params={"limit": 0}).status_code == 422


class TestRuntime:
    def test_is_zero_for_every_window_before_anything_has_run(self, client: TestClient) -> None:
        body = client.get(f"{API}/runtime").json()

        assert body["as_of"] == "2026-01-05T12:00:00Z"
        assert body["recording_since"] is None
        assert [window["days"] for window in body["windows"]] == [1, 7, 14, 30]
        assert body["windows"][0] == {
            "days": 1,
            "heater_hours": 0.0,
            "pump1_hours": 0.0,
            "pump1_low_hours": 0.0,
            "pump1_high_hours": 0.0,
            "pump2_hours": 0.0,
        }

    def test_counts_what_the_tub_did_while_the_service_watched(
        self, client: TestClient, spa: StubbornSpa, clock: FakeClock
    ) -> None:
        client.put(f"{API}/pumps/1", json={"speed": "high"})
        clock.advance(minutes=1)
        spa.tick()
        client.put(f"{API}/pumps/1", json={"speed": "off"})

        [day] = client.get(f"{API}/runtime", params={"days": "1"}).json()["windows"]

        assert day["pump1_high_hours"] == 0.02  # one minute
        assert day["pump1_hours"] == 0.02
        assert day["pump2_hours"] == 0.0

    def test_reports_stored_history_for_the_windows_asked(
        self, client: TestClient, config: AppConfig
    ) -> None:
        engine = open_database(config.database_path)
        intervals = SqlRunIntervalRepository(engine)
        for began, hours in [(NOON - timedelta(hours=25), 3), (NOON - timedelta(days=10), 6)]:
            intervals.start(Device.HEATER, None, began)
            intervals.finish(Device.HEATER, began + timedelta(hours=hours))
        SqlTemperatureSampleRepository(engine).add(
            TemperatureSample(NOON - timedelta(days=10), Temperature(90.0), Temperature(99.0))
        )
        engine.dispose()

        body = client.get(f"{API}/runtime", params={"days": "1,7,14"}).json()

        assert body["recording_since"] == "2025-12-26T12:00:00Z"
        assert [window["heater_hours"] for window in body["windows"]] == [2.0, 3.0, 9.0]

    @pytest.mark.parametrize("days", ["0", "91", "1,,7", "week", "", "1,2,3,4,5,6,7,8,9,10,11"])
    def test_refuses_windows_it_cannot_report(self, client: TestClient, days: str) -> None:
        response = client.get(f"{API}/runtime", params={"days": days})

        assert response.status_code == 422


class TestHeatingRate:
    def test_is_the_configured_assumption_at_first(self, client: TestClient) -> None:
        assert client.get(f"{API}/heating-rate").json() == {
            "f_per_hour": 4.0,
            "measured": False,
            "based_on_hours": 0.0,
        }

    def test_is_measured_from_stored_history(self, client: TestClient, config: AppConfig) -> None:
        began = NOON - timedelta(days=2)
        engine = open_database(config.database_path)
        intervals = SqlRunIntervalRepository(engine)
        intervals.start(Device.HEATER, None, began)
        intervals.finish(Device.HEATER, began + timedelta(hours=3))
        samples = SqlTemperatureSampleRepository(engine)
        for minute in range(181):  # 85 to 100 °F in three hours
            samples.add(
                TemperatureSample(
                    began + timedelta(minutes=minute),
                    Temperature(85.0 + minute // 12),
                    Temperature(100.0),
                )
            )
        engine.dispose()

        assert client.get(f"{API}/heating-rate").json() == {
            "f_per_hour": 5.0,
            "measured": True,
            "based_on_hours": 3.0,
        }


class TestHousekeeping:
    def test_history_older_than_ninety_days_is_deleted_at_start_up(
        self, config: AppConfig, spa: StubbornSpa, clock: FakeClock
    ) -> None:
        engine = open_database(config.database_path)
        migrate(engine)
        samples = SqlTemperatureSampleRepository(engine)
        for age_days in (91, 89):
            samples.add(
                TemperatureSample(
                    NOON - timedelta(days=age_days), Temperature(90.0), Temperature(99.0)
                )
            )

        with TestClient(build_app(config, spa=spa, clock=clock)):
            pass

        assert samples.earliest() == NOON - timedelta(days=89)
        engine.dispose()

    def test_an_interval_open_at_shutdown_is_closed(
        self, config: AppConfig, spa: StubbornSpa, clock: FakeClock
    ) -> None:
        with TestClient(build_app(config, spa=spa, clock=clock)) as client:
            client.put(f"{API}/pumps/2", json={"state": "on"})
            clock.advance(minutes=1)
            spa.tick()

        engine = open_database(config.database_path)
        intervals = SqlRunIntervalRepository(engine)
        intervals.extend_open(NOON + timedelta(hours=1))  # moves nothing if all are closed
        assert intervals.overlapping(NOON, NOON + timedelta(hours=2)) == [
            RunInterval(Device.PUMP2, None, NOON, NOON + timedelta(minutes=1))
        ]
        engine.dispose()


class TestPlans:
    """It is Monday noon UTC; the simulated tub is at 99 °F, set to 99 °F."""

    def test_nothing_is_planned_at_first(self, client: TestClient) -> None:
        response = client.get(f"{API}/plan/today")

        assert response.status_code == 404
        assert response.json() == {"detail": "Nothing is planned for today"}

    def test_planning_a_use_returns_its_timetable(self, client: TestClient) -> None:
        response = client.put(f"{API}/plan/today", json={"use_time": "18:00"})

        assert response.status_code == 200
        assert response.json() == {
            "day": "2026-01-05",
            "use_time": "18:00:00",
            "use_at": "2026-01-05T18:00:00Z",
            "ready_at": "2026-01-05T17:00:00Z",
            # The water is at the ceiling already, so only the margin remains.
            "heater_start": "2026-01-05T16:45:00Z",
            "heating_started": False,
            "hold_until": "2026-01-05T21:00:00Z",
            "expected_ready_at": "2026-01-05T16:45:00Z",
            "late": False,
        }
        assert client.get(f"{API}/plan/today").json() == response.json()

    def test_a_plan_takes_effect_at_once(self, client: TestClient) -> None:
        # Nothing is wanted until 16:45, so the idle tub goes to the floor.
        client.put(f"{API}/plan/today", json={"use_time": "18:00"})
        assert client.get(f"{API}/status").json()["target_temperature_f"] == 85.0

        # A use in half an hour is wanted now.
        body = client.put(f"{API}/plan/today", json={"use_time": "12:30"}).json()

        assert body["heating_started"] is True
        assert client.get(f"{API}/status").json()["target_temperature_f"] == 99.0

    def test_tomorrow_is_planned_separately(self, client: TestClient) -> None:
        client.put(f"{API}/plan/today", json={"use_time": "18:00"})

        response = client.put(f"{API}/plan/tomorrow", json={"use_time": "06:30"})

        assert response.json()["day"] == "2026-01-06"
        assert client.get(f"{API}/plan/today").json()["use_time"] == "18:00:00"

    def test_a_plan_survives_a_restart(
        self, config: AppConfig, clock: FakeClock, client: TestClient
    ) -> None:
        client.put(f"{API}/plan/today", json={"use_time": "18:00"})

        with TestClient(build_app(config, spa=FakeSpaAdapter(clock), clock=clock)) as restarted:
            assert restarted.get(f"{API}/plan/today").json()["use_time"] == "18:00:00"

    def test_cancelling_removes_the_plan(self, client: TestClient) -> None:
        client.put(f"{API}/plan/today", json={"use_time": "12:30"})

        response = client.delete(f"{API}/plan/today")

        assert response.status_code == 204
        assert client.get(f"{API}/plan/today").status_code == 404
        assert client.get(f"{API}/status").json()["target_temperature_f"] == 85.0
        assert client.delete(f"{API}/plan/today").status_code == 204  # nothing to cancel

    def test_a_use_that_is_already_over_is_refused(self, client: TestClient) -> None:
        response = client.put(f"{API}/plan/today", json={"use_time": "09:00"})

        assert response.status_code == 422
        assert "already be over" in response.json()["detail"]
        assert client.get(f"{API}/plan/today").status_code == 404

    @pytest.mark.parametrize("body", [{"use_time": "25:00"}, {"use_time": "soon"}, {}])
    def test_a_time_that_is_not_a_time_is_refused(
        self, client: TestClient, body: dict[str, str]
    ) -> None:
        assert client.put(f"{API}/plan/today", json=body).status_code == 422

    def test_only_today_and_tomorrow_can_be_planned(self, client: TestClient) -> None:
        response = client.put(f"{API}/plan/friday", json={"use_time": "18:00"})

        assert response.status_code == 422

    def test_a_plan_is_stored_even_when_the_spa_is_gone(
        self, client: TestClient, spa: StubbornSpa
    ) -> None:
        spa.drop_connection()

        response = client.put(f"{API}/plan/today", json={"use_time": "18:00"})

        assert response.status_code == 200
        assert client.get(f"{API}/plan/today").status_code == 200


class TestHoldTime:
    def test_is_three_hours_at_first(self, client: TestClient) -> None:
        assert client.get(f"{API}/plan/hold").json() == {"minutes": 180}

    def test_a_new_hold_time_applies_to_the_plan_already_made(
        self, config: AppConfig, clock: FakeClock, client: TestClient
    ) -> None:
        client.put(f"{API}/plan/today", json={"use_time": "18:00"})

        response = client.put(f"{API}/plan/hold", json={"minutes": 240})

        assert response.status_code == 200
        assert response.json() == {"minutes": 240}
        assert client.get(f"{API}/plan/today").json()["hold_until"] == "2026-01-05T22:00:00Z"
        with TestClient(build_app(config, spa=FakeSpaAdapter(clock), clock=clock)) as restarted:
            assert restarted.get(f"{API}/plan/hold").json() == {"minutes": 240}

    @pytest.mark.parametrize("minutes", [0, 29, 721, "long"])
    def test_a_hold_time_out_of_range_is_refused(
        self, client: TestClient, minutes: int | str
    ) -> None:
        response = client.put(f"{API}/plan/hold", json={"minutes": minutes})

        assert response.status_code == 422
        assert client.get(f"{API}/plan/hold").json() == {"minutes": 180}


class TestWeekend:
    def test_is_off_at_first_and_shows_its_times(self, client: TestClient) -> None:
        assert client.get(f"{API}/plan/weekend").json() == {
            "enabled": False,
            "start_time": "15:00:00",
            "blackout_start": "01:00:00",
            "blackout_end": "12:00:00",
        }

    def test_can_be_switched_on_and_off(self, client: TestClient) -> None:
        on = client.put(f"{API}/plan/weekend", json={"enabled": True})
        assert on.status_code == 200
        assert on.json()["enabled"] is True
        assert client.get(f"{API}/plan/weekend").json()["enabled"] is True

        off = client.put(f"{API}/plan/weekend", json={"enabled": False})
        assert off.json()["enabled"] is False

    def test_switching_it_on_during_the_weekend_heats_at_once(
        self, client: TestClient, clock: FakeClock
    ) -> None:
        client.put(f"{API}/plan/weekend", json={"enabled": False})  # a pass: idle, 85 °F
        assert client.get(f"{API}/status").json()["target_temperature_f"] == 85.0
        clock.advance(hours=5 * 24 + 2)  # Saturday 14:00

        client.put(f"{API}/plan/weekend", json={"enabled": True})

        assert client.get(f"{API}/status").json()["target_temperature_f"] == 99.0

    def test_a_body_that_is_not_a_switch_is_refused(self, client: TestClient) -> None:
        assert client.put(f"{API}/plan/weekend", json={"enabled": "maybe"}).status_code == 422


class TestNextAction:
    def test_when_nothing_is_planned(self, client: TestClient) -> None:
        assert client.get(f"{API}/plan/next-action").json() == {
            "as_of": "2026-01-05T12:00:00Z",
            "current": {
                "target_temperature_f": 85.0,
                "reason": "idle",
                "description": "Idle: the tub may cool to 85 °F.",
            },
            "next": None,
            "overrides_blackout": False,
            "warnings": [],
        }

    def test_announces_the_heater_start_for_a_planned_use(self, client: TestClient) -> None:
        client.put(f"{API}/plan/today", json={"use_time": "20:00"})

        body = client.get(f"{API}/plan/next-action").json()

        # The tub was lowered to 85 °F and still reads 99 °F, so the margin only.
        assert body["next"] == {
            "at": "2026-01-05T18:45:00Z",
            "target_temperature_f": 99.0,
            "reason": "planned_use",
            "description": (
                "Heater will start at about 6:45 PM to be ready by 7:00 PM for use at 8:00 PM."
            ),
        }

    def test_announces_the_weekend(self, client: TestClient) -> None:
        client.put(f"{API}/plan/weekend", json={"enabled": True})

        body = client.get(f"{API}/plan/next-action").json()

        assert body["next"]["at"] == "2026-01-09T15:00:00Z"
        assert body["next"]["reason"] == "weekend_ready"

    def test_says_when_a_planned_use_overrides_the_no_heating_hours(
        self, client: TestClient, clock: FakeClock
    ) -> None:
        clock.advance(hours=4 * 24 + 16)  # Saturday 04:00
        client.put(f"{API}/plan/weekend", json={"enabled": True})
        client.put(f"{API}/plan/today", json={"use_time": "05:00"})

        body = client.get(f"{API}/plan/next-action").json()

        assert body["current"]["reason"] == "planned_use"
        assert body["overrides_blackout"] is True
        assert body["next"]["reason"] == "weekend_blackout"

    def test_warns_when_the_spa_cannot_be_reached(
        self, client: TestClient, spa: StubbornSpa
    ) -> None:
        spa.drop_connection()

        body = client.get(f"{API}/plan/next-action").json()

        assert body["warnings"] == [
            "The spa cannot be reached, so nothing can be carried out right now."
        ]


class TestSchedulerSwitchedOff:
    def test_plans_are_kept_but_the_tub_is_left_alone(
        self, config: AppConfig, spa: StubbornSpa, clock: FakeClock
    ) -> None:
        off = replace(config, scheduler_enabled=False)
        with TestClient(build_app(off, spa=spa, clock=clock)) as client:
            response = client.put(f"{API}/plan/today", json={"use_time": "12:30"})
            status = client.get(f"{API}/status").json()
            warnings = client.get(f"{API}/plan/next-action").json()["warnings"]

        assert response.status_code == 200
        assert status["target_temperature_f"] == 99.0
        assert len(warnings) == 1  # the water is warm already, so the use is not late
        assert "switched off" in warnings[0]


FLOW = SpaFault(16, "The water flow is low", NOON)


def store_alert(config: AppConfig, alert: Alert) -> Alert:
    """Put an alert in the application's database, as the watchdog would."""
    engine = open_database(config.database_path)
    try:
        return SqlAlertRepository(engine).add(alert)
    finally:
        engine.dispose()


class TestAlerts:
    def test_there_are_none_at_first(self, client: TestClient) -> None:
        assert client.get(f"{API}/alerts").json() == {
            "active": [],
            "history": [],
            "notifications_configured": False,
        }

    def test_lists_an_active_alert(self, config: AppConfig, client: TestClient) -> None:
        store_alert(config, fault_alert(FLOW, NOON))

        body = client.get(f"{API}/alerts").json()

        assert body["active"] == [
            {
                "id": 1,
                "kind": "spa_fault",
                "title": "Spa fault",
                "severity": "critical",
                "message": "The spa logged: The water flow is low (code 16).",
                "raised_at": "2026-01-05T12:00:00Z",
                "cleared_at": None,
                "needs_acknowledging": True,
            }
        ]
        assert body["history"] == []

    def test_history_goes_back_as_far_as_asked(self, config: AppConfig, client: TestClient) -> None:
        cold = Alert(AlertKind.WATER_TOO_COLD, Severity.CRITICAL, "Cold.", NOON, NOON)
        store_alert(config, replace(cold, raised_at=NOON - timedelta(days=5)))
        store_alert(config, replace(cold, raised_at=NOON - timedelta(hours=5)))
        store_alert(config, fault_alert(FLOW, NOON))

        one_day = client.get(f"{API}/alerts", params={"days": 1}).json()
        one_week = client.get(f"{API}/alerts", params={"days": 7}).json()

        assert [alert["id"] for alert in one_day["history"]] == [3, 2]
        assert [alert["id"] for alert in one_week["history"]] == [3, 2, 1]
        assert [alert["id"] for alert in one_week["active"]] == [3]

    @pytest.mark.parametrize("days", [-1, 91, "all"])
    def test_a_history_that_makes_no_sense_is_refused(
        self, client: TestClient, days: int | str
    ) -> None:
        assert client.get(f"{API}/alerts", params={"days": days}).status_code == 422

    def test_a_spa_fault_is_cleared_by_acknowledging_it(
        self, config: AppConfig, client: TestClient
    ) -> None:
        store_alert(config, fault_alert(FLOW, NOON))

        response = client.post(f"{API}/alerts/1/acknowledge")

        assert response.status_code == 204
        body = client.get(f"{API}/alerts", params={"days": 1}).json()
        assert body["active"] == []
        assert body["history"][0]["cleared_at"] == "2026-01-05T12:00:00Z"

    def test_acknowledging_twice_is_a_conflict(self, config: AppConfig, client: TestClient) -> None:
        store_alert(config, fault_alert(FLOW, NOON))
        client.post(f"{API}/alerts/1/acknowledge")

        response = client.post(f"{API}/alerts/1/acknowledge")

        assert response.status_code == 409
        assert response.json() == {"detail": "Alert 1 is already over"}

    def test_an_alert_that_clears_by_itself_cannot_be_acknowledged(
        self, config: AppConfig, client: TestClient
    ) -> None:
        store_alert(config, Alert(AlertKind.NOT_HEATING, Severity.CRITICAL, "Not heating.", NOON))

        response = client.post(f"{API}/alerts/1/acknowledge")

        assert response.status_code == 409
        assert "clears by itself" in response.json()["detail"]
        assert len(client.get(f"{API}/alerts").json()["active"]) == 1

    def test_an_unknown_alert_is_not_found(self, client: TestClient) -> None:
        response = client.post(f"{API}/alerts/7/acknowledge")

        assert response.status_code == 404
        assert response.json() == {"detail": "There is no alert number 7"}

    def test_a_test_notification_without_keys_says_so(self, client: TestClient) -> None:
        response = client.post(f"{API}/alerts/test-notification")

        assert response.status_code == 409
        assert "No Pushover keys" in response.json()["detail"]

    def test_says_when_notifications_are_configured(
        self, config: AppConfig, spa: StubbornSpa, clock: FakeClock
    ) -> None:
        keyed = replace(config, pushover_token="app-token-secret", pushover_user="user-secret")

        with TestClient(build_app(keyed, spa=spa, clock=clock)) as client:
            configured = client.get(f"{API}/alerts").json()["notifications_configured"]

        assert configured is True
        assert "secret" not in repr(keyed)


class TestServing:
    def test_the_api_reference_is_served_under_the_prefix(self, client: TestClient) -> None:
        assert client.get("/hot-tub/api/docs").status_code == 200
        paths = client.get("/hot-tub/api/openapi.json").json()["paths"]
        assert f"{API}/pumps/1" in paths

    def test_nothing_is_served_outside_the_prefix(self, client: TestClient) -> None:
        assert client.get("/api/v1/status").status_code == 404

"""The watchdog watching a simulated tub, minute by minute.

The band is 85 to 99 °F. Each test starts at noon with the tub at 99 °F,
set to 99 °F, in Ready, unless it says otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hottub.control import ControlService
from hottub.domain import (
    Actor,
    Alert,
    AlertKind,
    AlertNotAcknowledgeableError,
    AlertNotFoundError,
    HeatMode,
    NotificationError,
    PumpSpeed,
    Severity,
    SpaFault,
    SpaUnavailableError,
    Temperature,
    TemperatureBand,
)
from hottub.spa import FakeSpaAdapter
from hottub.watchdog import NotificationOutbox, NullNotifier, WatchdogService
from tests.fakes.clock import FakeClock
from tests.fakes.notifier import RecordingNotifier
from tests.fakes.repositories import (
    InMemoryAlertRepository,
    InMemoryCommandLog,
    InMemorySettingsRepository,
)

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
BAND = TemperatureBand(floor=Temperature(85), ceiling=Temperature(99))
FLOW = SpaFault(16, "The water flow is low", NOON + timedelta(minutes=20))
PRIMING = SpaFault(19, "Priming Mode", NOON - timedelta(days=8))


class Rig:
    """A watchdog wired to a simulated tub, with everything exposed."""

    def __init__(
        self,
        *,
        water: float = 99,
        set_temperature: float = 99,
        heat_mode: HeatMode = HeatMode.READY,
    ) -> None:
        self.clock = FakeClock(NOON)
        self.spa = FakeSpaAdapter(
            self.clock,
            water=Temperature(water),
            set_temperature=Temperature(set_temperature),
            heat_mode=heat_mode,
        )
        self.control = ControlService(
            self.spa, InMemorySettingsRepository(), InMemoryCommandLog(), self.clock, BAND
        )
        self.alerts = InMemoryAlertRepository()
        self.notifier = RecordingNotifier()
        self.outbox = NotificationOutbox(self.notifier)
        self.watchdog = self.new_watchdog()

    def new_watchdog(self) -> WatchdogService:
        """Return another watchdog over the same alerts, as after a restart."""
        return WatchdogService(self.control, self.spa, self.alerts, self.outbox, self.clock)

    async def run(self, minutes: int) -> None:
        """Let ``minutes`` pass, with a pass of the watchdog every minute."""
        for _ in range(minutes):
            self.clock.advance(minutes=1)
            if self.spa.connected:
                self.spa.tick()
            await self.watchdog.check()
        await self.outbox.drain()

    @property
    def active(self) -> list[tuple[AlertKind, Severity]]:
        return [(alert.kind, alert.severity) for alert in self.watchdog.active_alerts()]

    def only_alert(self) -> Alert:
        (alert,) = self.alerts.all
        return alert


@pytest.fixture
async def rig() -> Rig:
    rig = Rig()
    await rig.spa.connect()
    return rig


class TestHealthyTub:
    async def test_raises_nothing(self, rig: Rig) -> None:
        await rig.run(180)

        assert rig.alerts.all == []
        assert rig.notifier.sent == []

    async def test_a_tub_heating_up_from_cold_raises_nothing(self) -> None:
        # 60 °F after a refill; 39 °F to gain takes nearly eight hours.
        rig = Rig(water=60)
        await rig.spa.connect()

        await rig.run(9 * 60)

        assert rig.alerts.all == []

    async def test_a_tub_resting_cold_raises_nothing(self) -> None:
        # In Rest the reading is not trusted, so it is unknown, not cold.
        rig = Rig(water=70, heat_mode=HeatMode.REST)
        await rig.spa.connect()

        await rig.run(180)

        assert rig.alerts.all == []


class TestStaleData:
    async def test_silence_is_a_warning_on_the_page_only(self, rig: Rig) -> None:
        rig.spa.drop_connection()

        await rig.run(1)
        assert rig.active == []

        await rig.run(1)
        assert rig.active == [(AlertKind.STALE_DATA, Severity.WARNING)]
        assert rig.notifier.sent == []

    async def test_long_silence_becomes_critical_and_is_pushed_once(self, rig: Rig) -> None:
        rig.spa.drop_connection()

        await rig.run(60)

        alert = rig.only_alert()  # the same alert, made more severe
        assert alert.severity is Severity.CRITICAL
        assert alert.raised_at == NOON + timedelta(minutes=2)
        assert "15 minutes" in alert.message
        assert rig.notifier.titles == ["Hot tub: No word from the spa"]

    async def test_cleared_when_the_spa_is_heard_again(self, rig: Rig) -> None:
        rig.spa.drop_connection()
        await rig.run(20)

        rig.spa.restore_connection()
        await rig.run(1)

        assert rig.active == []
        assert rig.only_alert().cleared_at == NOON + timedelta(minutes=21)
        assert rig.notifier.titles == [
            "Hot tub: No word from the spa",
            "Hot tub: No word from the spa: cleared",
        ]

    async def test_a_warning_that_clears_is_never_pushed(self, rig: Rig) -> None:
        rig.spa.drop_connection()
        await rig.run(5)
        rig.spa.restore_connection()
        await rig.run(1)

        assert rig.active == []
        assert rig.notifier.sent == []

    async def test_a_tub_that_is_off_when_the_service_starts_is_noticed(self) -> None:
        rig = Rig()  # never connected

        await rig.run(2)
        assert rig.active == [(AlertKind.STALE_DATA, Severity.WARNING)]

        await rig.run(13)
        assert rig.active == [(AlertKind.STALE_DATA, Severity.CRITICAL)]

    async def test_other_alerts_are_neither_raised_nor_cleared_meanwhile(self) -> None:
        # Too cold is raised, then the spa goes quiet. Unknown is not the
        # same as fine, so the alert stands; and it is not raised twice.
        rig = Rig(water=80, set_temperature=80)
        await rig.spa.connect()
        await rig.run(31)
        assert rig.active == [(AlertKind.WATER_TOO_COLD, Severity.CRITICAL)]

        rig.spa.drop_connection()
        await rig.run(60)

        assert rig.active == [
            (AlertKind.WATER_TOO_COLD, Severity.CRITICAL),
            (AlertKind.STALE_DATA, Severity.CRITICAL),
        ]

    async def test_counts_start_again_when_data_returns(self) -> None:
        # Cold for 29 minutes, silence, then cold again: the half hour
        # must be served in full once more.
        rig = Rig(water=80, set_temperature=80)
        await rig.spa.connect()
        await rig.run(29)
        rig.spa.drop_connection()
        await rig.run(5)
        rig.spa.restore_connection()

        await rig.run(29)
        assert AlertKind.WATER_TOO_COLD not in [kind for kind, _ in rig.active]

        await rig.run(2)
        assert (AlertKind.WATER_TOO_COLD, Severity.CRITICAL) in rig.active


class TestWaterTooCold:
    async def test_raised_once_and_pushed_once(self) -> None:
        # Set to 80 °F at the tub's own panel, with nothing correcting it.
        rig = Rig(water=80, set_temperature=80)
        await rig.spa.connect()

        await rig.run(30)  # first seen cold at 12:01, so 29 minutes so far
        assert rig.active == []

        await rig.run(120)
        alert = rig.only_alert()
        assert (alert.kind, alert.severity) == (AlertKind.WATER_TOO_COLD, Severity.CRITICAL)
        assert alert.raised_at == NOON + timedelta(minutes=31)
        assert rig.notifier.titles == ["Hot tub: Water too cold"]
        assert rig.notifier.sent[0].message == alert.message

    async def test_cleared_and_pushed_when_the_water_is_back_at_the_floor(self) -> None:
        rig = Rig(water=80, set_temperature=80)
        await rig.spa.connect()
        await rig.run(31)

        await rig.spa.set_temperature(Temperature(85))  # someone puts it right
        await rig.run(30)
        assert rig.active == [(AlertKind.WATER_TOO_COLD, Severity.CRITICAL)]  # warming, not there

        await rig.run(40)
        assert rig.active == []
        assert rig.notifier.titles == [
            "Hot tub: Water too cold",
            "Hot tub: Water too cold: cleared",
        ]

    async def test_survives_a_restart_without_being_raised_again(self) -> None:
        rig = Rig(water=80, set_temperature=80)
        await rig.spa.connect()
        await rig.run(31)

        rig.watchdog = rig.new_watchdog()
        await rig.run(60)

        assert len(rig.alerts.all) == 1
        assert len(rig.notifier.sent) == 1


class TestWaterTooHot:
    async def test_the_safety_limit_is_pushed_at_once(self) -> None:
        rig = Rig(water=104, set_temperature=104)
        await rig.spa.connect()

        await rig.run(1)

        assert rig.active == [(AlertKind.WATER_TOO_HOT, Severity.CRITICAL)]
        assert rig.notifier.titles == ["Hot tub: Water too hot"]

    async def test_a_tub_cooling_after_its_ceiling_was_lowered_raises_nothing(self) -> None:
        rig = Rig(water=103, set_temperature=103)
        await rig.spa.connect()
        await rig.control.set_ceiling(Temperature(103), Actor.USER)
        await rig.run(5)

        await rig.control.set_ceiling(Temperature(99), Actor.USER)
        await rig.run(10 * 60)  # half a degree an hour: back to 99 °F in eight

        assert rig.alerts.all == []


class TestNotHeating:
    async def test_a_heater_that_runs_without_warming_the_water(self) -> None:
        rig = Rig(water=90)
        await rig.spa.connect()
        rig.spa.fail_heater()

        await rig.run(44)
        assert rig.active == []

        await rig.run(2)
        assert rig.active == [(AlertKind.NOT_HEATING, Severity.CRITICAL)]
        assert rig.notifier.titles == ["Hot tub: Not heating"]

    async def test_cleared_when_the_heater_works_again(self) -> None:
        rig = Rig(water=90)
        await rig.spa.connect()
        rig.spa.fail_heater()
        await rig.run(50)

        rig.spa.repair_heater()
        await rig.run(30)

        assert rig.active == []
        assert rig.notifier.titles == ["Hot tub: Not heating", "Hot tub: Not heating: cleared"]

    async def test_a_failed_heater_in_a_cold_tub_is_reported_as_not_heating(self) -> None:
        # Too cold waits while the heater claims to run, so the owner
        # gets the one alert that names the cause.
        rig = Rig(water=70)
        await rig.spa.connect()
        rig.spa.fail_heater()

        await rig.run(120)

        assert rig.active == [(AlertKind.NOT_HEATING, Severity.CRITICAL)]


class TestSpaFault:
    async def test_the_entry_found_at_first_is_only_noted(self, rig: Rig) -> None:
        rig.spa.log_fault(PRIMING)

        await rig.run(30)

        assert rig.alerts.all == []
        assert rig.alerts.last_fault_seen() == PRIMING

    async def test_a_new_entry_is_raised_and_pushed(self, rig: Rig) -> None:
        rig.spa.log_fault(PRIMING)
        await rig.run(10)

        rig.spa.log_fault(FLOW)
        await rig.run(5)

        alert = rig.only_alert()
        assert (alert.kind, alert.severity) == (AlertKind.SPA_FAULT, Severity.CRITICAL)
        assert alert.message == "The spa logged: The water flow is low (code 16)."
        assert rig.notifier.titles == ["Hot tub: Spa fault"]

    async def test_the_log_is_read_every_five_minutes_not_every_minute(self, rig: Rig) -> None:
        asked: list[datetime] = []
        latest_fault = rig.spa.latest_fault

        async def counting() -> SpaFault | None:
            asked.append(rig.clock.now())
            return await latest_fault()

        rig.spa.latest_fault = counting  # type: ignore[method-assign]

        await rig.run(11)

        assert asked == [NOON + timedelta(minutes=minute) for minute in (1, 6, 11)]

    async def test_stays_until_acknowledged(self, rig: Rig) -> None:
        rig.spa.log_fault(PRIMING)
        await rig.run(1)
        rig.spa.log_fault(FLOW)
        await rig.run(120)
        alert = rig.only_alert()
        assert alert.active and alert.id is not None

        rig.watchdog.acknowledge(alert.id)
        await rig.run(30)

        assert rig.active == []
        assert rig.only_alert().cleared_at == NOON + timedelta(minutes=121)
        assert rig.notifier.titles == ["Hot tub: Spa fault"]  # no push for the owner's own act

    async def test_is_not_raised_again_after_a_restart(self, rig: Rig) -> None:
        rig.spa.log_fault(PRIMING)
        await rig.run(1)
        rig.spa.log_fault(FLOW)
        await rig.run(5)

        rig.watchdog = rig.new_watchdog()
        await rig.run(30)

        assert len(rig.alerts.all) == 1

    async def test_a_fault_logged_while_the_service_was_down_is_raised(self, rig: Rig) -> None:
        rig.spa.log_fault(PRIMING)
        await rig.run(1)

        rig.spa.log_fault(FLOW)  # nobody is watching
        rig.clock.advance(hours=6)
        rig.spa.tick()
        rig.watchdog = rig.new_watchdog()
        await rig.run(1)

        assert rig.active == [(AlertKind.SPA_FAULT, Severity.CRITICAL)]

    async def test_a_routine_entry_is_a_warning_on_the_page_only(self, rig: Rig) -> None:
        rig.spa.log_fault(FLOW)
        await rig.run(1)

        rig.spa.log_fault(SpaFault(19, "Priming Mode", NOON + timedelta(minutes=3)))
        await rig.run(5)

        assert rig.active == [(AlertKind.SPA_FAULT, Severity.WARNING)]
        assert rig.notifier.sent == []

    async def test_two_faults_are_two_alerts(self, rig: Rig) -> None:
        rig.spa.log_fault(PRIMING)
        await rig.run(1)
        rig.spa.log_fault(FLOW)
        await rig.run(5)
        rig.spa.log_fault(SpaFault(16, "The water flow is low", NOON + timedelta(minutes=40)))
        await rig.run(5)

        assert rig.active == [(AlertKind.SPA_FAULT, Severity.CRITICAL)] * 2

    async def test_a_spa_that_does_not_answer_is_asked_again_later(self, rig: Rig) -> None:
        rig.spa.log_fault(PRIMING)
        await rig.run(1)
        rig.spa.log_fault(FLOW)
        latest_fault = rig.spa.latest_fault

        async def no_answer() -> SpaFault | None:
            raise SpaUnavailableError("The spa did not answer the fault-log request")

        rig.spa.latest_fault = no_answer  # type: ignore[method-assign]
        await rig.run(5)
        assert rig.alerts.all == []

        rig.spa.latest_fault = latest_fault  # type: ignore[method-assign]
        await rig.run(1)  # the very next pass, not five minutes on
        assert rig.active == [(AlertKind.SPA_FAULT, Severity.CRITICAL)]


class TestAcknowledging:
    async def test_an_unknown_number_is_an_error(self, rig: Rig) -> None:
        with pytest.raises(AlertNotFoundError, match="no alert number 7"):
            rig.watchdog.acknowledge(7)

    async def test_an_alert_that_clears_by_itself_cannot_be_acknowledged(self, rig: Rig) -> None:
        rig.spa.drop_connection()
        await rig.run(2)
        alert = rig.only_alert()
        assert alert.id is not None

        with pytest.raises(AlertNotAcknowledgeableError, match="clears by itself"):
            rig.watchdog.acknowledge(alert.id)

        assert rig.only_alert().active

    async def test_an_alert_cannot_be_acknowledged_twice(self, rig: Rig) -> None:
        rig.spa.log_fault(PRIMING)
        await rig.run(1)
        rig.spa.log_fault(FLOW)
        await rig.run(5)
        alert = rig.only_alert()
        assert alert.id is not None
        rig.watchdog.acknowledge(alert.id)

        with pytest.raises(AlertNotAcknowledgeableError, match="already over"):
            rig.watchdog.acknowledge(alert.id)


class TestHistory:
    async def test_lists_recent_alerts_newest_first_including_active_ones(self, rig: Rig) -> None:
        rig.spa.drop_connection()
        await rig.run(5)
        rig.spa.restore_connection()
        await rig.run(1)
        rig.clock.advance(hours=48)
        rig.spa.tick()
        rig.spa.drop_connection()
        await rig.run(5)

        recent = rig.watchdog.history(days=1)
        both = rig.watchdog.history(days=7)

        assert [alert.active for alert in recent] == [True]
        assert [alert.active for alert in both] == [True, False]

    async def test_old_cleared_alerts_can_be_forgotten_but_never_active_ones(
        self, rig: Rig
    ) -> None:
        rig.spa.drop_connection()
        await rig.run(5)
        rig.spa.restore_connection()
        await rig.run(1)
        rig.spa.drop_connection()
        await rig.run(5)

        rig.watchdog.forget_alerts_before(rig.clock.now() + timedelta(days=365))

        assert [alert.active for alert in rig.alerts.all] == [True]


class TestTestNotification:
    async def test_is_sent_at_once(self, rig: Rig) -> None:
        await rig.watchdog.send_test_notification()

        assert rig.notifier.titles == ["Hot tub: test"]
        assert rig.watchdog.notifications_configured

    async def test_a_failure_is_reported_to_the_caller(self, rig: Rig) -> None:
        rig.notifier = RecordingNotifier(failures=[NotificationError("Pushover answered 500")])
        rig.outbox = NotificationOutbox(rig.notifier)
        rig.watchdog = rig.new_watchdog()

        with pytest.raises(NotificationError, match="500"):
            await rig.watchdog.send_test_notification()

        assert rig.notifier.attempts == 1  # no retry: the owner is waiting for the answer

    async def test_without_keys_it_says_so(self, rig: Rig) -> None:
        rig.outbox = NotificationOutbox(NullNotifier())
        rig.watchdog = rig.new_watchdog()

        with pytest.raises(NotificationError, match="No Pushover keys") as raised:
            await rig.watchdog.send_test_notification()

        assert raised.value.permanent
        assert not rig.watchdog.notifications_configured


class TestWithoutANotifier:
    async def test_alerts_are_still_raised_and_cleared(self) -> None:
        rig = Rig()
        rig.outbox = NotificationOutbox(NullNotifier())
        rig.watchdog = rig.new_watchdog()
        await rig.spa.connect()
        rig.spa.drop_connection()

        await rig.run(20)
        assert rig.active == [(AlertKind.STALE_DATA, Severity.CRITICAL)]

        rig.spa.restore_connection()
        await rig.run(1)
        assert rig.active == []


async def test_a_pump_running_in_rest_makes_the_reading_count() -> None:
    # Rest mode, 70 °F, heater never on: with pump 1 running the reading
    # is trusted, so after half an hour the tub is too cold. The pump
    # switches itself off after 15 minutes, so it is kept running here.
    rig = Rig(water=70, heat_mode=HeatMode.REST)
    await rig.spa.connect()

    for _ in range(4):
        await rig.spa.set_pump1(PumpSpeed.LOW)
        await rig.run(10)

    assert rig.active == [(AlertKind.WATER_TOO_COLD, Severity.CRITICAL)]

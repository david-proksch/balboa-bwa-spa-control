"""The recorder, watching a simulated tub minute by minute."""

from __future__ import annotations

from datetime import datetime

from hottub.domain import Device, PumpSpeed, RunInterval, Temperature
from tests.fakes.repositories import InMemoryRunIntervalRepository
from tests.unit.telemetry.rig import MINUTE, NOON, Rig


class TestRunIntervals:
    async def test_a_pump_is_recorded_from_switching_on_to_switching_off(self) -> None:
        rig = Rig()
        await rig.start()

        rig.run(3)
        await rig.spa.set_pump2(True)
        rig.run(10)
        await rig.spa.set_pump2(False)
        rig.run(3)

        assert rig.intervals.all == [
            RunInterval(Device.PUMP2, None, NOON + 3 * MINUTE, NOON + 13 * MINUTE)
        ]

    async def test_a_change_of_speed_ends_one_interval_and_starts_another(self) -> None:
        rig = Rig()
        await rig.start()

        await rig.spa.set_pump1(PumpSpeed.LOW)
        rig.run(5)
        await rig.spa.set_pump1(PumpSpeed.HIGH)
        rig.run(2)
        await rig.spa.set_pump1(PumpSpeed.OFF)

        assert rig.intervals.all == [
            RunInterval(Device.PUMP1, PumpSpeed.LOW, NOON, NOON + 5 * MINUTE),
            RunInterval(Device.PUMP1, PumpSpeed.HIGH, NOON + 5 * MINUTE, NOON + 7 * MINUTE),
        ]

    async def test_a_pump_the_tub_switches_off_itself_is_recorded_as_stopped(self) -> None:
        rig = Rig()
        await rig.start()

        await rig.spa.set_pump2(True)
        rig.run(30)  # the simulated tub's pump timeout is 15 minutes

        assert rig.intervals.all == [RunInterval(Device.PUMP2, None, NOON, NOON + 15 * MINUTE)]
        assert rig.intervals.open_devices == set()

    async def test_heating_is_recorded_with_the_pump_the_tub_runs_for_it(self) -> None:
        rig = Rig(water=97.0)  # two degrees to gain at five an hour: 24 minutes
        await rig.start()

        rig.run(60)

        [heater] = rig.intervals_of(Device.HEATER)
        assert heater.started_at == NOON
        # The simulation works in whole minutes and may need one more.
        assert heater.ended_at in (NOON + 24 * MINUTE, NOON + 25 * MINUTE)
        assert rig.intervals_of(Device.PUMP1) == [
            RunInterval(Device.PUMP1, PumpSpeed.LOW, NOON, heater.ended_at)
        ]

    async def test_a_running_device_is_credited_up_to_the_latest_minute(self) -> None:
        rig = Rig()
        await rig.start()

        await rig.spa.set_pump2(True)
        rig.run(5)

        assert rig.intervals.all == [RunInterval(Device.PUMP2, None, NOON, NOON + 5 * MINUTE)]
        assert rig.intervals.open_devices == {Device.PUMP2}

    async def test_a_device_already_running_when_recording_starts_is_picked_up(self) -> None:
        rig = Rig()
        await rig.spa.connect()
        await rig.spa.set_pump2(True)
        rig.clock.advance(minutes=2)
        rig.spa.tick()

        rig.recorder.start()
        rig.run(3)

        assert rig.intervals.all == [
            RunInterval(Device.PUMP2, None, NOON + 3 * MINUTE, NOON + 5 * MINUTE)
        ]


class TestOutages:
    async def test_a_lost_connection_ends_the_interval_at_the_last_report(self) -> None:
        rig = Rig(water=85.0)  # heats for hours
        await rig.start()
        rig.run(10)

        rig.spa.drop_connection()
        rig.run(30)

        assert rig.intervals_of(Device.HEATER) == [
            RunInterval(Device.HEATER, None, NOON, NOON + 10 * MINUTE)
        ]
        assert rig.intervals.open_devices == set()

    async def test_a_device_still_running_after_an_outage_gets_a_new_interval(self) -> None:
        rig = Rig(water=85.0)
        await rig.start()
        rig.run(10)
        rig.spa.drop_connection()
        rig.run(30)

        rig.spa.restore_connection()
        rig.run(5)

        # 15 minutes on record, not the 45 the heater really ran: time
        # during which the tub could not be heard is never counted.
        assert rig.intervals_of(Device.HEATER) == [
            RunInterval(Device.HEATER, None, NOON, NOON + 10 * MINUTE),
            RunInterval(Device.HEATER, None, NOON + 40 * MINUTE, NOON + 45 * MINUTE),
        ]

    async def test_a_short_silence_does_not_break_the_interval(self) -> None:
        rig = Rig(water=85.0)
        await rig.start()
        rig.run(10)

        # A stall of the Wi-Fi module: 45 seconds without a report, during
        # which a tick falls due.
        rig.clock.advance(seconds=45)
        rig.recorder.tick()
        rig.clock.advance(seconds=15)
        rig.spa.tick()
        rig.run(5)

        assert rig.intervals_of(Device.HEATER) == [
            RunInterval(Device.HEATER, None, NOON, NOON + 16 * MINUTE)
        ]

    async def test_a_long_silence_on_an_open_connection_ends_the_interval(self) -> None:
        rig = Rig(water=85.0)
        await rig.start()
        rig.run(10)

        rig.run(5, spa_reports=False)

        assert rig.spa.connected
        assert rig.intervals_of(Device.HEATER) == [
            RunInterval(Device.HEATER, None, NOON, NOON + 10 * MINUTE)
        ]
        assert rig.intervals.open_devices == set()

    async def test_a_report_after_a_gap_in_recording_does_not_bridge_the_gap(self) -> None:
        # The computer slept: no ticks for ten minutes, and the first thing
        # that happens afterwards is a report from the spa.
        rig = Rig()
        await rig.start()
        await rig.spa.set_pump2(True)
        rig.run(2)

        rig.clock.advance(minutes=10)
        await rig.spa.set_pump1(PumpSpeed.HIGH)
        rig.run(1)

        woke = NOON + 12 * MINUTE
        assert rig.intervals_of(Device.PUMP2) == [
            RunInterval(Device.PUMP2, None, NOON, NOON + 2 * MINUTE),
            RunInterval(Device.PUMP2, None, woke, woke + MINUTE),
        ]


class TestStartingAndStopping:
    async def test_an_interval_left_open_by_a_crash_is_closed_where_it_stood(self) -> None:
        rig = Rig(water=85.0)
        await rig.start()
        rig.run(10)

        # The program dies without stopping the recorder, and is restarted
        # an hour later.
        rig.clock.advance(hours=1)
        restarted = rig.new_recorder()
        restarted.start()

        assert rig.intervals.open_devices == set()
        assert rig.intervals_of(Device.HEATER) == [
            RunInterval(Device.HEATER, None, NOON, NOON + 10 * MINUTE)
        ]

    async def test_stopping_closes_the_intervals_as_of_now(self) -> None:
        rig = Rig()
        await rig.start()
        await rig.spa.set_pump2(True)
        rig.run(4)
        rig.clock.advance(seconds=30)
        rig.spa.tick()

        rig.recorder.stop()

        assert rig.intervals.open_devices == set()
        assert rig.intervals.all == [RunInterval(Device.PUMP2, None, NOON, NOON + 4.5 * MINUTE)]

    async def test_nothing_is_recorded_after_stopping(self) -> None:
        rig = Rig()
        await rig.start()
        rig.recorder.stop()

        await rig.spa.set_pump2(True)

        assert rig.intervals.all == []

    async def test_a_failure_to_record_does_not_disturb_the_spa_and_is_put_right(self) -> None:
        class FailsOnce(InMemoryRunIntervalRepository):
            failed = False

            def start(self, device: Device, speed: PumpSpeed | None, at: datetime) -> None:
                if not self.failed:
                    self.failed = True
                    raise OSError("disk full")
                super().start(device, speed, at)

        rig = Rig()
        rig.intervals = FailsOnce()
        rig.recorder = rig.new_recorder()
        await rig.start()

        await rig.spa.set_pump2(True)  # must not raise
        rig.run(3)

        assert rig.intervals.all == [
            RunInterval(Device.PUMP2, None, NOON + MINUTE, NOON + 3 * MINUTE)
        ]


class TestTemperatureSamples:
    async def test_one_is_taken_each_minute(self) -> None:
        rig = Rig(water=97.0)
        await rig.start()

        rig.run(13)

        samples = rig.samples.all
        assert [sample.at for sample in samples] == [NOON + n * MINUTE for n in range(1, 14)]
        assert samples[0].water == Temperature(97.0)
        assert samples[-1].water == Temperature(98.0)  # 13 minutes at 5 °F an hour
        assert samples[-1].set_temperature == Temperature(99.0)

    async def test_none_is_taken_while_the_spa_cannot_be_heard(self) -> None:
        rig = Rig()
        await rig.start()
        rig.run(2)

        rig.spa.drop_connection()
        rig.run(10)
        rig.spa.restore_connection()
        rig.run(1)

        assert [sample.at for sample in rig.samples.all] == [
            NOON + MINUTE,
            NOON + 2 * MINUTE,
            NOON + 13 * MINUTE,
        ]

    async def test_none_is_taken_before_the_spa_is_first_heard(self) -> None:
        rig = Rig()
        rig.recorder.start()

        rig.run(3, spa_reports=False)

        assert rig.samples.all == []

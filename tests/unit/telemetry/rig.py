"""A simulated tub with a recorder watching it, for the telemetry tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hottub.domain import Device, RunInterval, Temperature
from hottub.spa import FakeSpaAdapter
from hottub.telemetry import TelemetryRecorder, TelemetryService
from tests.fakes.clock import FakeClock
from tests.fakes.repositories import (
    InMemoryRunIntervalRepository,
    InMemoryTemperatureSampleRepository,
)

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
SEED_RATE = 4.0


class Rig:
    """Wires a recorder and a service to a simulated tub and a test clock.

    By default the water is at the set temperature, so the heater is off
    and nothing runs until a test switches it on.
    """

    def __init__(self, *, water: float = 99.0) -> None:
        self.clock = FakeClock(NOON)
        self.spa = FakeSpaAdapter(self.clock, water=Temperature(water))
        self.intervals = InMemoryRunIntervalRepository()
        self.samples = InMemoryTemperatureSampleRepository()
        self.recorder = self.new_recorder()
        self.service = TelemetryService(self.intervals, self.samples, self.clock, SEED_RATE)

    def new_recorder(self) -> TelemetryRecorder:
        """Return another recorder over the same history, as after a restart."""
        return TelemetryRecorder(self.spa, self.intervals, self.samples, self.clock)

    async def start(self) -> None:
        self.recorder.start()
        await self.spa.connect()

    def run(self, minutes: int, *, spa_reports: bool = True) -> None:
        """Let ``minutes`` pass, with the once-a-minute tick the application gives.

        Args:
            minutes: How long to run for.
            spa_reports: False to simulate a spa that has stopped talking
                while its connection stays open.
        """
        for _ in range(minutes):
            self.clock.advance(minutes=1)
            if spa_reports:
                self.spa.tick()
            self.recorder.tick()

    def intervals_of(self, device: Device) -> list[RunInterval]:
        return [interval for interval in self.intervals.all if interval.device is device]

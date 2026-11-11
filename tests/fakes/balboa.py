"""A stand-in for ``pybalboa.SpaClient``, for testing the Balboa adapter.

It mimics the library behaviours the adapter has to cope with, as read from
the library's source and recorded in ``docs/balboa-notes.md``:

* ``connect()`` returns False instead of raising;
* commands are toggles, and are dropped silently while disconnected;
* ``set_temperature`` raises ``ValueError`` outside the active range;
* each temperature range remembers its own set temperature;
* fault-log replies arrive without an update event.

Tests stage awkward situations through the plain attributes: ``reachable``,
``sends_configuration``, ``obeys`` on each control, and so on.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import IntEnum

from pybalboa.control import FaultLog
from pybalboa.enums import (
    HeatMode,
    HeatState,
    LowHighRange,
    OffLowHighState,
    OffOnState,
    TemperatureUnit,
)

from hottub.domain import Clock

RANGE_LIMITS = {LowHighRange.LOW: (50.0, 99.0), LowHighRange.HIGH: (80.0, 104.0)}


class StubControl:
    """A pump, the heat mode or the temperature range."""

    def __init__(self, client: StubSpaClient, options: list[IntEnum], state: IntEnum) -> None:
        self._client = client
        self.options = options
        self.state = state
        self.obeys = True
        self.requests: list[IntEnum] = []

    async def set_state(self, state: int | IntEnum) -> bool:
        target = next((option for option in self.options if option == state), None)
        if target is None:
            return False
        self.requests.append(target)
        if self._client.connected and self.obeys:
            self.state = target
            self._client.report()
        return True


class StubSpaClient:
    """Implements the adapter's ``BalboaClient`` protocol in memory."""

    def __init__(self, host: str, clock: Clock) -> None:
        self.host = host
        self._clock = clock
        self._callbacks: list[Callable[[], None]] = []

        self.reachable = True
        self.sends_configuration = True
        self.connected = False
        self.disconnect_calls = 0
        self.last_message_received: datetime | None = None

        self.temperature: float | None = 99.0
        self.temperature_unit = TemperatureUnit.FAHRENHEIT
        self.remembered_targets = {LowHighRange.LOW: 99.0, LowHighRange.HIGH: 102.0}
        self.temperature_range = StubControl(self, list(LowHighRange), LowHighRange.LOW)
        self.heat_mode = StubControl(self, [HeatMode.READY, HeatMode.REST], HeatMode.READY)
        self.heat_state = HeatState.OFF
        self.pumps = [
            StubControl(self, list(OffLowHighState), OffLowHighState.OFF),
            StubControl(self, list(OffOnState), OffOnState.OFF),
        ]

        self.obeys_temperature = True
        self.temperatures_sent: list[float] = []
        self.fault: FaultLog | None = None
        self.fault_reply: FaultLog | None = None
        self.fault_requests = 0

    # --- What the adapter calls ----------------------------------------------

    async def connect(self) -> bool:
        if self.reachable:
            self.connected = True
            self.last_message_received = self._clock.now()
        return self.connected

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    async def async_configuration_loaded(self, timeout: float = 15) -> bool:
        return self.sends_configuration

    def on(self, event_name: str, callback: Callable[[], None]) -> Callable[[], None]:
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe

    @property
    def _active_range(self) -> LowHighRange:
        return LowHighRange(self.temperature_range.state)

    @property
    def target_temperature(self) -> float:
        return self.remembered_targets[self._active_range]

    @property
    def temperature_minimum(self) -> float:
        return RANGE_LIMITS[self._active_range][0]

    @property
    def temperature_maximum(self) -> float:
        return RANGE_LIMITS[self._active_range][1]

    async def set_temperature(self, temperature: float) -> None:
        low, high = RANGE_LIMITS[self._active_range]
        if not low <= temperature <= high:
            raise ValueError(f"Invalid temperature: {temperature} (expected {low}..{high})")
        self.temperatures_sent.append(temperature)
        if self.connected and self.obeys_temperature:
            self.remembered_targets[self._active_range] = temperature
            self.report()

    async def set_temperature_range(self, temperature_range: LowHighRange) -> None:
        await self.temperature_range.set_state(temperature_range)

    async def request_fault_log(self) -> None:
        self.fault_requests += 1
        if self.connected and self.fault_reply is not None:
            # Like the library: stored without raising an update event.
            self.fault = self.fault_reply
            self.last_message_received = self._clock.now()

    # --- What tests call -----------------------------------------------------

    @property
    def listener_count(self) -> int:
        return len(self._callbacks)

    def report(self) -> None:
        """Simulate a status message that changed something."""
        self.last_message_received = self._clock.now()
        self.notify()

    def notify(self) -> None:
        """Call the update callbacks without touching anything else."""
        for callback in list(self._callbacks):
            callback()

    def drop(self) -> None:
        """Simulate the connection closing; the spa stays unreachable."""
        self.connected = False
        self.reachable = False

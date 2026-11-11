"""The Balboa adapter, tested against a stub of the library (no tub needed).

Waits are real but tiny: the settings below shrink every timeout to a few
milliseconds, so the whole file runs in well under a second.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pybalboa.control import FaultLog
from pybalboa.discovery import DiscoveredSpa
from pybalboa.enums import HeatMode as BalboaHeatMode
from pybalboa.enums import HeatState, LowHighRange, OffLowHighState, OffOnState, TemperatureUnit

from hottub.domain import (
    CommandRejectedError,
    HeaterState,
    HeatMode,
    PumpSpeed,
    SpaStateChanged,
    SpaUnavailableError,
    Temperature,
)
from hottub.spa.balboa import BalboaSettings, BalboaSpaAdapter
from tests.fakes.balboa import StubSpaClient
from tests.fakes.clock import FakeClock

SPA_HOST = "10.0.0.5"
SPA_MAC = "00:15:27:AB:CD:EF"

FAST = BalboaSettings(
    mac_suffix="ABCDEF",
    confirm_timeout_s=0.05,
    command_timeout_s=0.3,
    poll_interval_s=0.002,
    health_check_interval_s=0.005,
    reconnect_initial_delay_s=0.005,
    reconnect_max_delay_s=0.02,
)


class StubNetwork:
    """The world outside the adapter: which hosts answer, what discovery finds."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.reachable_hosts = {SPA_HOST}
        self.sends_configuration = True
        self.discovered = [DiscoveredSpa(SPA_HOST, 4257, SPA_MAC, "BWGSPA")]
        self.discoveries = 0
        self.clients: list[StubSpaClient] = []

    def make_client(self, host: str) -> StubSpaClient:
        client = StubSpaClient(host, self._clock)
        client.reachable = host in self.reachable_hosts
        client.sends_configuration = self.sends_configuration
        self.clients.append(client)
        return client

    async def discover(self, timeout_s: int) -> list[DiscoveredSpa]:
        self.discoveries += 1
        return list(self.discovered)

    @property
    def client(self) -> StubSpaClient:
        """The client most recently created."""
        return self.clients[-1]


async def eventually(condition: Callable[[], bool], timeout_s: float = 1.0) -> None:
    """Wait for something a background task is expected to do."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.002)


async def let_updates_publish() -> None:
    """Give the adapter the event-loop turn it defers publishing to."""
    await asyncio.sleep(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 11, 18, 0, tzinfo=UTC))


@pytest.fixture
def network(clock: FakeClock) -> StubNetwork:
    return StubNetwork(clock)


AdapterFactory = Callable[[BalboaSettings], BalboaSpaAdapter]


@pytest.fixture
async def make_adapter(clock: FakeClock, network: StubNetwork) -> AsyncIterator[AdapterFactory]:
    """Build adapters wired to the stub network; disconnect them afterwards."""
    made: list[BalboaSpaAdapter] = []

    def make(settings: BalboaSettings) -> BalboaSpaAdapter:
        adapter = BalboaSpaAdapter(
            clock, settings, client_factory=network.make_client, discover=network.discover
        )
        made.append(adapter)
        return adapter

    yield make
    for adapter in made:
        await adapter.disconnect()


@pytest.fixture
async def spa(make_adapter: AdapterFactory) -> BalboaSpaAdapter:
    """A connected adapter with the fast settings."""
    adapter = make_adapter(FAST)
    await adapter.connect()
    return adapter


# --- Finding and connecting ---------------------------------------------------


async def test_it_finds_the_spa_by_mac_suffix(spa: BalboaSpaAdapter, network: StubNetwork) -> None:
    assert spa.connected
    assert network.client.host == SPA_HOST
    assert network.discoveries == 1


async def test_it_ignores_other_spas_on_the_network(
    make_adapter: AdapterFactory, network: StubNetwork
) -> None:
    network.discovered.insert(0, DiscoveredSpa("10.0.0.9", 4257, "00:15:27:11:22:33", "BWGSPA"))
    adapter = make_adapter(FAST)

    await adapter.connect()

    assert network.client.host == SPA_HOST


async def test_connecting_fails_when_our_spa_does_not_answer_discovery(
    make_adapter: AdapterFactory, network: StubNetwork
) -> None:
    network.discovered = [DiscoveredSpa("10.0.0.9", 4257, "00:15:27:11:22:33", "BWGSPA")]
    adapter = make_adapter(FAST)

    with pytest.raises(SpaUnavailableError, match="ABCDEF"):
        await adapter.connect()

    assert not adapter.connected


async def test_without_a_mac_suffix_a_single_spa_is_accepted_but_two_are_not(
    make_adapter: AdapterFactory, network: StubNetwork
) -> None:
    any_spa = replace(FAST, mac_suffix=None)
    await make_adapter(any_spa).connect()
    assert network.client.host == SPA_HOST

    network.discovered.append(DiscoveredSpa("10.0.0.9", 4257, "00:15:27:11:22:33", "BWGSPA"))
    with pytest.raises(SpaUnavailableError, match="2 spas"):
        await make_adapter(any_spa).connect()


async def test_a_configured_host_is_used_without_discovery(
    make_adapter: AdapterFactory, network: StubNetwork
) -> None:
    adapter = make_adapter(replace(FAST, host=SPA_HOST))

    await adapter.connect()

    assert adapter.connected
    assert network.discoveries == 0


async def test_a_network_error_during_discovery_is_reported_as_unavailable(
    clock: FakeClock, network: StubNetwork
) -> None:
    async def broken_discover(timeout_s: int) -> list[DiscoveredSpa]:
        raise OSError("Network is unreachable")

    adapter = BalboaSpaAdapter(
        clock, FAST, client_factory=network.make_client, discover=broken_discover
    )
    try:
        with pytest.raises(SpaUnavailableError, match="Network is unreachable"):
            await adapter.connect()
    finally:
        await adapter.disconnect()


async def test_a_spa_that_never_sends_its_configuration_is_unavailable(
    make_adapter: AdapterFactory, network: StubNetwork
) -> None:
    network.sends_configuration = False
    adapter = make_adapter(FAST)

    with pytest.raises(SpaUnavailableError, match="configuration"):
        await adapter.connect()

    assert network.clients[0].disconnect_calls == 1  # not left half-open


async def test_a_spa_set_to_celsius_is_refused(
    clock: FakeClock, network: StubNetwork, make_adapter: AdapterFactory
) -> None:
    def celsius_client(host: str) -> StubSpaClient:
        client = network.make_client(host)
        client.temperature_unit = TemperatureUnit.CELSIUS
        return client

    adapter = BalboaSpaAdapter(
        clock, FAST, client_factory=celsius_client, discover=network.discover
    )
    try:
        with pytest.raises(SpaUnavailableError, match="Celsius"):
            await adapter.connect()
    finally:
        await adapter.disconnect()


async def test_connecting_twice_keeps_the_one_connection(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    await spa.connect()

    assert len(network.clients) == 1


async def test_disconnecting_closes_the_connection_and_is_safe_to_repeat(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    await spa.disconnect()
    await spa.disconnect()

    assert not spa.connected
    assert network.client.disconnect_calls == 1
    assert network.client.listener_count == 0


# --- Reading state -----------------------------------------------------------


async def test_state_is_available_as_soon_as_it_connects(
    spa: BalboaSpaAdapter, clock: FakeClock
) -> None:
    state = spa.state

    assert state is not None
    assert state.observed_at == clock.now()
    assert state.water_temperature == Temperature(99.0)
    assert state.set_temperature == Temperature(99.0)
    assert state.heat_mode is HeatMode.READY
    assert state.heater is HeaterState.OFF
    assert state.pump1 is PumpSpeed.OFF
    assert state.pump2_on is False


async def test_library_values_are_translated_to_domain_terms(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    client = network.client
    client.temperature = None  # no reading, e.g. in Rest between filter cycles
    client.heat_mode.state = BalboaHeatMode.READY_IN_REST
    client.heat_state = HeatState.HEAT_WAITING
    client.pumps[0].state = OffLowHighState.HIGH
    client.pumps[1].state = OffOnState.ON
    client.report()
    await let_updates_publish()

    state = spa.state
    assert state is not None
    assert state.water_temperature is None
    assert state.heat_mode is HeatMode.REST
    assert state.heater is HeaterState.WAITING
    assert state.pump1 is PumpSpeed.HIGH
    assert state.pump2_on is True


async def test_listeners_hear_changes_and_only_changes(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    events: list[SpaStateChanged] = []
    spa.subscribe(events.append)

    network.client.report()  # nothing changed
    await let_updates_publish()
    assert events == []

    network.client.temperature = 100.0
    network.client.report()
    await let_updates_publish()
    assert len(events) == 1
    assert events[0].previous is not None
    assert events[0].previous.water_temperature == Temperature(99.0)
    assert events[0].current.water_temperature == Temperature(100.0)


async def test_a_half_read_status_message_is_never_announced(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    # The library calls back once per changed control, before it has read the
    # rest of the message. Here both pumps change in one message.
    events: list[SpaStateChanged] = []
    spa.subscribe(events.append)
    client = network.client

    client.pumps[0].state = OffLowHighState.LOW
    client.notify()  # pump 2 still shows its old value
    client.pumps[1].state = OffOnState.ON
    client.notify()
    client.report()
    await let_updates_publish()

    assert len(events) == 1
    assert events[0].current.pump1 is PumpSpeed.LOW
    assert events[0].current.pump2_on is True


async def test_an_unsubscribed_listener_hears_nothing_more(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    events: list[SpaStateChanged] = []
    unsubscribe = spa.subscribe(events.append)
    unsubscribe()

    network.client.temperature = 100.0
    network.client.report()
    await let_updates_publish()

    assert events == []


async def test_a_failing_listener_does_not_silence_the_others(
    spa: BalboaSpaAdapter, network: StubNetwork, caplog: pytest.LogCaptureFixture
) -> None:
    def faulty(event: SpaStateChanged) -> None:
        raise RuntimeError("listener bug")

    events: list[SpaStateChanged] = []
    spa.subscribe(faulty)
    spa.subscribe(events.append)

    network.client.temperature = 100.0
    with caplog.at_level(logging.ERROR):
        network.client.report()
        await let_updates_publish()

    assert len(events) == 1
    assert "listener failed" in caplog.text


async def test_last_message_time_follows_the_spa_even_when_nothing_changes(
    spa: BalboaSpaAdapter, network: StubNetwork, clock: FakeClock
) -> None:
    clock.advance(seconds=10)
    network.client.report()

    assert spa.last_message_at == clock.now()


# --- Set temperature ---------------------------------------------------------


async def test_setting_a_temperature_within_the_active_range(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    await spa.set_temperature(Temperature(95.0))

    assert network.client.temperatures_sent == [95.0]
    assert network.client.temperature_range.requests == []
    assert spa.state is not None
    assert spa.state.set_temperature == Temperature(95.0)


async def test_fractions_are_rounded_to_a_whole_degree(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    await spa.set_temperature(Temperature(94.5))

    assert network.client.temperatures_sent == [95.0]


async def test_a_temperature_above_the_low_range_switches_to_the_high_range_first(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    client = network.client
    client.remembered_targets[LowHighRange.HIGH] = 104.0

    await spa.set_temperature(Temperature(101.0))

    assert client.temperature_range.state == LowHighRange.HIGH
    assert client.temperatures_sent == [101.0]
    assert client.target_temperature == 101.0


async def test_a_temperature_below_the_high_range_switches_to_the_low_range_first(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    client = network.client
    client.temperature_range.state = LowHighRange.HIGH

    await spa.set_temperature(Temperature(60.0))

    assert client.temperature_range.state == LowHighRange.LOW
    assert client.temperatures_sent == [60.0]


async def test_nothing_is_sent_when_the_spa_already_holds_that_temperature(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    await spa.set_temperature(Temperature(99.0))

    assert network.client.temperatures_sent == []


@pytest.mark.parametrize("fahrenheit", [49.0, 105.0])
async def test_a_temperature_the_spa_cannot_hold_is_rejected_before_anything_is_sent(
    spa: BalboaSpaAdapter, network: StubNetwork, fahrenheit: float
) -> None:
    with pytest.raises(CommandRejectedError):
        await spa.set_temperature(Temperature(fahrenheit))

    assert network.client.temperatures_sent == []
    assert network.client.temperature_range.requests == []


async def test_an_unconfirmed_range_switch_is_an_error_and_no_temperature_is_sent(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    network.client.temperature_range.obeys = False

    with pytest.raises(CommandRejectedError, match="high temperature range"):
        await spa.set_temperature(Temperature(102.0))

    assert network.client.temperatures_sent == []


async def test_an_unconfirmed_set_temperature_is_an_error(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    network.client.obeys_temperature = False

    with pytest.raises(CommandRejectedError, match="did not confirm"):
        await spa.set_temperature(Temperature(95.0))


# --- Pumps and heat mode -----------------------------------------------------


async def test_pump1_can_be_set_to_each_speed(spa: BalboaSpaAdapter) -> None:
    for speed in (PumpSpeed.HIGH, PumpSpeed.LOW, PumpSpeed.OFF):
        await spa.set_pump1(speed)
        assert spa.state is not None
        assert spa.state.pump1 is speed


async def test_the_spa_may_overrule_a_pump_request_without_it_being_an_error(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    # As when the heater is running and the spa keeps pump 1 on low.
    pump1 = network.client.pumps[0]
    pump1.state = OffLowHighState.LOW
    pump1.obeys = False

    await spa.set_pump1(PumpSpeed.OFF)

    assert pump1.requests == [OffLowHighState.OFF]
    assert pump1.state == OffLowHighState.LOW


async def test_pump2_can_be_switched_on_and_off(spa: BalboaSpaAdapter) -> None:
    await spa.set_pump2(True)
    assert spa.state is not None
    assert spa.state.pump2_on is True

    await spa.set_pump2(False)
    assert spa.state.pump2_on is False


async def test_nothing_is_sent_when_a_pump_is_already_as_requested(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    await spa.set_pump2(False)

    assert network.client.pumps[1].requests == []


async def test_heat_mode_can_be_switched_to_rest_and_back(spa: BalboaSpaAdapter) -> None:
    modes: list[HeatMode] = []
    spa.subscribe(lambda event: modes.append(event.current.heat_mode))

    await spa.set_heat_mode(HeatMode.REST)
    await spa.set_heat_mode(HeatMode.READY)

    assert modes == [HeatMode.REST, HeatMode.READY]


async def test_asking_for_rest_while_ready_in_rest_sends_nothing(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    network.client.heat_mode.state = BalboaHeatMode.READY_IN_REST

    await spa.set_heat_mode(HeatMode.REST)

    assert network.client.heat_mode.requests == []


async def test_asking_for_ready_while_ready_in_rest_switches_the_mode(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    network.client.heat_mode.state = BalboaHeatMode.READY_IN_REST

    await spa.set_heat_mode(HeatMode.READY)

    assert network.client.heat_mode.state == BalboaHeatMode.READY


async def test_an_unconfirmed_heat_mode_is_an_error(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    network.client.heat_mode.obeys = False

    with pytest.raises(CommandRejectedError, match="did not confirm"):
        await spa.set_heat_mode(HeatMode.REST)


async def test_commands_issued_together_are_sent_one_after_the_other(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    await asyncio.gather(
        spa.set_pump1(PumpSpeed.HIGH), spa.set_pump1(PumpSpeed.HIGH), spa.set_pump2(True)
    )

    # The second identical request found the pump already on high.
    assert network.client.pumps[0].requests == [OffLowHighState.HIGH]
    assert network.client.pumps[1].requests == [OffOnState.ON]


# --- Stalls of the Wi-Fi module ------------------------------------------------
# The real module goes silent for about 20 s at a time with the connection
# open. In these tests "silent" is the clock moving on with no report.


async def test_a_command_waits_for_a_stalled_link_before_sending(
    spa: BalboaSpaAdapter, network: StubNetwork, clock: FakeClock
) -> None:
    client = network.client
    clock.advance(seconds=10)  # nothing heard for 10 s: a stall

    command = asyncio.create_task(spa.set_pump2(True))
    await asyncio.sleep(0.02)
    assert client.pumps[1].requests == []  # held back, not sent into the void

    client.report()  # the spa is talking again
    await command

    assert client.pumps[1].requests == [OffOnState.ON]


async def test_a_command_is_not_sent_if_the_link_stays_stalled(
    spa: BalboaSpaAdapter, network: StubNetwork, clock: FakeClock
) -> None:
    clock.advance(seconds=10)

    with pytest.raises(SpaUnavailableError, match="not sent"):
        await spa.set_heat_mode(HeatMode.REST)

    assert network.client.heat_mode.requests == []


async def test_confirmation_is_awaited_through_a_stall_that_starts_after_sending(
    spa: BalboaSpaAdapter, network: StubNetwork, clock: FakeClock
) -> None:
    client = network.client
    client.heat_mode.obeys = False  # the spa will act on it only after the stall

    command = asyncio.create_task(spa.set_heat_mode(HeatMode.REST))
    await eventually(lambda: client.heat_mode.requests != [])
    clock.advance(seconds=10)  # the stall begins
    await asyncio.sleep(0.1)  # twice the confirmation timeout
    assert not command.done()

    client.heat_mode.state = BalboaHeatMode.REST  # the stall ends; the toggle lands
    client.report()
    await command

    assert spa.state is not None
    assert spa.state.heat_mode is HeatMode.REST


async def test_a_command_never_confirmed_because_the_spa_went_quiet_is_unavailable(
    spa: BalboaSpaAdapter, network: StubNetwork, clock: FakeClock
) -> None:
    client = network.client
    client.heat_mode.obeys = False

    command = asyncio.create_task(spa.set_heat_mode(HeatMode.REST))
    await eventually(lambda: client.heat_mode.requests != [])
    clock.advance(seconds=10)

    with pytest.raises(SpaUnavailableError, match="went quiet"):
        await command


# --- Fault log ---------------------------------------------------------------


def a_fault(code: int = 16, count: int = 20) -> FaultLog:
    """A fault-log reply as the library builds it: 12:00 today on the spa's clock."""
    return FaultLog(count, count - 1, code, 0, 12, 0, 0, 99, 99, 99, datetime(2026, 11, 11, 13, 0))


async def test_the_latest_fault_is_requested_and_translated(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    network.client.fault_reply = a_fault(code=16)

    fault = await spa.latest_fault()

    assert fault is not None
    assert fault.code == 16
    assert fault.message == "The water flow is low"
    # The spa's naive local time becomes an aware UTC time.
    assert fault.occurred_at == datetime(2026, 11, 11, 12, 0).astimezone(UTC)
    assert fault.occurred_at.tzinfo is UTC


async def test_each_call_asks_the_spa_again(spa: BalboaSpaAdapter, network: StubNetwork) -> None:
    network.client.fault_reply = a_fault(code=16)
    await spa.latest_fault()
    network.client.fault_reply = a_fault(code=27)

    fault = await spa.latest_fault()

    assert fault is not None
    assert fault.message == "The heater is dry"
    assert network.client.fault_requests == 2


async def test_an_empty_fault_log_is_none(spa: BalboaSpaAdapter, network: StubNetwork) -> None:
    network.client.fault_reply = FaultLog(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    assert await spa.latest_fault() is None


async def test_an_unanswered_fault_request_is_reported_as_unavailable(
    spa: BalboaSpaAdapter,
) -> None:
    with pytest.raises(SpaUnavailableError, match="did not answer"):
        await spa.latest_fault()


# --- Losing and regaining the connection -------------------------------------


async def test_commands_fail_loudly_while_the_spa_is_disconnected(
    make_adapter: AdapterFactory, network: StubNetwork
) -> None:
    # Slow supervisor, so the adapter is still disconnected when we look.
    adapter = make_adapter(replace(FAST, health_check_interval_s=30.0))
    await adapter.connect()
    network.client.drop()

    with pytest.raises(SpaUnavailableError):
        await adapter.set_temperature(Temperature(95.0))
    with pytest.raises(SpaUnavailableError):
        await adapter.set_pump1(PumpSpeed.HIGH)
    with pytest.raises(SpaUnavailableError):
        await adapter.set_pump2(True)
    with pytest.raises(SpaUnavailableError):
        await adapter.set_heat_mode(HeatMode.REST)
    with pytest.raises(SpaUnavailableError):
        await adapter.latest_fault()

    assert network.client.temperatures_sent == []
    assert adapter.state is not None  # the last known state is kept


async def test_losing_the_connection_while_waiting_for_confirmation_is_unavailable(
    make_adapter: AdapterFactory, network: StubNetwork
) -> None:
    adapter = make_adapter(replace(FAST, health_check_interval_s=30.0, confirm_timeout_s=1.0))
    await adapter.connect()
    client = network.client
    client.heat_mode.obeys = False

    command = asyncio.create_task(adapter.set_heat_mode(HeatMode.REST))
    await eventually(lambda: client.heat_mode.requests != [])
    client.drop()

    with pytest.raises(SpaUnavailableError, match="lost"):
        await command


async def test_it_reconnects_after_the_connection_drops(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    events: list[SpaStateChanged] = []
    spa.subscribe(events.append)
    first = network.client

    first.connected = False  # dropped, but the spa still answers
    await eventually(lambda: spa.connected)

    second = network.client
    assert second is not first
    assert first.disconnect_calls == 1
    assert first.listener_count == 0  # see the shared-listener note in the adapter

    second.temperature = 98.0
    second.report()
    await let_updates_publish()
    assert [event.current.water_temperature for event in events] == [Temperature(98.0)]


async def test_it_reconnects_when_the_connection_is_open_but_silent(
    spa: BalboaSpaAdapter, network: StubNetwork, clock: FakeClock
) -> None:
    first = network.client

    clock.advance(seconds=61)  # nothing heard for longer than the limit
    await eventually(lambda: network.client is not first)

    assert first.disconnect_calls == 1
    assert spa.connected


async def test_it_keeps_trying_until_the_spa_comes_back(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    network.reachable_hosts.clear()
    network.client.drop()
    await eventually(lambda: len(network.clients) >= 4)  # several failed attempts
    assert not spa.connected

    network.reachable_hosts.add(SPA_HOST)
    await eventually(lambda: spa.connected)


async def test_it_follows_the_spa_to_a_new_address(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    new_host = "10.0.0.77"  # the router handed out a different address
    network.reachable_hosts = {new_host}
    network.discovered = [DiscoveredSpa(new_host, 4257, SPA_MAC, "BWGSPA")]
    network.client.drop()

    await eventually(lambda: spa.connected)

    assert network.client.host == new_host


async def test_the_last_address_is_tried_before_searching_again(
    spa: BalboaSpaAdapter, network: StubNetwork
) -> None:
    first = network.client

    first.connected = False
    await eventually(lambda: spa.connected)

    assert network.discoveries == 1  # only the original search


async def test_a_failed_first_connection_is_retried_in_the_background(
    make_adapter: AdapterFactory, network: StubNetwork
) -> None:
    network.reachable_hosts.clear()
    adapter = make_adapter(FAST)
    with pytest.raises(SpaUnavailableError):
        await adapter.connect()

    network.reachable_hosts.add(SPA_HOST)  # the tub is switched on

    await eventually(lambda: adapter.connected)
    assert adapter.state is not None


async def test_the_last_message_time_survives_a_lost_connection(
    make_adapter: AdapterFactory, network: StubNetwork, clock: FakeClock
) -> None:
    adapter = make_adapter(replace(FAST, health_check_interval_s=0.005))
    await adapter.connect()
    heard = clock.now()
    network.reachable_hosts.clear()
    network.client.drop()
    await eventually(lambda: len(network.clients) >= 2)

    clock.advance(minutes=5)

    assert adapter.last_message_at == heard

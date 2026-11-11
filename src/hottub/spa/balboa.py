"""The adapter for the real Balboa controller.

Patterns: Adapter (GoF) and Anti-Corruption Layer (Evans).
:class:`BalboaSpaAdapter` implements :class:`~hottub.spa.port.SpaPort` on top
of the ``pybalboa`` library and translates the library's types and failures
into the domain's. This is the only module that imports ``pybalboa``; replacing
the library, or working around a new quirk of it, should touch this file only.

Read ``docs/balboa-notes.md`` first. Each oddity handled below is described
there, with how it was found.

Responsibilities
----------------
* **Find the spa.** By a configured address, or by a discovery broadcast
  matched on the Wi-Fi module's MAC address (its address comes from DHCP and
  may change).
* **Keep the connection alive.** One background task, the *supervisor*, checks
  the connection every few seconds and rebuilds it when it is closed or has
  gone silent. The library's own reconnection is not used, because it can only
  ever retry the address it started with.
* **Report state.** Library updates are translated into
  :class:`~hottub.domain.SpaState` and listeners are told when the readings
  change.
* **Send commands and confirm them.** The library reports a command as done
  once the bytes are written. Here a command returns only after the spa's own
  status shows the new value, or the wait times out.

What the library makes necessary
--------------------------------
* It **drops commands silently** when the connection is down, so every
  command checks the connection first.
* The Wi-Fi module **stalls** for about 20 seconds roughly once a minute,
  connection open, nothing arriving and nothing acted on. Commands wait for
  the link to be live before sending and wait through a stall for their
  confirmation (:meth:`BalboaSpaAdapter._wait_until`).
* Pumps, heat mode and temperature range are **toggles** ("next state"), not
  "set to X". The number of toggles is worked out from the last known state,
  so commands are sent one at a time (a lock) and each waits for the spa to
  catch up before the next is worked out.
* Its **event listeners are shared** between every client and control object,
  and controls fire while a status message is only half read. Updates are
  therefore collected and published once the message is complete (see
  :meth:`BalboaSpaAdapter._on_library_update`).
* The spa has **two temperature ranges** and the library accepts only values
  in the active one, so :meth:`BalboaSpaAdapter.set_temperature` switches
  range first when it has to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Protocol

from pybalboa import EVENT_UPDATE, SpaClient
from pybalboa.control import FaultLog
from pybalboa.discovery import DiscoveredSpa, async_discover
from pybalboa.enums import HeatMode as BalboaHeatMode
from pybalboa.enums import (
    HeatState,
    LowHighRange,
    OffLowHighState,
    OffOnState,
    TemperatureUnit,
)
from pybalboa.exceptions import SpaConfigurationNotLoadedError

from hottub.domain import (
    SPA_MAXIMUM_F,
    SPA_MINIMUM_F,
    Clock,
    CommandRejectedError,
    HeaterState,
    HeatMode,
    PumpSpeed,
    SpaFault,
    SpaState,
    SpaStateChanged,
    SpaStateListener,
    SpaUnavailableError,
    Temperature,
    Unsubscribe,
)
from hottub.spa.port import SpaPort

_LOGGER = logging.getLogger(__name__)

#: Set-temperature limits of the controller's two ranges, in °F, as measured
#: on this spa (``docs/balboa-notes.md``). The library exposes only the active
#: range's limits, so a copy is kept here; it is compared with the library's
#: figures at connect time and a mismatch is logged.
_RANGE_LIMITS_F: dict[LowHighRange, tuple[float, float]] = {
    LowHighRange.LOW: (50.0, 99.0),
    LowHighRange.HIGH: (80.0, 104.0),
}

_PUMP1_TO_BALBOA: dict[PumpSpeed, OffLowHighState] = {
    PumpSpeed.OFF: OffLowHighState.OFF,
    PumpSpeed.LOW: OffLowHighState.LOW,
    PumpSpeed.HIGH: OffLowHighState.HIGH,
}
_PUMP1_FROM_BALBOA: dict[int, PumpSpeed] = {
    balboa.value: speed for speed, balboa in _PUMP1_TO_BALBOA.items()
}

_HEATER_FROM_BALBOA: dict[int, HeaterState] = {
    HeatState.OFF.value: HeaterState.OFF,
    HeatState.HEATING.value: HeaterState.HEATING,
    HeatState.HEAT_WAITING.value: HeaterState.WAITING,
}

#: Ready-in-Rest is reported as Rest (PLAN.md, section 1).
_HEAT_MODE_FROM_BALBOA: dict[int, HeatMode] = {
    BalboaHeatMode.READY.value: HeatMode.READY,
    BalboaHeatMode.REST.value: HeatMode.REST,
    BalboaHeatMode.READY_IN_REST.value: HeatMode.REST,
}


# --- The part of pybalboa this adapter relies on -----------------------------
# Written out as Protocols for two reasons: it documents exactly which library
# features are in use, and it lets the tests substitute a stub without a tub.


class BalboaControl(Protocol):
    """A switchable item on the spa: a pump, the heat mode, the range."""

    @property
    def state(self) -> IntEnum:
        """Current state, or the library's ``UnknownState.UNKNOWN``."""
        ...

    @property
    def options(self) -> Sequence[IntEnum]:
        """The states that may be requested."""
        ...

    async def set_state(self, state: int | IntEnum) -> bool:
        """Send toggles towards ``state``. False if it is not an option."""
        ...


class BalboaClient(Protocol):
    """The subset of ``pybalboa.SpaClient`` used here."""

    async def connect(self) -> bool:
        """Open the connection. Returns whether it worked; does not raise."""
        ...

    async def disconnect(self) -> None:
        """Close the connection and stop the library's background tasks."""
        ...

    async def async_configuration_loaded(self, timeout: float = 15) -> bool:
        """Wait until the spa has described itself. False on timeout."""
        ...

    def on(self, event_name: str, callback: Callable[[], None]) -> Callable[[], None]:
        """Register an update callback; returns the function that removes it."""
        ...

    @property
    def connected(self) -> bool:
        """Whether the TCP connection is open."""
        ...

    @property
    def last_message_received(self) -> datetime | None:
        """When any message last arrived, timezone-aware UTC."""
        ...

    @property
    def temperature(self) -> float | None:
        """Water temperature, or ``None`` when the spa has no reading."""
        ...

    @property
    def target_temperature(self) -> float:
        """Set temperature. Raises until the first status has arrived."""
        ...

    @property
    def temperature_minimum(self) -> float:
        """Lowest set temperature accepted in the active range."""
        ...

    @property
    def temperature_maximum(self) -> float:
        """Highest set temperature accepted in the active range."""
        ...

    @property
    def temperature_unit(self) -> TemperatureUnit:
        """The unit the spa is configured for."""
        ...

    @property
    def temperature_range(self) -> BalboaControl:
        """The low/high range switch."""
        ...

    @property
    def heat_mode(self) -> BalboaControl:
        """The Ready/Rest switch."""
        ...

    @property
    def heat_state(self) -> HeatState:
        """What the heater is doing."""
        ...

    @property
    def pumps(self) -> Sequence[BalboaControl]:
        """The pumps, pump 1 first."""
        ...

    @property
    def fault(self) -> FaultLog | None:
        """The fault-log entry most recently received, if any."""
        ...

    async def request_fault_log(self) -> None:
        """Ask for the newest fault-log entry; the reply arrives later."""
        ...

    async def set_temperature(self, temperature: float) -> None:
        """Send a set temperature. ValueError outside the active range."""
        ...

    async def set_temperature_range(self, temperature_range: LowHighRange) -> None:
        """Toggle to the given range if it is not already active."""
        ...


#: Builds a library client for an address. Replaced by a stub in tests.
ClientFactory = Callable[[str], BalboaClient]

#: Looks for spas on the local network for up to the given number of seconds.
Discover = Callable[[int], Awaitable[Sequence[DiscoveredSpa]]]


async def _discover_by_broadcast(timeout_s: int) -> Sequence[DiscoveredSpa]:
    """Broadcast for spas and collect every answer until the timeout."""
    return await async_discover(timeout=timeout_s)


@dataclass(frozen=True)
class BalboaSettings:
    """How to find the spa, and how patient to be with it.

    The defaults suit the real tub. Tests shorten the waits.

    Attributes:
        host: Address of the spa's Wi-Fi module. When set, discovery is never
            used. Leave as ``None`` unless the address is reserved on the
            router.
        mac_suffix: The end of the Wi-Fi module's MAC address, e.g.
            ``"ABCDEF"`` (it is the tail of the module's own SSID). Used to
            pick the right spa from the discovery answers. With ``None``, a
            single answering spa is accepted and several are an error.
        discovery_timeout_s: How long to collect discovery answers.
        configuration_timeout_s: How long to wait, after connecting, for the
            spa to describe its equipment. Long enough to sit out a stall of
            the Wi-Fi module, which is better than hammering it with
            reconnections.
        confirm_timeout_s: How long a command waits for the spa's status to
            show the requested value, counting only time during which the
            spa is talking. Confirmation normally takes under half a second.
        link_live_window_s: The link counts as live if anything arrived
            within this many seconds. The spa reports about three times a
            second.
        command_timeout_s: The longest a command may take in all, including
            waiting out a stall of the Wi-Fi module (about 20 seconds).
        poll_interval_s: How often those waits look at the status.
        silence_before_reconnect_s: A connection that is open but has
            delivered nothing for this long is torn down and rebuilt. The spa
            normally reports several times a second.
        health_check_interval_s: How often the supervisor checks the
            connection.
        reconnect_initial_delay_s: Pause after the first failed attempt to
            reconnect; doubled after each further failure.
        reconnect_max_delay_s: Upper limit for that pause.
    """

    host: str | None = None
    mac_suffix: str | None = None
    discovery_timeout_s: int = 5
    configuration_timeout_s: float = 45.0
    confirm_timeout_s: float = 5.0
    link_live_window_s: float = 2.0
    command_timeout_s: float = 45.0
    poll_interval_s: float = 0.1
    silence_before_reconnect_s: float = 60.0
    health_check_interval_s: float = 5.0
    reconnect_initial_delay_s: float = 2.0
    reconnect_max_delay_s: float = 60.0


def _normalise_mac(mac: str) -> str:
    """Return ``mac`` upper-cased with separators removed, for suffix matching."""
    return mac.upper().replace(":", "").replace("-", "")


def _translate(client: BalboaClient, observed_at: datetime) -> SpaState | None:
    """Describe the library client's current state in domain terms.

    Returns ``None`` while the library does not yet know enough to fill in a
    complete snapshot, which happens briefly after connecting.
    """
    pumps = client.pumps
    if len(pumps) < 2:
        return None
    try:
        set_temperature = client.target_temperature
    except SpaConfigurationNotLoadedError:
        return None
    heat_mode = _HEAT_MODE_FROM_BALBOA.get(int(client.heat_mode.state))
    heater = _HEATER_FROM_BALBOA.get(int(client.heat_state))
    pump1 = _PUMP1_FROM_BALBOA.get(int(pumps[0].state))
    pump2 = int(pumps[1].state)
    if heat_mode is None or heater is None or pump1 is None or pump2 < 0:
        return None
    water = client.temperature
    return SpaState(
        observed_at=observed_at,
        water_temperature=None if water is None else Temperature(water),
        set_temperature=Temperature(set_temperature),
        heat_mode=heat_mode,
        heater=heater,
        pump1=pump1,
        pump2_on=pump2 != OffOnState.OFF,
    )


class BalboaSpaAdapter(SpaPort):
    """Talks to the Balboa controller through its Wi-Fi module.

    Call :meth:`connect` once at start-up and :meth:`disconnect` at shutdown.
    In between, the adapter looks after the connection itself.

    Args:
        clock: Source of the time stamped on each state snapshot.
        settings: Where the spa is and how long to wait for it.
        client_factory: Builds the library client; tests pass a stub.
        discover: Finds spas on the network; tests pass a stub.
    """

    def __init__(
        self,
        clock: Clock,
        settings: BalboaSettings | None = None,
        *,
        client_factory: ClientFactory = SpaClient,
        discover: Discover = _discover_by_broadcast,
    ) -> None:
        """Create a disconnected adapter. Nothing touches the network yet."""
        self._clock = clock
        self._settings = settings or BalboaSettings()
        self._client_factory = client_factory
        self._discover = discover

        self._client: BalboaClient | None = None
        self._unsubscribe_from_client: Callable[[], None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        # Address that last worked; tried before a new discovery.
        self._last_good_host: str | None = None

        self._state: SpaState | None = None
        self._last_message_at: datetime | None = None
        self._listeners: list[SpaStateListener] = []
        self._publish_scheduled = False

        # Commands are toggles computed from the current state, so two of
        # them must never overlap.
        self._command_lock = asyncio.Lock()

    # --- SpaPort: connection -------------------------------------------------

    async def connect(self) -> None:
        """Find the spa, connect, and start looking after the connection.

        If this first attempt fails the error is raised, but the supervisor
        is running all the same and keeps trying; the application can start
        while the tub is switched off and will pick it up when it returns.
        Calling this again while already started does nothing.

        Raises:
            SpaUnavailableError: If the spa could not be found or reached now.
        """
        if self._supervisor is not None:
            return
        try:
            await self._open_client()
        finally:
            self._supervisor = asyncio.create_task(
                self._supervise(), name="spa-connection-supervisor"
            )

    async def disconnect(self) -> None:
        """Stop the supervisor and close the connection."""
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        await self._close_client()

    @property
    def connected(self) -> bool:
        """Whether the connection to the spa is currently open."""
        return self._client is not None and self._client.connected

    @property
    def last_message_at(self) -> datetime | None:
        """When the spa was last heard from, across reconnections."""
        if self._client is not None and self._client.last_message_received is not None:
            return self._client.last_message_received
        return self._last_message_at

    # --- SpaPort: reading ----------------------------------------------------

    @property
    def state(self) -> SpaState | None:
        """The most recent state reported by the spa."""
        return self._state

    def subscribe(self, listener: SpaStateListener) -> Unsubscribe:
        """Register ``listener`` for state changes; returns its remover."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def latest_fault(self) -> SpaFault | None:
        """Request the newest fault-log entry and wait for the reply."""
        async with self._command_lock:
            client = await self._client_with_live_link()
            # Every reply produces a new object, so "a different object" is
            # how a fresh answer is told from the previous one.
            before = client.fault
            await client.request_fault_log()
            if not await self._wait_until(client, lambda: client.fault is not before):
                raise SpaUnavailableError("The spa did not answer the fault-log request")
            fault = client.fault
        if fault is None or fault.count == 0:
            return None
        return SpaFault(
            code=fault.message_code,
            message=fault.message,
            # The library gives a naive time on the spa's clock, which is
            # local time; ``astimezone`` reads a naive value as local.
            occurred_at=fault.fault_datetime.astimezone(UTC),
        )

    # --- SpaPort: commands ---------------------------------------------------

    async def set_temperature(self, temperature: Temperature) -> None:
        """Set the temperature to hold, switching range first if necessary.

        Switching range makes the spa adopt that range's own remembered set
        temperature for a moment, until ours arrives. Listeners may see that
        value pass by.
        """
        whole = temperature.to_whole_degree().fahrenheit
        if not SPA_MINIMUM_F <= whole <= SPA_MAXIMUM_F:
            raise CommandRejectedError(
                f"Set temperature {temperature} is outside {SPA_MINIMUM_F:g}-{SPA_MAXIMUM_F:g} °F"
            )
        async with self._command_lock:
            client = await self._client_with_live_link()
            await self._ensure_range_accepts(client, whole)
            if client.target_temperature == whole:
                return
            try:
                await client.set_temperature(whole)
            except ValueError as error:
                # Only reachable if this spa's range limits differ from
                # _RANGE_LIMITS_F; the connect-time check will have logged it.
                raise CommandRejectedError(str(error)) from error
            if not await self._wait_until(client, lambda: client.target_temperature == whole):
                raise CommandRejectedError(
                    f"The spa did not confirm a set temperature of {whole:g} °F"
                )
            _LOGGER.info("Set temperature is now %g °F", whole)

    async def set_pump1(self, speed: PumpSpeed) -> None:
        """Request a speed for pump 1.

        The spa is allowed to disagree (it keeps the pump on low while it
        heats or filters), so an unconfirmed request is logged, not raised.
        """
        async with self._command_lock:
            client = await self._client_with_live_link()
            await self._switch(client.pumps[0], _PUMP1_TO_BALBOA[speed], client, "Pump 1")

    async def set_pump2(self, on: bool) -> None:
        """Switch pump 2 on or off."""
        async with self._command_lock:
            client = await self._client_with_live_link()
            target = OffOnState.ON if on else OffOnState.OFF
            await self._switch(client.pumps[1], target, client, "Pump 2")

    async def set_heat_mode(self, mode: HeatMode) -> None:
        """Switch between Ready and Rest.

        Raises:
            CommandRejectedError: If the spa's status does not show the new
                mode within the confirmation timeout.
            SpaUnavailableError: If the spa cannot be reached.
        """
        async with self._command_lock:
            client = await self._client_with_live_link()
            control = client.heat_mode

            def in_requested_mode() -> bool:
                # Compared in domain terms, so Ready-in-Rest counts as Rest
                # and asking for Rest then sends nothing.
                return _HEAT_MODE_FROM_BALBOA.get(int(control.state)) is mode

            if in_requested_mode():
                return
            target = BalboaHeatMode.READY if mode is HeatMode.READY else BalboaHeatMode.REST
            if not await control.set_state(target):
                raise CommandRejectedError(f"The spa does not offer heat mode {mode.value}")
            if not await self._wait_until(client, in_requested_mode):
                raise CommandRejectedError(f"The spa did not confirm heat mode {mode.value}")
            _LOGGER.info("Heat mode is now %s", mode.value)

    # --- Command helpers -----------------------------------------------------

    def _require_client(self) -> BalboaClient:
        """Return the connected client, or raise because there is none.

        Needed before every command: the library discards messages without a
        word when its connection is down.
        """
        client = self._client
        if client is None or not client.connected:
            raise SpaUnavailableError("The spa is not connected")
        return client

    def _link_is_live(self, client: BalboaClient) -> bool:
        """Return whether the spa has been heard from in the last moment.

        The spa reports about three times a second, but this Wi-Fi module
        also falls completely silent for some 20 seconds roughly once a
        minute, with the connection still open (``docs/balboa-notes.md``,
        "Stalls"). During a stall nothing sent is acted on or answered.
        """
        heard = client.last_message_received
        if heard is None:
            return False
        return self._clock.now() - heard <= timedelta(seconds=self._settings.link_live_window_s)

    async def _client_with_live_link(self) -> BalboaClient:
        """Return the client once the spa is talking, ready for a command.

        A command sent into a stall may be acted on much later or not at
        all, and for a toggle "later" is dangerous: by then it may no longer
        be wanted. So nothing is sent until the link is live.

        Raises:
            SpaUnavailableError: If the spa is not connected, or stays silent
                for the whole command timeout. Nothing has been sent.
        """
        client = self._require_client()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.command_timeout_s
        while not self._link_is_live(client):
            if loop.time() >= deadline:
                raise SpaUnavailableError("The spa has gone quiet; the command was not sent")
            await asyncio.sleep(self._settings.poll_interval_s)
            if self._client is not client or not client.connected:
                raise SpaUnavailableError("The spa is not connected")
        return client

    async def _wait_until(self, client: BalboaClient, condition: Callable[[], bool]) -> bool:
        """Wait for the spa's status to satisfy ``condition``.

        Polls rather than waiting for an event because fault-log replies do
        not raise one in the library.

        Only time during which the spa is talking counts towards "it did
        not happen". If the link stalls after the command was sent, the wait
        carries on through the stall, because the spa usually acts on the
        command when the stall ends.

        Before returning, :attr:`state` is brought up to date, so a caller
        that reads it straight after a command sees the command's outcome
        rather than the snapshot from before it.

        Returns:
            True as soon as the condition holds. False once the spa has been
            talking for ``confirm_timeout_s`` without it holding.

        Raises:
            SpaUnavailableError: If the connection is lost while waiting, or
                the spa stays silent until the command timeout. In both
                cases it is unknown whether the command will take effect.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.command_timeout_s
        live_s = 0.0
        while True:
            if condition():
                self._publish()
                return True
            if self._client is not client or not client.connected:
                raise SpaUnavailableError(
                    "The connection to the spa was lost before it confirmed the command"
                )
            if live_s >= self._settings.confirm_timeout_s:
                self._publish()
                return False
            if loop.time() >= deadline:
                raise SpaUnavailableError("The spa went quiet before it confirmed the command")
            await asyncio.sleep(self._settings.poll_interval_s)
            if self._link_is_live(client):
                live_s += self._settings.poll_interval_s

    async def _ensure_range_accepts(self, client: BalboaClient, whole_f: float) -> None:
        """Switch the spa's temperature range if ``whole_f`` needs the other one.

        The active range is kept whenever the value fits it, so the range is
        only ever switched when there is no alternative.
        """
        try:
            active = LowHighRange(int(client.temperature_range.state))
        except ValueError:
            raise SpaUnavailableError("The spa has not reported its temperature range") from None
        low, high = _RANGE_LIMITS_F[active]
        if low <= whole_f <= high:
            return
        needed = LowHighRange.HIGH if active is LowHighRange.LOW else LowHighRange.LOW
        await client.set_temperature_range(needed)
        if not await self._wait_until(client, lambda: client.temperature_range.state == needed):
            raise CommandRejectedError(
                f"The spa did not switch to its {needed.name.lower()} temperature range"
            )
        _LOGGER.info("Temperature range switched to %s", needed.name.lower())

    async def _switch(
        self, control: BalboaControl, target: IntEnum, client: BalboaClient, label: str
    ) -> None:
        """Send a pump control towards ``target`` and wait for the outcome."""
        if control.state == target:
            return
        if not await control.set_state(target):
            raise CommandRejectedError(f"{label} cannot be set to {target.name}")
        if await self._wait_until(client, lambda: control.state == target):
            _LOGGER.info("%s is now %s", label, target.name)
        else:
            _LOGGER.info(
                "%s was asked for %s but the spa reports %s",
                label,
                target.name,
                control.state.name,
            )

    # --- Connection management -----------------------------------------------

    async def _supervise(self) -> None:
        """Keep the connection alive until cancelled.

        Runs for the life of the adapter. Each pass either finds the
        connection healthy and sleeps, or discards it and builds a new one,
        waiting longer after each consecutive failure (2 s, 4 s, ... 60 s).
        """
        failures = 0
        while True:
            if self._is_healthy():
                failures = 0
                await asyncio.sleep(self._settings.health_check_interval_s)
                continue
            if self._client is not None:
                _LOGGER.warning("Lost contact with the spa; reconnecting")
            try:
                await self._close_client()
                await self._open_client()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                delay = min(
                    self._settings.reconnect_initial_delay_s * 2**failures,
                    self._settings.reconnect_max_delay_s,
                )
                failures += 1
                if isinstance(error, SpaUnavailableError):
                    _LOGGER.warning("%s; trying again in %.0f s", error, delay)
                else:
                    # A bug or an unforeseen library failure. Keep going: a
                    # dead supervisor would leave the tub unwatched for good.
                    _LOGGER.exception("Unexpected error while reconnecting to the spa")
                await asyncio.sleep(delay)

    def _is_healthy(self) -> bool:
        """Return whether the connection is open and the spa is talking.

        An open socket is not enough: if the Wi-Fi module loses power the
        socket can stay "open" indefinitely while nothing arrives.
        """
        client = self._client
        if client is None or not client.connected:
            return False
        heard = client.last_message_received
        if heard is None:
            return False
        silence = self._clock.now() - heard
        return silence <= timedelta(seconds=self._settings.silence_before_reconnect_s)

    async def _open_client(self) -> None:
        """Connect to the spa and start receiving its updates.

        Raises:
            SpaUnavailableError: If the spa could not be found or reached.
        """
        client = await self._connect_to_known_host()
        if client is None:
            client = await self._connect_to(await self._discover_host())
        self._client = client
        self._unsubscribe_from_client = client.on(EVENT_UPDATE, self._on_library_update)
        _LOGGER.info("Connected to the spa")
        self._publish()

    async def _connect_to_known_host(self) -> BalboaClient | None:
        """Try the configured address, or else the one that worked last time.

        Returns ``None`` when discovery should be tried instead. A failure of
        the *configured* address is raised: the operator pinned it, so
        quietly connecting elsewhere would be wrong.
        """
        if self._settings.host is not None:
            return await self._connect_to(self._settings.host)
        if self._last_good_host is None:
            return None
        try:
            return await self._connect_to(self._last_good_host)
        except SpaUnavailableError:
            _LOGGER.info(
                "The spa no longer answers at %s; searching the network", self._last_good_host
            )
            return None

    async def _discover_host(self) -> str:
        """Broadcast for spas and return the address of ours.

        Raises:
            SpaUnavailableError: If no spa, the wrong spa, or (with no MAC
                suffix configured) more than one spa answers.
        """
        try:
            found = await self._discover(self._settings.discovery_timeout_s)
        except OSError as error:
            raise SpaUnavailableError(
                f"Could not search the network for the spa: {error}"
            ) from error
        suffix = self._settings.mac_suffix
        if suffix is None:
            if len(found) == 1:
                return found[0].address
            raise SpaUnavailableError(
                "No spa answered the discovery broadcast"
                if not found
                else f"{len(found)} spas answered; configure a MAC suffix to choose one"
            )
        wanted = _normalise_mac(suffix)
        for spa in found:
            if _normalise_mac(spa.mac_address).endswith(wanted):
                return spa.address
        raise SpaUnavailableError(f"No spa with a MAC address ending in {suffix} was found")

    async def _connect_to(self, host: str) -> BalboaClient:
        """Connect to ``host`` and wait until the spa has described itself.

        Raises:
            SpaUnavailableError: If it cannot be reached, stays silent, or is
                not equipped the way this application expects.
        """
        client = self._client_factory(host)
        try:
            if not await client.connect():
                raise SpaUnavailableError(f"Could not connect to the spa at {host}")
            if not await client.async_configuration_loaded(self._settings.configuration_timeout_s):
                raise SpaUnavailableError(
                    f"The spa at {host} accepted the connection but did not send its configuration"
                )
            self._check_equipment(client, host)
        except BaseException:
            # Also on cancellation: never leave a half-open client behind
            # with its background tasks running.
            await client.disconnect()
            raise
        self._last_good_host = host
        return client

    def _check_equipment(self, client: BalboaClient, host: str) -> None:
        """Refuse a spa this application was not written for.

        Raises:
            SpaUnavailableError: If the unit or the pumps are not as recorded
                in ``docs/balboa-notes.md``.
        """
        if client.temperature_unit is not TemperatureUnit.FAHRENHEIT:
            raise SpaUnavailableError(
                f"The spa at {host} is set to Celsius; Fahrenheit is required"
            )
        pumps = client.pumps
        if len(pumps) < 2 or OffLowHighState.HIGH not in pumps[0].options:
            raise SpaUnavailableError(
                f"The spa at {host} does not have a two-speed pump 1 and a pump 2"
            )
        try:
            active = LowHighRange(int(client.temperature_range.state))
        except ValueError:
            return
        expected = _RANGE_LIMITS_F[active]
        actual = (client.temperature_minimum, client.temperature_maximum)
        if actual != expected:
            _LOGGER.warning(
                "The spa's %s range is %s °F, not the %s °F recorded in _RANGE_LIMITS_F; "
                "update the table",
                active.name.lower(),
                actual,
                expected,
            )

    async def _close_client(self) -> None:
        """Discard the current client, if any. The last known state is kept."""
        client, self._client = self._client, None
        if client is None:
            return
        # Essential, not tidiness: the library keeps listeners in one list
        # shared by all clients, so a forgotten one would keep being called.
        if self._unsubscribe_from_client is not None:
            self._unsubscribe_from_client()
            self._unsubscribe_from_client = None
        if client.last_message_received is not None:
            self._last_message_at = client.last_message_received
        await client.disconnect()

    # --- Publishing state ----------------------------------------------------

    def _on_library_update(self) -> None:
        """Note that something changed; publish once the message is complete.

        The library calls this from the middle of reading a status message,
        once per control that changed, when the remaining fields still hold
        their old values. Translating at that point would announce states the
        spa was never in. The work is therefore deferred to the event loop's
        next turn, by which time the whole message has been read, and several
        calls collapse into one.

        It also keeps exceptions out of the library's reader task, which
        would stop for good if one escaped from here.
        """
        if self._publish_scheduled:
            return
        self._publish_scheduled = True
        asyncio.get_running_loop().call_soon(self._publish)

    def _publish(self) -> None:
        """Record the spa's current state and tell listeners if it changed."""
        self._publish_scheduled = False
        client = self._client
        if client is None:
            return
        snapshot = _translate(client, self._clock.now())
        if snapshot is None:
            return
        previous, self._state = self._state, snapshot
        if previous is not None and snapshot.same_readings_as(previous):
            return
        event = SpaStateChanged(previous=previous, current=snapshot)
        # Iterate over a copy: a listener may unsubscribe while being called.
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                # One faulty listener must not silence the others.
                _LOGGER.exception("A spa state listener failed")

"""The interface between this application and a hot tub.

Pattern: Port (Cockburn, Ports and Adapters) and Gateway (Fowler, PoEAA).
:class:`SpaPort` states what the application needs from a spa in the
application's own vocabulary. Two adapters implement it:

* :class:`~hottub.spa.balboa.BalboaSpaAdapter` talks to the real tub over Wi-Fi.
* :class:`~hottub.spa.fake.FakeSpaAdapter` simulates a tub for development
  and tests.

Everything above this package depends on ``SpaPort`` only, so either adapter
can be plugged in at start-up (``hottub.app``).

Contract every adapter must honour
----------------------------------
* There is one connection to the spa, owned by the adapter. The Wi-Fi module
  is unreliable with several clients (PLAN.md, section 3).
* Commands raise :class:`~hottub.domain.SpaUnavailableError` when the spa
  cannot be reached and :class:`~hottub.domain.CommandRejectedError` when it
  refuses. They never fail silently.
* A pump command returning does **not** promise the pump ended up in the
  requested state. The spa has its own rules (it keeps pump 1 on low while
  heating, for example). Read :attr:`SpaPort.state` afterwards for what
  actually happened. Set temperature and heat mode are different: the spa has
  no reason to refuse those, so if it does not take them up the command
  raises.
* Listeners are called only when the readings change, not on every report.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from hottub.domain import (
    HeatMode,
    PumpSpeed,
    SpaFault,
    SpaState,
    SpaStateListener,
    Temperature,
    Unsubscribe,
)


class SpaPort(ABC):
    """Everything the application can ask of a spa."""

    # --- Connection ----------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Find the spa and open the connection.

        From this call until :meth:`disconnect`, the adapter looks after the
        connection and re-opens it when it is lost. That holds even when
        this first attempt fails: the error is raised so the caller knows,
        and the adapter keeps trying in the background.

        Raises:
            SpaUnavailableError: If the spa cannot be found or reached now.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection. Safe to call when already disconnected."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether the connection to the spa is currently open."""

    @property
    @abstractmethod
    def last_message_at(self) -> datetime | None:
        """When the spa was last heard from (UTC), or ``None`` if never.

        The watchdog uses this to detect a connection that is open but
        silent. It advances on every report, including ones that change
        nothing and therefore notify no listener.
        """

    # --- Reading -------------------------------------------------------------

    @property
    @abstractmethod
    def state(self) -> SpaState | None:
        """The most recent state reported by the spa.

        ``None`` until the first report arrives. While disconnected this
        keeps returning the last known state, so check
        :attr:`last_message_at` before trusting it. (``observed_at`` is not
        a freshness test: on a steady tub it can be minutes old while the
        spa is reporting the same readings several times a second.)

        After a command returns, this already reflects the command's outcome.
        """

    @abstractmethod
    def subscribe(self, listener: SpaStateListener) -> Unsubscribe:
        """Call ``listener`` whenever the spa's readings change.

        Returns:
            A function that removes the listener again.
        """

    @abstractmethod
    async def latest_fault(self) -> SpaFault | None:
        """Ask the spa for the newest entry in its fault log.

        Unlike state, the fault log is not pushed; each call is a request to
        the spa. Returns ``None`` if the log is empty.

        Raises:
            SpaUnavailableError: If the spa cannot be reached.
        """

    # --- Commands ------------------------------------------------------------

    @abstractmethod
    async def set_temperature(self, temperature: Temperature) -> None:
        """Set the temperature the spa should hold.

        The spa works in whole degrees Fahrenheit; fractions are rounded to
        the nearest degree. The adapter deals with the controller's low and
        high ranges, so any value from 50 to 104 °F is accepted here.

        Raises:
            CommandRejectedError: If the temperature is outside 50-104 °F, or
                the spa does not take it up.
            SpaUnavailableError: If the spa cannot be reached.
        """

    @abstractmethod
    async def set_pump1(self, speed: PumpSpeed) -> None:
        """Request a speed for pump 1.

        The spa may keep the pump on low when asked for off, because it needs
        the flow to heat or filter. It also switches the pump off by itself
        after a timeout.

        Raises:
            SpaUnavailableError: If the spa cannot be reached.
        """

    @abstractmethod
    async def set_pump2(self, on: bool) -> None:
        """Switch pump 2 on or off. The spa switches it off after a timeout.

        Raises:
            SpaUnavailableError: If the spa cannot be reached.
        """

    @abstractmethod
    async def set_heat_mode(self, mode: HeatMode) -> None:
        """Switch between Ready and Rest.

        Raises:
            CommandRejectedError: If the spa does not take up the new mode.
            SpaUnavailableError: If the spa cannot be reached.
        """

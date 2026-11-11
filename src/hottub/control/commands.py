"""The commands that can be sent to the spa.

Pattern: Command (GoF). Each request to change something on the tub is an
object. That gives :class:`~hottub.control.service.ControlService` one way
to run any of them: execute it, then write what happened to the audit log,
whichever command it was and whoever asked. A new kind of command is a new
class here and nothing else changes.

Commands are immutable and carry no reference to the spa; the spa is handed
to :meth:`SpaCommand.execute`. They are not queued: the spa adapter already
sends one command at a time over its single connection.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from hottub.domain import HeatMode, PumpSpeed, SpaState, Temperature
from hottub.spa import SpaPort

_NO_STATE = "the spa reported no state afterwards"


class SpaCommand(ABC):
    """A request to change one thing on the spa."""

    #: Machine-readable name, written to the audit log.
    name: ClassVar[str]

    @property
    @abstractmethod
    def argument(self) -> str:
        """What is being asked for, as text for the audit log."""

    @abstractmethod
    async def execute(self, spa: SpaPort) -> None:
        """Send the command.

        Raises:
            SpaUnavailableError: If the spa cannot be reached.
            CommandRejectedError: If the spa refuses.
        """

    @abstractmethod
    def outcome(self, state: SpaState | None) -> str:
        """Describe, for the audit log, what the spa did about the command.

        Args:
            state: The spa's state after the command returned.
        """


@dataclass(frozen=True)
class SetTemperature(SpaCommand):
    """Set the temperature the spa should hold."""

    name: ClassVar[str] = "set_temperature"
    temperature: Temperature

    @property
    def argument(self) -> str:
        """The requested temperature, e.g. ``"99 °F"``."""
        return str(self.temperature)

    async def execute(self, spa: SpaPort) -> None:
        """Send the set temperature to the spa."""
        await spa.set_temperature(self.temperature)

    def outcome(self, state: SpaState | None) -> str:
        """Report the set temperature the spa now shows."""
        if state is None:
            return _NO_STATE
        return f"set temperature is {state.set_temperature}"


@dataclass(frozen=True)
class SetPump1(SpaCommand):
    """Request a speed for pump 1."""

    name: ClassVar[str] = "set_pump1"
    speed: PumpSpeed

    @property
    def argument(self) -> str:
        """The requested speed: ``"off"``, ``"low"`` or ``"high"``."""
        return self.speed.value

    async def execute(self, spa: SpaPort) -> None:
        """Send the pump request to the spa."""
        await spa.set_pump1(self.speed)

    def outcome(self, state: SpaState | None) -> str:
        """Report the pump's actual speed, noting if the spa overruled us."""
        if state is None:
            return _NO_STATE
        if state.pump1 is not self.speed:
            # Normal while the spa is heating, filtering or measuring the
            # water: it keeps pump 1 on low whatever it is asked.
            return f"pump 1 is {state.pump1.value}; the spa overruled the request"
        return f"pump 1 is {state.pump1.value}"


@dataclass(frozen=True)
class SetPump2(SpaCommand):
    """Switch pump 2 on or off."""

    name: ClassVar[str] = "set_pump2"
    on: bool

    @property
    def argument(self) -> str:
        """``"on"`` or ``"off"``."""
        return "on" if self.on else "off"

    async def execute(self, spa: SpaPort) -> None:
        """Send the pump request to the spa."""
        await spa.set_pump2(self.on)

    def outcome(self, state: SpaState | None) -> str:
        """Report whether pump 2 is running, noting if that is not as asked."""
        if state is None:
            return _NO_STATE
        actual = "on" if state.pump2_on else "off"
        if state.pump2_on is not self.on:
            return f"pump 2 is {actual}; the spa did not follow the request"
        return f"pump 2 is {actual}"


@dataclass(frozen=True)
class SetHeatMode(SpaCommand):
    """Switch between Ready and Rest."""

    name: ClassVar[str] = "set_heat_mode"
    mode: HeatMode

    @property
    def argument(self) -> str:
        """``"ready"`` or ``"rest"``."""
        return self.mode.value

    async def execute(self, spa: SpaPort) -> None:
        """Send the heat mode to the spa."""
        await spa.set_heat_mode(self.mode)

    def outcome(self, state: SpaState | None) -> str:
        """Report the heat mode the spa now shows."""
        if state is None:
            return _NO_STATE
        return f"heat mode is {state.heat_mode.value}"

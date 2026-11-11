"""What the spa is doing at one instant, in this application's own terms.

These types are the only description of the spa the rest of the code sees.
The adapter in ``hottub.spa`` translates the Balboa library's types into them
(Anti-Corruption Layer, Evans), so a change of library touches one module.

Scope is deliberately limited to what the project uses: the blower and the
light exist on the tub but are not modelled (PLAN.md, section 8).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from hottub.domain.temperature import Temperature


class PumpSpeed(Enum):
    """Speed of pump 1, the two-speed pump that also circulates for heating."""

    OFF = "off"
    LOW = "low"
    HIGH = "high"


class HeatMode(Enum):
    """How eagerly the spa keeps the water at the set temperature.

    The controller also reports a transient "Ready-in-Rest" state. It cannot
    be commanded and is out of scope, so adapters report it as :attr:`REST`.
    """

    READY = "ready"
    """Heat whenever the water falls below the set temperature."""

    REST = "rest"
    """Heat only during filter cycles."""


class HeaterState(Enum):
    """What the heater element is doing."""

    OFF = "off"
    HEATING = "heating"
    WAITING = "waiting"
    """Heat is called for, but the spa is still confirming water flow."""


@dataclass(frozen=True)
class SpaState:
    """An immutable snapshot of the spa.

    Attributes:
        observed_at: When the spa reported this, timezone-aware UTC.
        water_temperature: Measured water temperature, or ``None`` when the
            spa has no reading. That is normal in Rest mode between filter
            cycles, because the sensor is only trusted in moving water.
        set_temperature: The temperature the spa is currently trying to hold.
        heat_mode: Ready or Rest.
        heater: Whether the heater is running.
        pump1: Actual speed of pump 1. May differ from the last request: the
            spa runs it on low by itself to heat and filter.
        pump2_on: Whether pump 2 (single speed) is running.
    """

    observed_at: datetime
    water_temperature: Temperature | None
    set_temperature: Temperature
    heat_mode: HeatMode
    heater: HeaterState
    pump1: PumpSpeed
    pump2_on: bool

    @property
    def is_heating(self) -> bool:
        """Return whether the heater element is actually producing heat."""
        return self.heater is HeaterState.HEATING

    def same_readings_as(self, other: SpaState) -> bool:
        """Return whether ``other`` differs only in when it was observed.

        Used to decide whether a new snapshot is worth announcing: the spa
        reports continuously, but most reports repeat the previous one.
        """
        return replace(self, observed_at=other.observed_at) == other


@dataclass(frozen=True)
class SpaFault:
    """An entry from the spa controller's fault log.

    The log is a history, not a live flag: an entry says a fault happened, and
    nothing ever says it stopped (``docs/balboa-notes.md``, "Faults").

    Attributes:
        code: The controller's numeric message code.
        message: The controller's description, e.g. ``"The water flow is low"``.
        occurred_at: When the controller logged it, timezone-aware UTC. Taken
            from the spa's own clock, so it can be a minute or two off.
    """

    code: int
    message: str
    occurred_at: datetime

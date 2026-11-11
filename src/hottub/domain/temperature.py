"""Temperature value objects.

Pattern: Value Object (Evans, *Domain-Driven Design*). Instances are immutable,
compared by value, and impossible to construct in an invalid state, so code
that receives one does not need to re-validate it.

Everything is in degrees Fahrenheit, the unit the spa is configured for
(``docs/balboa-notes.md``). The unit is in the attribute name on purpose:
``temperature.fahrenheit`` cannot be mistaken for Celsius at a call site.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hottub.domain.errors import InvalidTemperatureError

#: Lowest set temperature the controller accepts (bottom of its low range).
SPA_MINIMUM_F = 50.0

#: Highest set temperature the controller accepts (top of its high range).
#: This is also the regulatory safety limit for spa water.
SPA_MAXIMUM_F = 104.0


@dataclass(frozen=True, order=True)
class Temperature:
    """A water or set-point temperature in degrees Fahrenheit.

    Orderable, so ``water < ceiling`` reads naturally.

    Raises:
        InvalidTemperatureError: If the value is not a finite number.
    """

    fahrenheit: float

    def __post_init__(self) -> None:
        """Reject NaN and infinities, which would poison every comparison."""
        if not math.isfinite(self.fahrenheit):
            raise InvalidTemperatureError(f"Temperature must be finite, got {self.fahrenheit!r}")

    def degrees_above(self, other: Temperature) -> float:
        """Return how many degrees this is above ``other`` (negative if below)."""
        return self.fahrenheit - other.fahrenheit

    def to_whole_degree(self) -> Temperature:
        """Return this temperature rounded to a whole degree, halves upward.

        The spa works in whole degrees Fahrenheit. Halves round up (99.5
        becomes 100) rather than to the nearest even number as Python's
        ``round`` does, so the result never surprises someone reading a log.
        """
        return Temperature(float(math.floor(self.fahrenheit + 0.5)))

    def __str__(self) -> str:
        """Format for logs and messages, e.g. ``102 °F`` or ``99.5 °F``."""
        return f"{self.fahrenheit:g} °F"


@dataclass(frozen=True)
class TemperatureBand:
    """The floor and ceiling the tub should stay between.

    The ceiling is the temperature the tub is heated to when it should be
    ready for use; the floor is what it is allowed to fall to when idle
    (PLAN.md, section 1). The scheduler only ever commands one of these two.

    Raises:
        InvalidTemperatureError: If the floor is not below the ceiling, or
            either end is outside what the controller accepts.
    """

    floor: Temperature
    ceiling: Temperature

    def __post_init__(self) -> None:
        """Validate the band against itself and the controller's limits."""
        if not self.floor < self.ceiling:
            raise InvalidTemperatureError(
                f"Floor ({self.floor}) must be below ceiling ({self.ceiling})"
            )
        for label, value in (("Floor", self.floor), ("Ceiling", self.ceiling)):
            if not SPA_MINIMUM_F <= value.fahrenheit <= SPA_MAXIMUM_F:
                raise InvalidTemperatureError(
                    f"{label} ({value}) must be between {SPA_MINIMUM_F:g} and {SPA_MAXIMUM_F:g} °F"
                )

    def contains(self, temperature: Temperature) -> bool:
        """Return whether ``temperature`` lies within the band, ends included."""
        return self.floor <= temperature <= self.ceiling

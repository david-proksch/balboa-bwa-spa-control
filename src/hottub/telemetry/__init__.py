"""Telemetry: the record of what the tub did, and questions asked of it.

Two classes, one for each direction:

* :class:`TelemetryRecorder` watches the spa and writes history: when the
  heater and pumps ran, and the temperature once a minute.
* :class:`TelemetryService` reads that history back: running time over the
  last days, and how fast the heater warms the water.

The package sits beside ``hottub.control`` and knows nothing of it. It only
observes; it never sends the spa a command.
"""

from hottub.telemetry.recorder import TelemetryRecorder
from hottub.telemetry.service import RuntimeReport, RuntimeWindow, TelemetryService

__all__ = ["RuntimeReport", "RuntimeWindow", "TelemetryRecorder", "TelemetryService"]

"""Exceptions raised by the domain and by adapters on its behalf.

Adapters translate library-specific failures (for example ``pybalboa``'s
``SpaConnectionError``) into these, so callers never need to know which
library is underneath.
"""

from __future__ import annotations


class HotTubError(Exception):
    """Base class for every error this application raises deliberately."""


class InvalidTemperatureError(HotTubError, ValueError):
    """A temperature or temperature band is outside what the spa can do."""


class InvalidPlanError(HotTubError, ValueError):
    """A planned use cannot be honoured, for example because it is in the past."""


class SpaUnavailableError(HotTubError):
    """The spa cannot be reached, so the request was not carried out.

    Callers should treat this as temporary: the adapter keeps trying to
    reconnect, and the scheduler's next pass will retry on its own.
    """


class CommandRejectedError(HotTubError):
    """The spa was reachable but the command is one it will not accept."""

"""Domain events: facts about something that has happened.

Pattern: Observer (GoF) with Domain Events (Fowler/Evans). The spa adapter
announces these; telemetry, the watchdog and the live web page each react in
their own way without the adapter knowing they exist.

Events are immutable and named in the past tense. They carry everything a
listener needs, so listeners never have to call back into the publisher.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hottub.domain.spa_state import SpaState


@dataclass(frozen=True)
class SpaStateChanged:
    """The spa reported a state that differs from the one before.

    Attributes:
        previous: The state before the change, or ``None`` for the first
            state seen after connecting.
        current: The new state.
    """

    previous: SpaState | None
    current: SpaState


#: A callback that receives state changes. It runs on the event loop, so it
#: must return quickly and must not raise; schedule slow work as a task.
SpaStateListener = Callable[[SpaStateChanged], None]

#: Calling this removes the listener it was returned for. Safe to call twice.
Unsubscribe = Callable[[], None]

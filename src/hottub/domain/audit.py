"""The audit trail: a record of every command sent to the spa.

When the tub is unexpectedly cold, the first question is "who told it to do
that, and did it listen?" Every command, whether from a person or from the
scheduler, leaves a :class:`CommandRecord` that answers it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Actor(Enum):
    """Who issued a command."""

    USER = "user"
    """A person, through the web page or the API."""

    SCHEDULER = "scheduler"
    """The scheduler, carrying out a plan."""


@dataclass(frozen=True)
class CommandRecord:
    """One command sent to the spa, and what came of it.

    Attributes:
        issued_at: When the command was issued, timezone-aware UTC.
        actor: Who issued it.
        command: Machine-readable name, e.g. ``"set_pump1"``.
        argument: What was asked for, e.g. ``"high"`` or ``"99 °F"``.
        succeeded: False if the spa could not be reached or refused. True
            does not mean the spa ended up as asked: it may overrule a pump
            request, and ``result`` then says so.
        result: What the spa reported afterwards, or the error message.
    """

    issued_at: datetime
    actor: Actor
    command: str
    argument: str
    succeeded: bool
    result: str

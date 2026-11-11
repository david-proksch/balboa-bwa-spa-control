"""Plain-language descriptions of what the scheduler is doing and will do.

The sentences are for the owner, on the web page and in the API, so they
use the owner's wall clock and a 12-hour format ("2:40 PM"). They are kept
apart from the rules in ``hottub.domain`` so that rewording one never risks
changing a decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from hottub.domain import Decision, PlannedHeating, Reason, UpcomingChange

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def clock_time(instant: datetime, now: datetime, zone: tzinfo) -> str:
    """Format ``instant`` as the owner would say it at ``now``.

    Today's times are given bare (``"2:40 PM"``), tomorrow's as
    ``"tomorrow 9:00 AM"``, and any other day's with the weekday
    (``"Fri 3:00 PM"``).
    """
    local = instant.astimezone(zone)
    hour = local.hour % 12 or 12
    text = f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    today = now.astimezone(zone).date()
    if local.date() == today:
        return text
    if local.date() == today + timedelta(days=1):
        return f"tomorrow {text}"
    return f"{_WEEKDAYS[local.weekday()]} {text}"


def describe_current(decision: Decision, now: datetime, zone: tzinfo) -> str:
    """Say what the scheduler wants at this moment, and why."""
    target = decision.target
    if decision.reason is Reason.PLANNED_USE and decision.planned is not None:
        planned = decision.planned
        use = clock_time(planned.use_at, now, zone)
        end = clock_time(planned.ends_at, now, zone)
        if now < planned.use_at:
            text = f"Heating to {target} for the planned use at {use}."
        else:
            text = f"Holding {target} for the planned use until {end}."
        if decision.instead_of is Reason.WEEKEND_BLACKOUT:
            text += " This overrides the weekend's no-heating hours."
        return text
    if decision.reason is Reason.WEEKEND_READY:
        return f"Keeping the tub at {target} for the weekend."
    if decision.reason is Reason.WEEKEND_BLACKOUT:
        return f"Weekend no-heating hours: the tub may cool to {target}."
    return f"Idle: the tub may cool to {target}."


def describe_upcoming(change: UpcomingChange, now: datetime, zone: tzinfo) -> str:
    """Say what the scheduler will do next, and when."""
    decision = change.decision
    when = clock_time(change.at, now, zone)
    if decision.reason is Reason.PLANNED_USE and decision.planned is not None:
        planned = decision.planned
        ready = clock_time(planned.ready_at, now, zone)
        use = clock_time(planned.use_at, now, zone)
        return f"Heater will start at about {when} to be ready by {ready} for use at {use}."
    if decision.reason is Reason.WEEKEND_READY:
        return f"Heating to {decision.target} for the weekend starts at {when}."
    if decision.reason is Reason.WEEKEND_BLACKOUT:
        return f"Heating pauses at {when} for the weekend's no-heating hours."
    return f"The tub goes back to {decision.target} at {when}."


def describe_lateness(planned: PlannedHeating, now: datetime, zone: tzinfo) -> str:
    """Warn that the tub will not be warm by the time it was wanted."""
    expected = clock_time(planned.expected_ready_at, now, zone)
    ready = clock_time(planned.ready_at, now, zone)
    use = clock_time(planned.use_at, now, zone)
    return (
        f"The tub cannot be ready by {ready} for the planned use at {use}: "
        f"it is expected to be warm at about {expected}."
    )

"""Scheduling rules: what the spa's set temperature should be, and why.

The owner keeps two temperatures, a ceiling (ready for use) and a floor
(allowed to cool to). At any moment the spa should be set to one of them.
This module decides which, from the time, the stored plans and the water
temperature. It only *decides*; ``hottub.scheduler`` acts on the decision.

Patterns
--------
* **Strategy** (GoF): each scheduling rule is a :class:`HeatingPolicy`.
  :class:`Schedule` asks them in priority order and the first that has an
  opinion wins. A new rule is a new class; the others do not change.
* **Specification** (Evans; Fowler): :class:`BlackoutWindow` answers one
  yes/no question about an instant.

Everything here is a pure function of a :class:`Situation`. Nothing reads a
clock or a database, which is what lets the tests check a whole weekend, a
daylight-saving change included, in milliseconds.

Time
----
Instants are timezone-aware UTC, as everywhere in this code base. The rules
themselves are written in wall-clock terms ("Friday 15:00", "01:00 to
12:00"), so each policy converts to the owner's time zone at the point where
it applies such a rule, and converts back at once. Local datetimes are never
subtracted from one another: Python ignores daylight saving when both have
the same zone, which gives wrong answers twice a year.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import Enum

from hottub.domain.temperature import Temperature, TemperatureBand

#: How long before the use time the water should be at the ceiling.
DEFAULT_READY_LEAD = timedelta(hours=1)

#: Extra time allowed for heating, on top of the estimate. It covers an
#: estimate that is a little optimistic and the minute between two passes
#: of the scheduler.
DEFAULT_MARGIN = timedelta(minutes=15)

#: How long after the use time the tub is kept at the ceiling, until the
#: owner chooses otherwise.
DEFAULT_USE_DURATION = timedelta(hours=3)

#: The shortest and longest hold the owner may choose.
MIN_USE_DURATION = timedelta(minutes=30)
MAX_USE_DURATION = timedelta(hours=12)

#: How far below its set temperature a spa lets the water fall before it
#: heats again. Water this close to the ceiling counts as warm.
THERMOSTAT_SLACK_F = 1.0

#: Monday is 0 in :meth:`datetime.date.weekday`.
_FRIDAY, _SATURDAY, _SUNDAY = 4, 5, 6

_MIDNIGHT = time(0, 0)


def local_instant(day: date, at: time, zone: tzinfo) -> datetime:
    """Return the UTC instant of wall-clock time ``at`` on ``day`` in ``zone``.

    On the two awkward days of the year: a time the clocks skip in spring
    is taken as if they had not moved yet (02:30 becomes 03:30 summer
    time), and a time that happens twice in autumn means the first of the
    two.
    """
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)


@dataclass(frozen=True)
class BlackoutWindow:
    """A stretch of every day, in wall-clock time, with no heating.

    Pattern: Specification. The window runs from ``start`` up to but not
    including ``end``. It may cross midnight (22:00 to 06:00). A window
    whose start and end are equal is empty.
    """

    start: time
    end: time

    def contains(self, instant: datetime, zone: tzinfo) -> bool:
        """Return whether ``instant`` falls inside the window in ``zone``."""
        moment = instant.astimezone(zone).time()
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end


@dataclass(frozen=True)
class PlannedUse:
    """The owner's intention to use the tub at a certain time.

    There is at most one per calendar day.

    Attributes:
        day: The calendar day, in the owner's time zone.
        use_time: The wall-clock time the owner means to get in.
        created_at: When the plan was made, timezone-aware UTC.
        heating_started_at: When the scheduler began heating for this use,
            or ``None`` if it has not yet. Once set, heating continues to
            the end of the use: see :class:`PlannedUsePolicy` for why the
            start must not be recomputed after that.
    """

    day: date
    use_time: time
    created_at: datetime
    heating_started_at: datetime | None = None

    def use_at(self, zone: tzinfo) -> datetime:
        """Return the instant of the planned use (UTC)."""
        return local_instant(self.day, self.use_time, zone)


class Reason(Enum):
    """Why the scheduler wants the temperature it wants."""

    PLANNED_USE = "planned_use"
    """Heating for, or holding during, a planned use."""

    WEEKEND_READY = "weekend_ready"
    """Weekend keep-ready is on and it is the weekend."""

    WEEKEND_BLACKOUT = "weekend_blackout"
    """It is the weekend, but within the hours when no heating is allowed."""

    IDLE = "idle"
    """Nothing asks for a warm tub."""


@dataclass(frozen=True)
class PlannedHeating:
    """The timetable worked out for one planned use.

    All instants are UTC.

    Attributes:
        use: The plan this belongs to.
        use_at: When the owner means to get in.
        ready_at: When the water should be at the ceiling.
        heater_start: When heating begins: the recorded start if heating
            is under way, otherwise the current estimate, which moves
            earlier as the water cools.
        ends_at: When the tub stops being held at the ceiling.
        expected_ready_at: When the water is expected to reach the ceiling,
            judged from its temperature now.
    """

    use: PlannedUse
    use_at: datetime
    ready_at: datetime
    heater_start: datetime
    ends_at: datetime
    expected_ready_at: datetime

    @property
    def late(self) -> bool:
        """Whether the water is expected to reach the ceiling after ``ready_at``."""
        return self.expected_ready_at > self.ready_at

    def covers(self, instant: datetime) -> bool:
        """Return whether the tub is to be held at the ceiling at ``instant``."""
        return self.heater_start <= instant < self.ends_at


@dataclass(frozen=True)
class Situation:
    """Everything a policy may base its decision on.

    Attributes:
        now: The instant being decided for, timezone-aware UTC.
        zone: The owner's time zone, for the wall-clock rules.
        band: The floor and ceiling in force.
        water: The measured water temperature, or ``None`` when the spa has
            no reading (normal in Rest mode).
        heating_rate_f_per_hour: Degrees the heater gains per hour.
        planned_uses: The stored plans that may still matter.
        weekend_ready: Whether weekend keep-ready is switched on.
        use_duration: How long after a planned use time the ceiling is
            held. The owner can change it, which is why it is part of the
            situation and not fixed in the rule.
    """

    now: datetime
    zone: tzinfo
    band: TemperatureBand
    water: Temperature | None
    heating_rate_f_per_hour: float
    planned_uses: Sequence[PlannedUse] = ()
    weekend_ready: bool = False
    use_duration: timedelta = DEFAULT_USE_DURATION

    def at(self, instant: datetime) -> Situation:
        """Return the same situation at another instant.

        Used to look ahead. The water temperature is carried over as it is
        now, so the look-ahead is an estimate.
        """
        return replace(self, now=instant)


@dataclass(frozen=True)
class Decision:
    """What the spa should be set to at one instant.

    Attributes:
        target: The set temperature wanted: the ceiling or the floor.
        reason: Which rule decided.
        heating_wanted: Whether the tub is to be brought to, or kept at,
            the ceiling. When true the spa must be in Ready mode, because
            in Rest it only heats during filter cycles.
        planned: The timetable, when the reason is a planned use.
        instead_of: What the next rule in line would have decided had this
            one not applied. Lets the user interface say that a planned
            use is overriding the weekend's no-heating hours.
    """

    target: Temperature
    reason: Reason
    heating_wanted: bool
    planned: PlannedHeating | None = None
    instead_of: Reason | None = None


@dataclass(frozen=True)
class UpcomingChange:
    """The next moment the wanted set temperature changes.

    Attributes:
        at: When (UTC). An estimate when it is the start of heating for a
            planned use.
        decision: What applies from then on.
    """

    at: datetime
    decision: Decision


class HeatingPolicy(ABC):
    """One scheduling rule. Pattern: Strategy."""

    @abstractmethod
    def decide(self, situation: Situation) -> Decision | None:
        """Return this rule's decision, or ``None`` if it does not apply now."""

    @abstractmethod
    def turning_points(self, situation: Situation, until: datetime) -> Iterable[datetime]:
        """Return the instants up to ``until`` at which the decision may change.

        Listing an instant at which nothing turns out to change is
        harmless; :meth:`Schedule.next_change` checks each one. Missing one
        makes the forecast wrong, so when in doubt, include it.
        """


class PlannedUsePolicy(HeatingPolicy):
    """Heat in time for a planned use, and hold the ceiling until it is over.

    The water is to be at the ceiling ``ready_lead`` before the use time.
    Heating starts early enough for that::

        heater start = ready time - (ceiling - water) / heating rate - margin

    The start is worked out afresh on every pass while it lies in the
    future, so a tub that cools in the meantime starts earlier. When the
    spa reports no water temperature the floor is assumed, which is the
    coldest the tub should be and so gives the earliest start.

    Once heating has begun the start is fixed (``heating_started_at``).
    Recomputing it then would be wrong: as the water warms, the formula
    yields a later and later start, and the decision would flip back to
    "not yet" a minute after heating began.

    Args:
        ready_lead: How long before the use the water should be ready.
        margin: Extra heating time allowed on top of the estimate.

    How long the ceiling is held after the use time is the owner's choice
    and comes with the situation (``Situation.use_duration``).
    """

    def __init__(
        self,
        ready_lead: timedelta = DEFAULT_READY_LEAD,
        margin: timedelta = DEFAULT_MARGIN,
    ) -> None:
        """Set the rule's timings."""
        self._ready_lead = ready_lead
        self._margin = margin

    def heating_for(self, use: PlannedUse, situation: Situation) -> PlannedHeating:
        """Work out the timetable for ``use`` as things stand."""
        use_at = use.use_at(situation.zone)
        ready_at = use_at - self._ready_lead
        heat_up = heat_up_time(
            situation.water or situation.band.floor,
            situation.band.ceiling,
            situation.heating_rate_f_per_hour,
        )
        heater_start = use.heating_started_at or ready_at - heat_up - self._margin
        expected_ready_at = max(situation.now, heater_start) + heat_up
        if self._is_warm(situation):
            # Water that is warm is not late, however late the plan was
            # made. Without this, a tub being held at the ceiling would be
            # called late each time its thermostat let it drop a degree.
            expected_ready_at = min(expected_ready_at, ready_at)
        return PlannedHeating(
            use=use,
            use_at=use_at,
            ready_at=ready_at,
            heater_start=heater_start,
            ends_at=use_at + situation.use_duration,
            expected_ready_at=expected_ready_at,
        )

    @staticmethod
    def _is_warm(situation: Situation) -> bool:
        """Whether the water is at the ceiling, give or take the thermostat's slack."""
        if situation.water is None:
            return False
        return situation.water.fahrenheit >= situation.band.ceiling.fahrenheit - THERMOSTAT_SLACK_F

    def decide(self, situation: Situation) -> Decision | None:
        """Want the ceiling if any planned use is being heated for or under way."""
        for planned in self._timetables(situation):
            if planned.covers(situation.now):
                return Decision(
                    target=situation.band.ceiling,
                    reason=Reason.PLANNED_USE,
                    heating_wanted=True,
                    planned=planned,
                )
        return None

    def turning_points(self, situation: Situation, until: datetime) -> Iterable[datetime]:
        """Return each use's start of heating and end."""
        for planned in self._timetables(situation):
            yield planned.heater_start
            yield planned.ends_at

    def _timetables(self, situation: Situation) -> list[PlannedHeating]:
        timetables = [self.heating_for(use, situation) for use in situation.planned_uses]
        return sorted(timetables, key=lambda planned: planned.use_at)


class WeekendReadyPolicy(HeatingPolicy):
    """Keep the tub at the ceiling over the weekend, outside the blackout.

    The weekend runs from ``start_time`` on Friday to midnight at the end
    of Sunday, on the owner's wall clock. Within it, the ceiling is wanted
    except during the blackout, when the floor is: the heater then stays
    off unless the water falls all the way to the floor.

    The rule does not apply at all unless the owner has switched weekend
    keep-ready on.

    Args:
        start_time: When on Friday the weekend begins.
        blackout: The hours of each day with no heating.
    """

    def __init__(self, start_time: time, blackout: BlackoutWindow) -> None:
        """Set the rule's times."""
        self._start_time = start_time
        self._blackout = blackout

    @property
    def start_time(self) -> time:
        """When on Friday the weekend begins (wall clock)."""
        return self._start_time

    @property
    def blackout(self) -> BlackoutWindow:
        """The hours of each day with no heating."""
        return self._blackout

    def decide(self, situation: Situation) -> Decision | None:
        """Want the ceiling during the weekend and the floor during its blackout."""
        if not situation.weekend_ready or not self._is_weekend(situation):
            return None
        if self._blackout.contains(situation.now, situation.zone):
            return Decision(
                target=situation.band.floor, reason=Reason.WEEKEND_BLACKOUT, heating_wanted=False
            )
        return Decision(
            target=situation.band.ceiling, reason=Reason.WEEKEND_READY, heating_wanted=True
        )

    def turning_points(self, situation: Situation, until: datetime) -> Iterable[datetime]:
        """Return every instant at which a weekend or a blackout may begin or end.

        All four clock times are listed for every day, whatever the day of
        the week. That is more than needed and cannot miss one.
        """
        if not situation.weekend_ready:
            return
        day = situation.now.astimezone(situation.zone).date()
        last_day = until.astimezone(situation.zone).date()
        while day <= last_day:
            for moment in (_MIDNIGHT, self._start_time, self._blackout.start, self._blackout.end):
                yield local_instant(day, moment, situation.zone)
            day += timedelta(days=1)

    def _is_weekend(self, situation: Situation) -> bool:
        local = situation.now.astimezone(situation.zone)
        weekday = local.weekday()
        if weekday == _FRIDAY:
            return local.time() >= self._start_time
        return weekday in (_SATURDAY, _SUNDAY)


class IdlePolicy(HeatingPolicy):
    """Let the tub fall to the floor. Always applies, so it goes last."""

    def decide(self, situation: Situation) -> Decision:
        """Want the floor."""
        return Decision(target=situation.band.floor, reason=Reason.IDLE, heating_wanted=False)

    def turning_points(self, situation: Situation, until: datetime) -> Iterable[datetime]:
        """Return nothing: this rule never changes its mind."""
        return ()


class Schedule:
    """The scheduling rules in priority order.

    Args:
        policies: Highest priority first. :class:`IdlePolicy` is added at
            the end, so there is always a decision.
    """

    def __init__(self, policies: Sequence[HeatingPolicy]) -> None:
        """Fix the order of the rules."""
        self._policies: tuple[HeatingPolicy, ...] = (*policies, IdlePolicy())

    def decide(self, situation: Situation) -> Decision:
        """Return the decision of the first rule that applies."""
        applicable = (policy.decide(situation) for policy in self._policies)
        decisions = [decision for decision in applicable if decision is not None]
        winner = decisions[0]
        if len(decisions) > 1:
            return replace(winner, instead_of=decisions[1].reason)
        return winner

    def next_change(self, situation: Situation, horizon: timedelta) -> UpcomingChange | None:
        """Return the next time the wanted set temperature changes.

        A change of reason alone (a planned use beginning while the
        weekend already holds the ceiling) is not reported: nothing
        happens at the tub.

        Args:
            situation: Things as they stand.
            horizon: How far ahead to look.

        Returns:
            The first change within the horizon, or ``None`` if there is
            none.
        """
        until = situation.now + horizon
        current = self.decide(situation).target
        points = {
            point
            for policy in self._policies
            for point in policy.turning_points(situation, until)
            if situation.now < point <= until
        }
        for point in sorted(points):
            decision = self.decide(situation.at(point))
            if decision.target != current:
                return UpcomingChange(at=point, decision=decision)
        return None


def heat_up_time(water: Temperature, target: Temperature, rate_f_per_hour: float) -> timedelta:
    """Return how long the heater needs to bring ``water`` up to ``target``.

    Zero if the water is already there.

    Raises:
        ValueError: If the rate is not greater than zero.
    """
    if rate_f_per_hour <= 0:
        raise ValueError(f"The heating rate must be greater than zero, got {rate_f_per_hour!r}")
    degrees = max(0.0, target.degrees_above(water))
    return timedelta(hours=degrees / rate_f_per_hour)

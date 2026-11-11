"""The scheduler: keeps the spa's set temperature where the plans say it should be.

Patterns
--------
* **Service Layer** (Fowler, PoEAA): :class:`SchedulerService` is the entry
  point for everything about plans. The web handlers call it to store a
  plan or to ask what happens next; the application calls
  :meth:`SchedulerService.reconcile` once a minute.
* **Reconciliation loop**: the scheduler sets no timers. Each pass works
  out what the set temperature *should* be right now (the rules are in
  ``hottub.domain.schedule``), compares that with what the spa reports, and
  sends a command only if the two differ. A pass has no memory of the
  previous one, so a restart, a missed minute, a lost connection or someone
  turning the temperature up at the tub's own panel are all put right by
  the next pass, with no special handling.

What a pass does
----------------
1. Nothing, if the spa is not connected. (The watchdog reports that; a
   command would only fail and fill the audit log.)
2. Sets the spa's set temperature to the decided one, if it differs.
3. Sees to the heat mode. In Rest mode the spa heats only during filter
   cycles, so while heat is wanted the scheduler switches a spa that is in
   Rest to Ready, notes that it did so, and switches back to Rest once
   heat is no longer wanted. The note is stored, so the return to Rest
   survives a restart. A spa the owner left in Ready is never put in Rest.

Commands go through :class:`~hottub.control.ControlService` like anyone
else's, so they are validated against the band and written to the audit
log, marked as the scheduler's.

When a command fails the scheduler waits before trying again, a minute at
first and up to fifteen, so a spa that keeps refusing does not add a line
to the audit log every minute.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from hottub.control import ControlService, SpaStatus
from hottub.domain import (
    DEFAULT_USE_DURATION,
    MAX_USE_DURATION,
    MIN_USE_DURATION,
    Actor,
    BlackoutWindow,
    Clock,
    Decision,
    HeatingRate,
    HeatMode,
    HotTubError,
    InvalidPlanError,
    PlannedHeating,
    PlannedUse,
    PlannedUsePolicy,
    PlanRepository,
    Schedule,
    Situation,
    Temperature,
    UpcomingChange,
    WeekendReadyPolicy,
)
from hottub.scheduler import wording

_LOGGER = logging.getLogger(__name__)

#: How far ahead the forecast looks. Eight days always includes the start
#: of the next weekend.
LOOKAHEAD = timedelta(days=8)

#: How long a recorded water temperature stands in for one the spa does
#: not report at the moment. The water cools well under a degree in this
#: time, which the heating margin covers.
READING_TRUSTED_FOR = timedelta(hours=1)

#: The longest wait before a failed command is tried again.
_LONGEST_RETRY_DELAY = timedelta(minutes=15)

_SWITCHED_OFF = (
    "The scheduler is switched off in the configuration, so none of this is being carried out."
)
_NOT_CONNECTED = "The spa cannot be reached, so nothing can be carried out right now."


@dataclass(frozen=True)
class WeekendReady:
    """The weekend keep-ready switch and the times that go with it.

    Attributes:
        enabled: Whether the owner has switched it on.
        start_time: When on Friday the weekend begins (wall clock).
        blackout: The hours of each day with no heating.
    """

    enabled: bool
    start_time: time
    blackout: BlackoutWindow


@dataclass(frozen=True)
class Outlook:
    """What the scheduler wants now and what it will do next.

    Attributes:
        as_of: When this was worked out (UTC).
        current: The decision in force.
        current_text: The same, as a sentence for the owner.
        next_change: The next change of set temperature within
            :data:`LOOKAHEAD`, or ``None`` if there is none.
        next_text: The same, as a sentence; ``None`` if there is none.
        warnings: Things the owner should know: a planned use that cannot
            be met in time, an unreachable spa, a switched-off scheduler.
    """

    as_of: datetime
    current: Decision
    current_text: str
    next_change: UpcomingChange | None
    next_text: str | None
    warnings: tuple[str, ...]


class SchedulerService:
    """Stores the owner's plans and keeps the spa in line with them.

    Args:
        control: Used to read the spa and to send it commands.
        plans: Where the plans are stored.
        heating_rate: Returns how fast the heater warms the water; called
            on every pass, so a newly measured rate is picked up.
        clock: Source of the current time.
        zone: The owner's time zone, in which the plans are expressed.
        planned_use_policy: The rule for planned uses (highest priority).
        weekend_policy: The rule for weekend keep-ready.
        recent_water: Returns the last water temperature on record that is
            recent enough to trust (see :data:`READING_TRUSTED_FOR`), or
            ``None``. Consulted when the spa reports no reading, which it
            does for some seconds now and then. Without it a momentary
            gap would be taken for a tub at the floor, and heating for a
            planned use would start hours early.
        enabled: False to store plans and forecast, but never send a
            command. For running the application beside the tub without
            taking it over.
    """

    def __init__(
        self,
        control: ControlService,
        plans: PlanRepository,
        heating_rate: Callable[[], HeatingRate],
        clock: Clock,
        zone: tzinfo,
        planned_use_policy: PlannedUsePolicy,
        weekend_policy: WeekendReadyPolicy,
        *,
        recent_water: Callable[[], Temperature | None] | None = None,
        enabled: bool = True,
    ) -> None:
        """Wire the service to its collaborators; does no I/O."""
        self._control = control
        self._plans = plans
        self._heating_rate = heating_rate
        self._clock = clock
        self._zone = zone
        self._planned_use_policy = planned_use_policy
        self._weekend_policy = weekend_policy
        self._recent_water = recent_water or (lambda: None)
        # The order is the priority: a planned use beats the weekend rule,
        # which is how it overrides the blackout.
        self._schedule = Schedule([planned_use_policy, weekend_policy])
        self._enabled = enabled

        # One pass at a time: the timer and a web request may both ask.
        self._lock = asyncio.Lock()
        self._failures = 0
        self._retry_not_before: datetime | None = None
        self._last_logged: Decision | None = None

    # --- Plans ---------------------------------------------------------------

    @property
    def zone(self) -> tzinfo:
        """The owner's time zone, for showing times as the owner reads them."""
        return self._zone

    def today(self) -> date:
        """Return today's date on the owner's calendar."""
        return self._clock.now().astimezone(self._zone).date()

    def planned_heating(self, day: date) -> PlannedHeating | None:
        """Return the timetable for the use planned on ``day``, if there is one."""
        use = self._plans.planned_use(day)
        if use is None:
            return None
        return self._planned_use_policy.heating_for(use, self._situation())

    async def plan_use(self, day: date, use_time: time) -> PlannedHeating:
        """Plan to use the tub on ``day`` at ``use_time``, and act on it at once.

        Replaces any earlier plan for that day. A time that cannot be met
        is accepted, since a tub that is warm late is better than a cold
        one; the returned timetable has ``late`` set.

        Args:
            day: The calendar day, on the owner's calendar.
            use_time: The wall-clock time the owner means to get in.

        Raises:
            InvalidPlanError: If the use would already be over.
        """
        situation = self._situation()
        use = PlannedUse(day=day, use_time=use_time, created_at=situation.now)
        if self._planned_use_policy.heating_for(use, situation).ends_at <= situation.now:
            raise InvalidPlanError(
                f"A use at {use_time:%H:%M} on {day:%Y-%m-%d} would already be over"
            )
        self._plans.save_planned_use(use)
        _LOGGER.info("Use planned for %s at %s", day, use_time)
        await self._act_now()
        # Read back: the pass above may have started heating for it.
        planned = self.planned_heating(day)
        assert planned is not None
        return planned

    async def cancel_use(self, day: date) -> None:
        """Drop the plan for ``day``, if there is one, and act on that at once."""
        self._plans.delete_planned_use(day)
        _LOGGER.info("Plan for %s cancelled", day)
        await self._act_now()

    def weekend_ready(self) -> WeekendReady:
        """Return the weekend keep-ready switch and its times."""
        return WeekendReady(
            enabled=self._plans.weekend_ready(),
            start_time=self._weekend_policy.start_time,
            blackout=self._weekend_policy.blackout,
        )

    async def set_weekend_ready(self, enabled: bool) -> WeekendReady:
        """Switch weekend keep-ready on or off, and act on that at once."""
        self._plans.set_weekend_ready(enabled)
        _LOGGER.info("Weekend keep-ready switched %s", "on" if enabled else "off")
        await self._act_now()
        return self.weekend_ready()

    def hold_time(self) -> timedelta:
        """Return how long after a planned use time the tub is kept at the ceiling."""
        return self._plans.use_duration() or DEFAULT_USE_DURATION

    async def set_hold_time(self, duration: timedelta) -> timedelta:
        """Choose how long a planned use holds the ceiling, and act on that at once.

        Applies to the plans already made as well as to new ones.

        Raises:
            InvalidPlanError: If ``duration`` is outside the allowed range.
        """
        if not MIN_USE_DURATION <= duration <= MAX_USE_DURATION:
            raise InvalidPlanError(
                f"The hold time must be between {MIN_USE_DURATION.total_seconds() / 60:g} "
                f"minutes and {MAX_USE_DURATION.total_seconds() / 3600:g} hours"
            )
        self._plans.set_use_duration(duration)
        _LOGGER.info("Hold time after a planned use set to %s", duration)
        await self._act_now()
        return self.hold_time()

    def forget_plans_before(self, day: date) -> None:
        """Delete the plans for days earlier than ``day``."""
        self._plans.delete_planned_uses_before(day)

    # --- Forecast ------------------------------------------------------------

    def outlook(self) -> Outlook:
        """Say what the scheduler wants now and what it will do next."""
        status = self._control.status()
        situation = self._situation(status)
        now, zone = situation.now, self._zone
        current = self._schedule.decide(situation)
        upcoming = self._schedule.next_change(situation, LOOKAHEAD)

        warnings: list[str] = []
        if not self._enabled:
            warnings.append(_SWITCHED_OFF)
        elif not status.connected:
            warnings.append(_NOT_CONNECTED)
        if current.planned is not None and current.planned.late:
            warnings.append(wording.describe_lateness(current.planned, now, zone))

        return Outlook(
            as_of=now,
            current=current,
            current_text=wording.describe_current(current, now, zone),
            next_change=upcoming,
            next_text=None if upcoming is None else wording.describe_upcoming(upcoming, now, zone),
            warnings=tuple(warnings),
        )

    # --- Acting --------------------------------------------------------------

    async def reconcile(self) -> None:
        """Make one pass: bring the spa in line with the plans.

        Never raises for a spa that is unreachable or refuses; that is
        logged and retried on a later pass.
        """
        if not self._enabled:
            return
        async with self._lock:
            status = self._control.status()
            if status.state is None or not status.connected:
                _LOGGER.debug("The spa is not connected; nothing to reconcile")
                return
            situation = self._situation(status)
            if self._retry_not_before is not None and situation.now < self._retry_not_before:
                return

            decision = self._schedule.decide(situation)
            self._log_if_changed(decision)
            if decision.planned is not None:
                # Fix the start, so warming water cannot move it into the
                # future again (see PlannedUsePolicy).
                self._plans.mark_heating_started(decision.planned.use.day, situation.now)
            try:
                await self._hold_temperature(decision, status)
                await self._hold_heat_mode(decision, status)
            except HotTubError as error:
                self._failures += 1
                delay = min(timedelta(minutes=2 ** (self._failures - 1)), _LONGEST_RETRY_DELAY)
                self._retry_not_before = situation.now + delay
                _LOGGER.warning(
                    "The scheduler could not set the spa (%s); trying again in %d min",
                    error,
                    delay.total_seconds() // 60,
                )
            else:
                self._forget_failures()

    async def _act_now(self) -> None:
        """Make a pass at once, because the owner has just changed a plan."""
        self._forget_failures()
        await self.reconcile()

    def _forget_failures(self) -> None:
        self._failures = 0
        self._retry_not_before = None

    async def _hold_temperature(self, decision: Decision, status: SpaStatus) -> None:
        assert status.state is not None
        target = decision.target.to_whole_degree()
        if status.state.set_temperature != target:
            await self._control.set_target(decision.target, Actor.SCHEDULER)

    async def _hold_heat_mode(self, decision: Decision, status: SpaStatus) -> None:
        assert status.state is not None
        mode = status.state.heat_mode
        if decision.heating_wanted:
            if mode is HeatMode.REST:
                # Noted before the command is sent: if we stop in between,
                # the worst case is a note about a switch that never
                # happened, which is cleared harmlessly below.
                self._plans.set_rest_to_restore(True)
                await self._control.set_heat_mode(HeatMode.READY, Actor.SCHEDULER)
        elif self._plans.rest_to_restore():
            if mode is HeatMode.READY:
                await self._control.set_heat_mode(HeatMode.REST, Actor.SCHEDULER)
            self._plans.set_rest_to_restore(False)

    # --- Internals -----------------------------------------------------------

    def _situation(self, status: SpaStatus | None = None) -> Situation:
        """Gather what the rules decide on."""
        status = status or self._control.status()
        now = self._clock.now()
        today = now.astimezone(self._zone).date()
        return Situation(
            now=now,
            zone=self._zone,
            band=status.band,
            water=self._water(status),
            heating_rate_f_per_hour=self._heating_rate().f_per_hour,
            # Yesterday's use may run past midnight.
            planned_uses=self._plans.planned_uses_between(
                today - timedelta(days=1), today + LOOKAHEAD
            ),
            weekend_ready=self._plans.weekend_ready(),
            use_duration=self.hold_time(),
        )

    def _water(self, status: SpaStatus) -> Temperature | None:
        """Return the water temperature to plan with, or ``None`` if unknown.

        The spa's own reading if it has one, else the last one recorded
        recently. Only when there is neither do the rules fall back on
        assuming the floor.
        """
        if status.state is not None and status.state.water_temperature is not None:
            return status.state.water_temperature
        return self._recent_water()

    def _log_if_changed(self, decision: Decision) -> None:
        last = self._last_logged
        if last is None or (last.target, last.reason) != (decision.target, decision.reason):
            _LOGGER.info("The scheduler now wants %s (%s)", decision.target, decision.reason.value)
        self._last_logged = decision

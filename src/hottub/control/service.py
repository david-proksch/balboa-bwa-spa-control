"""The control use cases: what a person or the scheduler can ask of the tub.

Pattern: Service Layer (Fowler, PoEAA). :class:`ControlService` is the one
entry point for changing anything on the spa. The web handlers and, later,
the scheduler call it; neither talks to the spa directly. That is what
guarantees every command is validated and written to the audit log.

Ceiling, floor and the spa's set temperature
--------------------------------------------
The owner sets two temperatures, a *ceiling* (ready for use) and a *floor*
(allowed to fall to when idle). The spa itself has one set temperature. Which
of the two it should be at any moment is the scheduler's decision
(PLAN.md, section 4). This service only keeps the spa consistent with the
band when the band changes:

* if the spa was set to the edge that moved, it follows the edge;
* if the spa's set temperature now lies outside the band, it is pulled back
  to the nearest edge;
* otherwise the spa is left alone.

Changing the band succeeds even when the spa cannot be told at that moment.
The band is the owner's wish and is stored; the failed command is in the
audit log, and the scheduler's next pass puts the spa right.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from hottub.control.commands import SetHeatMode, SetPump1, SetPump2, SetTemperature, SpaCommand
from hottub.domain import (
    Actor,
    Clock,
    CommandLog,
    CommandRecord,
    HeatMode,
    HotTubError,
    InvalidTemperatureError,
    PumpSpeed,
    SettingsRepository,
    SpaState,
    Temperature,
    TemperatureBand,
)
from hottub.spa import SpaPort

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpaStatus:
    """Everything the status page shows, gathered at one instant.

    Attributes:
        state: The spa's last reported state, or ``None`` if it has never
            reported. It is the *last known* state: check ``connected`` and
            ``seconds_since_last_message`` before trusting it.
        band: The floor and ceiling in force.
        connected: Whether the connection to the spa is open.
        last_message_at: When the spa was last heard from (UTC), if ever.
        seconds_since_last_message: The same, as an age. The Wi-Fi module
            routinely goes quiet for about 20 seconds, so only a larger
            value means trouble (``docs/balboa-notes.md``, "Stalls").
    """

    state: SpaState | None
    band: TemperatureBand
    connected: bool
    last_message_at: datetime | None
    seconds_since_last_message: float | None


class ControlService:
    """Carries out requests to read or change the spa.

    Args:
        spa: The spa.
        settings: Where the floor and ceiling are stored.
        command_log: The audit log.
        clock: Source of the current time.
        default_band: The floor and ceiling used until the owner sets them.
    """

    def __init__(
        self,
        spa: SpaPort,
        settings: SettingsRepository,
        command_log: CommandLog,
        clock: Clock,
        default_band: TemperatureBand,
    ) -> None:
        """Wire the service to its collaborators; does no I/O."""
        self._spa = spa
        self._settings = settings
        self._command_log = command_log
        self._clock = clock
        self._default_band = default_band

    # --- Reading -------------------------------------------------------------

    @property
    def band(self) -> TemperatureBand:
        """The floor and ceiling in force."""
        return self._settings.load_band() or self._default_band

    def status(self) -> SpaStatus:
        """Return the spa's last known state and how fresh it is."""
        heard = self._spa.last_message_at
        age = None if heard is None else (self._clock.now() - heard).total_seconds()
        return SpaStatus(
            state=self._spa.state,
            band=self.band,
            connected=self._spa.connected,
            last_message_at=heard,
            seconds_since_last_message=age,
        )

    def recent_commands(self, limit: int = 50) -> Sequence[CommandRecord]:
        """Return the newest entries of the audit log, newest first."""
        return self._command_log.recent(limit)

    # --- Ceiling and floor ---------------------------------------------------

    async def set_ceiling(self, ceiling: Temperature, actor: Actor) -> SpaStatus:
        """Change the ceiling, and move the spa's set temperature if needed.

        Fractions are rounded to a whole degree, the spa's resolution.

        Raises:
            InvalidTemperatureError: If the ceiling is not above the floor or
                is outside 50-104 °F. Nothing is changed.
        """
        old = self.band
        return await self._change_band(
            old, TemperatureBand(old.floor, ceiling.to_whole_degree()), actor
        )

    async def set_floor(self, floor: Temperature, actor: Actor) -> SpaStatus:
        """Change the floor, and move the spa's set temperature if needed.

        Fractions are rounded to a whole degree, the spa's resolution.

        Raises:
            InvalidTemperatureError: If the floor is not below the ceiling or
                is outside 50-104 °F. Nothing is changed.
        """
        old = self.band
        return await self._change_band(
            old, TemperatureBand(floor.to_whole_degree(), old.ceiling), actor
        )

    async def _change_band(
        self, old: TemperatureBand, new: TemperatureBand, actor: Actor
    ) -> SpaStatus:
        self._settings.save_band(new)
        _LOGGER.info(
            "Band changed by %s: floor %s, ceiling %s", actor.value, new.floor, new.ceiling
        )
        target = self._set_temperature_after_band_change(old, new)
        if target is not None:
            try:
                await self._run(SetTemperature(target), actor)
            except HotTubError as error:
                # The band is saved and the failure is in the audit log; see
                # the module docstring for why this is not passed on.
                _LOGGER.warning("The band was saved but the spa was not updated: %s", error)
        return self.status()

    def _set_temperature_after_band_change(
        self, old: TemperatureBand, new: TemperatureBand
    ) -> Temperature | None:
        """Return the set temperature the spa should move to, if it should move."""
        state = self._spa.state
        if state is None or not self._spa.connected:
            _LOGGER.warning("The spa is not connected; its set temperature was left alone")
            return None
        current = state.set_temperature
        if current == old.ceiling:
            target = new.ceiling
        elif current == old.floor:
            target = new.floor
        elif current > new.ceiling:
            target = new.ceiling
        elif current < new.floor:
            target = new.floor
        else:
            return None
        return None if target == current else target

    async def set_target(self, temperature: Temperature, actor: Actor) -> SpaStatus:
        """Set the spa's set temperature to a value within the band.

        This is how the scheduler moves the tub between floor and ceiling.
        The value is rounded to a whole degree, the spa's resolution.

        Raises:
            InvalidTemperatureError: If the temperature is outside the
                band. Nothing is sent: the software never commands a set
                temperature the owner has not allowed.
            SpaUnavailableError: If the spa cannot be reached.
            CommandRejectedError: If the spa refuses.
        """
        band = self.band
        if not band.contains(temperature):
            raise InvalidTemperatureError(
                f"{temperature} is outside the band of {band.floor} to {band.ceiling}"
            )
        await self._run(SetTemperature(temperature.to_whole_degree()), actor)
        return self.status()

    # --- Pumps and heat mode -------------------------------------------------

    async def set_pump1(self, speed: PumpSpeed, actor: Actor) -> SpaStatus:
        """Request a speed for pump 1.

        The spa may overrule the request, typically by keeping the pump on
        low while it heats. That is not an error: the returned status shows
        the actual speed.

        Raises:
            SpaUnavailableError: If the spa cannot be reached.
        """
        await self._run(SetPump1(speed), actor)
        return self.status()

    async def set_pump2(self, on: bool, actor: Actor) -> SpaStatus:
        """Switch pump 2 on or off. The returned status shows the outcome.

        Raises:
            SpaUnavailableError: If the spa cannot be reached.
        """
        await self._run(SetPump2(on), actor)
        return self.status()

    async def set_heat_mode(self, mode: HeatMode, actor: Actor) -> SpaStatus:
        """Switch between Ready and Rest.

        Raises:
            CommandRejectedError: If the spa does not take up the mode.
            SpaUnavailableError: If the spa cannot be reached.
        """
        await self._run(SetHeatMode(mode), actor)
        return self.status()

    # --- Internals -----------------------------------------------------------

    async def _run(self, command: SpaCommand, actor: Actor) -> None:
        """Execute ``command`` and write the outcome to the audit log.

        The record is written whether the command succeeds or fails, and a
        failure is then passed on to the caller unchanged.
        """
        issued_at = self._clock.now()

        def record(succeeded: bool, result: str) -> None:
            self._command_log.append(
                CommandRecord(
                    issued_at=issued_at,
                    actor=actor,
                    command=command.name,
                    argument=command.argument,
                    succeeded=succeeded,
                    result=result,
                )
            )

        try:
            await command.execute(self._spa)
        except HotTubError as error:
            record(False, str(error))
            raise
        record(True, command.outcome(self._spa.state))

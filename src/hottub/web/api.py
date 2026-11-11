"""The JSON API, version 1.

Pattern: thin handlers over a Service Layer (Fowler, PoEAA). Each handler
converts the request into domain values, calls one method of
:class:`~hottub.control.ControlService` (to act on the tub),
:class:`~hottub.telemetry.TelemetryService` (to read its history)
:class:`~hottub.scheduler.SchedulerService` (plans) or
:class:`~hottub.watchdog.WatchdogService` (alerts) and converts the
answer back. No rule about the tub lives here.

Everything the API does is done on behalf of a person, so commands are
logged with :attr:`~hottub.domain.Actor.USER`.

Errors are turned into HTTP statuses in ``hottub.web.server``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response

from hottub.control import ControlService
from hottub.domain import Actor, NotificationError, Temperature
from hottub.scheduler import SchedulerService
from hottub.telemetry import TelemetryService
from hottub.watchdog import WatchdogService
from hottub.web.schemas import (
    AlertResponse,
    AlertsResponse,
    CommandResponse,
    ErrorResponse,
    HealthResponse,
    HeatingRateResponse,
    HeatModeRequest,
    HoldTime,
    NextActionResponse,
    PlanRequest,
    PlanResponse,
    Pump1Request,
    Pump2Request,
    RuntimeResponse,
    StatusResponse,
    TemperatureRequest,
    WeekendRequest,
    WeekendResponse,
)

#: Error responses, declared so they appear in the generated API reference.
_Responses = dict[int | str, dict[str, Any]]
_UNAVAILABLE: _Responses = {
    503: {"model": ErrorResponse, "description": "The spa cannot be reached."}
}
_REJECTED: _Responses = {
    409: {"model": ErrorResponse, "description": "The spa refused the command."}
}
_INVALID: _Responses = {
    422: {"model": ErrorResponse, "description": "The value is not acceptable."}
}
_NO_ALERT: _Responses = {
    404: {"model": ErrorResponse, "description": "There is no such alert."},
    409: {"model": ErrorResponse, "description": "The alert is over, or clears by itself."},
}
_NOT_SENT: _Responses = {
    409: {"model": ErrorResponse, "description": "No Pushover keys are configured."},
    502: {"model": ErrorResponse, "description": "Pushover could not be reached or refused."},
}
_NO_PLAN: _Responses = {404: {"model": ErrorResponse, "description": "Nothing is planned."}}

#: The days a use can be planned for. Tomorrow is offered so that an early
#: soak can be planned the evening before.
PlanDay = Literal["today", "tomorrow"]


#: The longest window ``GET /runtime`` accepts: as far back as history is kept.
MAX_RUNTIME_DAYS = 90

#: The longest history ``GET /alerts`` accepts, for the same reason.
MAX_ALERT_DAYS = 90


def create_api_router(
    control: ControlService,
    telemetry: TelemetryService,
    scheduler: SchedulerService,
    watchdog: WatchdogService,
) -> APIRouter:
    """Build the version 1 routes around the four services.

    The services are captured by the handlers rather than looked up from a
    global, so tests can build an API around a simulated spa.
    """
    router = APIRouter()

    def calendar_day(day: PlanDay) -> date:
        return scheduler.today() + timedelta(days=1 if day == "tomorrow" else 0)

    @router.get("/status", tags=["status"])
    async def get_status() -> StatusResponse:
        """Return the spa's last known state, the band, and connection health."""
        return StatusResponse.from_status(control.status())

    @router.put("/temperature/ceiling", tags=["temperature"], responses=_INVALID)
    async def put_ceiling(body: TemperatureRequest) -> StatusResponse:
        """Set the ceiling: the temperature the tub is heated to for use.

        The setting is saved even if the spa cannot be reached; check
        ``target_temperature_f`` in the response for what the spa is doing.
        """
        status = await control.set_ceiling(Temperature(body.fahrenheit), Actor.USER)
        return StatusResponse.from_status(status)

    @router.put("/temperature/floor", tags=["temperature"], responses=_INVALID)
    async def put_floor(body: TemperatureRequest) -> StatusResponse:
        """Set the floor: the temperature the tub may fall to when idle.

        The setting is saved even if the spa cannot be reached; check
        ``target_temperature_f`` in the response for what the spa is doing.
        """
        status = await control.set_floor(Temperature(body.fahrenheit), Actor.USER)
        return StatusResponse.from_status(status)

    @router.put("/pumps/1", tags=["pumps"], responses=_UNAVAILABLE)
    async def put_pump1(body: Pump1Request) -> StatusResponse:
        """Request off, low or high for pump 1.

        The spa keeps the pump on low while it heats, filters or measures
        the water. The response shows the actual speed, which may therefore
        differ from the request.
        """
        return StatusResponse.from_status(await control.set_pump1(body.speed, Actor.USER))

    @router.put("/pumps/2", tags=["pumps"], responses=_UNAVAILABLE)
    async def put_pump2(body: Pump2Request) -> StatusResponse:
        """Switch pump 2 on or off. The response shows the actual state."""
        return StatusResponse.from_status(await control.set_pump2(body.state == "on", Actor.USER))

    @router.put("/heat-mode", tags=["heat mode"], responses=_UNAVAILABLE | _REJECTED)
    async def put_heat_mode(body: HeatModeRequest) -> StatusResponse:
        """Switch between Ready and Rest."""
        return StatusResponse.from_status(await control.set_heat_mode(body.mode, Actor.USER))

    @router.get("/commands", tags=["status"])
    async def get_commands(
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[CommandResponse]:
        """Return the audit log of commands sent to the spa, newest first."""
        return [CommandResponse.from_record(record) for record in control.recent_commands(limit)]

    @router.get("/runtime", tags=["history"], responses=_INVALID)
    async def get_runtime(
        days: Annotated[
            str,
            Query(
                description="Window lengths in days, separated by commas.",
                pattern=r"^\d{1,3}(,\d{1,3}){0,9}$",
            ),
        ] = "1,7,14,30",
    ) -> RuntimeResponse:
        """Return how long the heater and pumps ran over the last days.

        A window of N days is the N times 24 hours up to now. The figures
        for a device that is running now can be up to a minute behind.
        """
        wanted = [int(length) for length in days.split(",")]
        if not all(1 <= length <= MAX_RUNTIME_DAYS for length in wanted):
            raise HTTPException(
                status_code=422, detail=f"Each window must be 1 to {MAX_RUNTIME_DAYS} days"
            )
        return RuntimeResponse.from_report(telemetry.runtime(wanted))

    @router.get("/heating-rate", tags=["history"])
    async def get_heating_rate() -> HeatingRateResponse:
        """Return how fast the heater warms the water.

        Measured from the last 30 days of heating once there is enough of
        it; until then, the configured assumption.
        """
        return HeatingRateResponse.from_rate(telemetry.heating_rate())

    # The fixed paths are declared before "/plan/{day}" so that they
    # are not taken for a day.
    @router.get("/plan/weekend", tags=["plans"])
    async def get_weekend() -> WeekendResponse:
        """Return whether weekend keep-ready is on, and its times."""
        return WeekendResponse.from_weekend(scheduler.weekend_ready())

    @router.put("/plan/weekend", tags=["plans"])
    async def put_weekend(body: WeekendRequest) -> WeekendResponse:
        """Switch weekend keep-ready on or off. Takes effect at once."""
        return WeekendResponse.from_weekend(await scheduler.set_weekend_ready(body.enabled))

    @router.get("/plan/hold", tags=["plans"])
    async def get_hold() -> HoldTime:
        """Return how long after a planned use time the tub is kept at the ceiling."""
        return HoldTime(minutes=round(scheduler.hold_time().total_seconds() / 60))

    @router.put("/plan/hold", tags=["plans"], responses=_INVALID)
    async def put_hold(body: HoldTime) -> HoldTime:
        """Choose the hold time, from 30 minutes to 12 hours. Takes effect at once."""
        held = await scheduler.set_hold_time(timedelta(minutes=body.minutes))
        return HoldTime(minutes=round(held.total_seconds() / 60))

    @router.get("/plan/next-action", tags=["plans"])
    async def get_next_action() -> NextActionResponse:
        """Return what the scheduler wants now and what it will do next."""
        return NextActionResponse.from_outlook(scheduler.outlook())

    @router.get("/plan/{day}", tags=["plans"], responses=_NO_PLAN)
    async def get_plan(day: PlanDay) -> PlanResponse:
        """Return the use planned for today or tomorrow, with its timetable."""
        planned = scheduler.planned_heating(calendar_day(day))
        if planned is None:
            raise HTTPException(status_code=404, detail=f"Nothing is planned for {day}")
        return PlanResponse.from_planned(planned)

    @router.put("/plan/{day}", tags=["plans"], responses=_INVALID)
    async def put_plan(day: PlanDay, body: PlanRequest) -> PlanResponse:
        """Plan to use the tub today or tomorrow at the given time.

        Replaces any earlier plan for that day and takes effect at once.
        A time too soon to be met is accepted; `late` is then true.
        """
        return PlanResponse.from_planned(await scheduler.plan_use(calendar_day(day), body.use_time))

    @router.delete("/plan/{day}", tags=["plans"], status_code=204)
    async def delete_plan(day: PlanDay) -> Response:
        """Cancel the plan for today or tomorrow. Succeeds if there was none."""
        await scheduler.cancel_use(calendar_day(day))
        return Response(status_code=204)

    @router.get("/alerts", tags=["alerts"])
    async def get_alerts(
        days: Annotated[
            int,
            Query(
                ge=0, le=MAX_ALERT_DAYS, description="How far back the history goes; 0 for none."
            ),
        ] = 0,
    ) -> AlertsResponse:
        """Return the active alerts and, with `days`, the recent history."""
        history = watchdog.history(days) if days else []
        return AlertsResponse(
            active=[AlertResponse.from_alert(alert) for alert in watchdog.active_alerts()],
            history=[AlertResponse.from_alert(alert) for alert in history],
            notifications_configured=watchdog.notifications_configured,
        )

    # Declared before "/alerts/{alert_id}/..." so that it is not taken for a number.
    @router.post("/alerts/test-notification", tags=["alerts"], status_code=204, responses=_NOT_SENT)
    async def post_test_notification() -> Response:
        """Send a test notification to the owner's phone, now."""
        try:
            await watchdog.send_test_notification()
        except NotificationError as error:
            # Permanent with no keys at all: nothing was attempted.
            status = 502 if watchdog.notifications_configured else 409
            raise HTTPException(status_code=status, detail=str(error)) from error
        return Response(status_code=204)

    @router.post(
        "/alerts/{alert_id}/acknowledge", tags=["alerts"], status_code=204, responses=_NO_ALERT
    )
    async def post_acknowledge(alert_id: int) -> Response:
        """Clear a spa-fault alert. The other kinds clear by themselves."""
        watchdog.acknowledge(alert_id)
        return Response(status_code=204)

    @router.get("/healthz", tags=["status"])
    async def get_health() -> HealthResponse:
        """Report that the service is running.

        Answers 200 whenever the process is up, whether or not the spa is
        reachable: systemd restarts the service on a failed check, and a
        restart does not fix a tub that is switched off.
        """
        return HealthResponse(spa_connected=control.status().connected)

    return router

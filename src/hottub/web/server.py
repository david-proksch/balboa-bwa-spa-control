"""The web application: routes, and how errors become HTTP responses.

Three things are served, all under the same prefix: the pages
(``hottub.web.pages``), the files they load (``static/``), and the JSON
API (``hottub.web.api``, under ``/api/v1``).

nginx serves the application under a path prefix (``/hot-tub``) and passes
requests through with the prefix intact, so the application registers its
routes under the same prefix. Run locally, the URLs are therefore the same
as on the Pi.

Error mapping, in one place:

=============================  ======  =====================================
Raised by the service          Status  Meaning for the caller
=============================  ======  =====================================
``InvalidTemperatureError``    422     The request can never succeed as sent
``InvalidPlanError``           422     The same, for a planned use
``CommandRejectedError``       409     The spa was reached but refused
``SpaUnavailableError``        503     Try again later
``AlertNotFoundError``         404     There is no such alert
``AlertNotAcknowledgeable…``   409     The alert is over, or clears by itself
=============================  ======  =====================================
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from hottub import __version__
from hottub.control import ControlService
from hottub.domain import (
    AlertNotAcknowledgeableError,
    AlertNotFoundError,
    CommandRejectedError,
    InvalidPlanError,
    InvalidTemperatureError,
    SpaUnavailableError,
)
from hottub.scheduler import SchedulerService
from hottub.telemetry import TelemetryService
from hottub.watchdog import WatchdogService
from hottub.web.api import create_api_router
from hottub.web.pages import STATIC_DIRECTORY, create_pages_router

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

_STATUS_FOR_ERROR: dict[type[Exception], int] = {
    InvalidTemperatureError: 422,
    InvalidPlanError: 422,
    CommandRejectedError: 409,
    SpaUnavailableError: 503,
    AlertNotFoundError: 404,
    AlertNotAcknowledgeableError: 409,
}


def create_web_app(
    control: ControlService,
    telemetry: TelemetryService,
    scheduler: SchedulerService,
    watchdog: WatchdogService,
    *,
    base_path: str,
    lifespan: Lifespan,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        control: The service the handlers call to act on the tub.
        telemetry: The service the handlers call to read its history.
        scheduler: The service the handlers call about plans.
        watchdog: The service the handlers call about alerts.
        base_path: The prefix every URL starts with, e.g. ``"/hot-tub"``,
            or ``""`` for none.
        lifespan: Start-up and shutdown work, supplied by ``hottub.app``.
    """
    app = FastAPI(
        title="Hot Tub Controller",
        version=__version__,
        lifespan=lifespan,
        docs_url=f"{base_path}/api/docs",
        openapi_url=f"{base_path}/api/openapi.json",
        redoc_url=None,
    )
    app.include_router(
        create_api_router(control, telemetry, scheduler, watchdog), prefix=f"{base_path}/api/v1"
    )

    app.include_router(
        create_pages_router(control, telemetry, scheduler, watchdog, base_path=base_path)
    )
    app.mount(f"{base_path}/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")

    async def domain_error(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_FOR_ERROR[type(error)], content={"detail": str(error)}
        )

    for error_type in _STATUS_FOR_ERROR:
        app.add_exception_handler(error_type, domain_error)
    return app

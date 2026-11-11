"""The web pages: the dashboard, the alerts and the settings.

How the pages work
------------------
The HTML is rendered on the server from Jinja2 templates
(``templates/``). A page is made of **parts**, each a template in
``templates/partials/`` with one root element that has an ``id``: the
temperature reading, the controls, the plans, and so on.

HTMX (``static/vendor``) keeps the page current without a page reload and
without any JavaScript of our own beyond a few lines in ``static/app.js``:

* A part that changes by itself (the reading, the alerts banner) carries
  ``hx-get`` and ``hx-trigger="every Ns"``: the browser fetches the part
  again from its ``/ui/...`` address and replaces it.
* A button or form carries ``hx-post``. The handler here does the work
  and answers with the part the button lives in, re-rendered; HTMX swaps
  it in. Other parts that the action changed are sent along in the same
  answer, marked ``hx-swap-oob`` ("out of band"), and HTMX replaces those
  too.

So every ``/ui/...`` address returns a fragment of HTML, not a page, and
is of no use to anything but these pages. Programs should use the JSON
API (``hottub.web.api``), which offers the same actions.

When an action fails (the spa is unreachable, a value is not acceptable)
the handler still answers 200 with the part re-rendered, now containing
the error message, because HTMX does not swap in an error response.

Pattern: thin handlers over a Service Layer, as in the API. What to show
is decided in ``hottub.web.views``; the templates only place it.

There is no login: the service is reachable from the home network only
(PLAN.md, section 1).

Only the pages may press the buttons
------------------------------------
Without a login, the one thing standing between a stranger's web page and
the tub is the browser. A page on any website, opened on a phone that is
on the home network, could otherwise submit a form to
``http://james-brown.local/hot-tub/ui/...`` and switch a pump or change a
plan without the owner knowing (cross-site request forgery). So every
action (``POST``) under ``/ui`` is refused with 403 unless:

* it carries the ``HX-Request`` header, which HTMX adds to everything the
  pages send. A form on another site cannot add a header, and a script on
  another site that tries must first ask this service's permission, which
  is never given;
* the browser does not mark it as coming from another site
  (``Sec-Fetch-Site: cross-site``); and
* if the browser names where it comes from (``Origin``), that is the host
  the request was sent to.

Fetching a part (``GET``) changes nothing and is not checked. The JSON API
is safe without this: it takes JSON bodies, which another site cannot send
without asking permission first.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from hottub import __version__
from hottub.control import ControlService
from hottub.domain import (
    Actor,
    HeatMode,
    HotTubError,
    PumpSpeed,
    Temperature,
    TemperatureBand,
)
from hottub.scheduler import SchedulerService
from hottub.telemetry import TelemetryService
from hottub.watchdog import WatchdogService
from hottub.web import views

WEB_DIRECTORY = Path(__file__).parent
STATIC_DIRECTORY = WEB_DIRECTORY / "static"

#: The windows of the running-time table, in days.
RUNTIME_WINDOWS = (1, 7, 14, 30)

#: How far back the alerts page goes, in days.
ALERT_HISTORY_DAYS = 30

PlanDay = Literal["today", "tomorrow"]

#: ``ControlService.set_floor`` or ``set_ceiling``.
_ChangeEdge = Callable[[Temperature, Actor], Awaitable[object]]

#: A part of a page: the template's name and the values it needs.
Part = tuple[str, dict[str, Any]]


def create_pages_router(
    control: ControlService,
    telemetry: TelemetryService,
    scheduler: SchedulerService,
    watchdog: WatchdogService,
    *,
    base_path: str,
) -> APIRouter:
    """Build the routes for the pages and their parts.

    Args:
        control: Reads the tub and carries out the controls.
        telemetry: Supplies the running times.
        scheduler: Holds the plans.
        watchdog: Holds the alerts.
        base_path: The prefix every URL starts with; the templates need it
            to address the parts and the static files.
    """
    templates = Environment(
        loader=FileSystemLoader(WEB_DIRECTORY / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    templates.globals.update(base=base_path, version=__version__)
    router = APIRouter(include_in_schema=False, dependencies=[Depends(_from_our_pages)])
    zone = scheduler.zone

    # --- Rendering -------------------------------------------------------

    def render(main: Part, *also: Part) -> HTMLResponse:
        """Render ``main``, followed by the parts to swap in out of band."""
        html = templates.get_template(main[0]).render(main[1])
        for name, values in also:
            html += templates.get_template(name).render(values, oob=True)
        return HTMLResponse(html)

    # --- The parts, each with the values its template needs ---------------

    def reading(error: str | None = None) -> Part:
        return "partials/reading.html", {"tub": views.tub_view(control.status()), "error": error}

    def controls(error: str | None = None) -> Part:
        return "partials/controls.html", {
            "tub": views.tub_view(control.status()),
            "pump1_choices": views.PUMP1_CHOICES,
            "pump2_choices": views.PUMP2_CHOICES,
            "heat_mode_choices": views.HEAT_MODE_CHOICES,
            "error": error,
        }

    def outlook() -> Part:
        return "partials/outlook.html", {"outlook": views.outlook_view(scheduler.outlook())}

    def plans(error: str | None = None) -> Part:
        today = scheduler.today()
        now = scheduler.outlook().as_of
        days = [
            views.plan_day_view(key, day, scheduler.planned_heating(day), now, zone)
            for key, day in (("today", today), ("tomorrow", today + timedelta(days=1)))
        ]
        return "partials/plans.html", {
            "days": days,
            "weekend": views.weekend_view(scheduler.weekend_ready()),
            "error": error,
        }

    def runtime() -> Part:
        report = telemetry.runtime(RUNTIME_WINDOWS)
        return "partials/runtime.html", {"runtime": views.runtime_view(report, zone)}

    def banner() -> Part:
        return "partials/banner.html", {"alerts": views.alert_views(watchdog.active_alerts(), zone)}

    def alert_list(error: str | None = None) -> Part:
        history = [alert for alert in watchdog.history(ALERT_HISTORY_DAYS) if not alert.active]
        return "partials/alert_list.html", {
            "active": views.alert_views(watchdog.active_alerts(), zone),
            "history": views.alert_views(history, zone),
            "days": ALERT_HISTORY_DAYS,
            "error": error,
        }

    def band(error: str | None = None, saved: bool = False) -> Part:
        return "partials/settings_band.html", {
            "tub": views.tub_view(control.status()),
            "error": error,
            "saved": saved,
        }

    def hold(error: str | None = None, saved: bool = False) -> Part:
        return "partials/settings_hold.html", {
            "hours": f"{scheduler.hold_time().total_seconds() / 3600:g}",
            "error": error,
            "saved": saved,
        }

    def notify(result: str | None = None, failed: bool = False) -> Part:
        return "partials/settings_notify.html", {
            "configured": watchdog.notifications_configured,
            "result": result,
            "failed": failed,
        }

    def calendar_day(day: PlanDay) -> date:
        return scheduler.today() + timedelta(days=1 if day == "tomorrow" else 0)

    async def attempt(action: Callable[[], Awaitable[object]]) -> str | None:
        """Carry out ``action``; return the error message if it failed."""
        try:
            await action()
        except (HotTubError, ValueError) as error:
            return str(error)
        return None

    # --- Pages -----------------------------------------------------------

    def page(name: str, title: str, **parts: Part) -> HTMLResponse:
        """Render a whole page, with each of its parts already rendered."""
        values: dict[str, Any] = {"title": title, "page": name}
        for key, (template, part_values) in {"banner": banner(), **parts}.items():
            values[key] = Markup(templates.get_template(template).render(part_values))
        return HTMLResponse(templates.get_template(f"{name}.html").render(values))

    if base_path:

        @router.get(base_path)
        async def without_slash() -> RedirectResponse:
            """Send ``/hot-tub`` to ``/hot-tub/``."""
            return RedirectResponse(f"{base_path}/", status_code=308)

    @router.get(f"{base_path}/")
    async def dashboard() -> HTMLResponse:
        """The dashboard: the tub now, the plans, the controls, running time."""
        return page(
            "dashboard",
            "Hot tub",
            reading=reading(),
            outlook=outlook(),
            plans=plans(),
            controls=controls(),
            runtime=runtime(),
        )

    @router.get(f"{base_path}/alerts")
    async def alerts_page() -> HTMLResponse:
        """The alerts that stand now, and the recent history."""
        return page("alerts", "Alerts", alert_list=alert_list())

    @router.get(f"{base_path}/settings")
    async def settings_page() -> HTMLResponse:
        """Floor and ceiling, hold time, notifications."""
        return page("settings", "Settings", band=band(), hold=hold(), notify=notify())

    # --- Parts that refresh themselves -----------------------------------

    ui = f"{base_path}/ui"

    @router.get(f"{ui}/reading")
    async def get_reading() -> HTMLResponse:
        return render(reading())

    @router.get(f"{ui}/controls")
    async def get_controls() -> HTMLResponse:
        return render(controls())

    @router.get(f"{ui}/outlook")
    async def get_outlook() -> HTMLResponse:
        return render(outlook())

    @router.get(f"{ui}/runtime")
    async def get_runtime() -> HTMLResponse:
        return render(runtime())

    @router.get(f"{ui}/banner")
    async def get_banner() -> HTMLResponse:
        return render(banner())

    # --- Controls --------------------------------------------------------

    @router.post(f"{ui}/pump1")
    async def post_pump1(request: Request) -> HTMLResponse:
        speed = (await _form(request)).get("value", "")
        error = await attempt(lambda: control.set_pump1(PumpSpeed(speed), Actor.USER))
        return render(controls(error), reading())

    @router.post(f"{ui}/pump2")
    async def post_pump2(request: Request) -> HTMLResponse:
        value = (await _form(request)).get("value", "")
        error = await attempt(lambda: control.set_pump2(_on_or_off(value), Actor.USER))
        return render(controls(error), reading())

    @router.post(f"{ui}/heat-mode")
    async def post_heat_mode(request: Request) -> HTMLResponse:
        mode = (await _form(request)).get("value", "")
        error = await attempt(lambda: control.set_heat_mode(HeatMode(mode), Actor.USER))
        return render(controls(error), reading())

    # --- Plans -----------------------------------------------------------

    @router.post(f"{ui}/plan/{{day}}")
    async def post_plan(day: PlanDay, request: Request) -> HTMLResponse:
        use_time = (await _form(request)).get("use_time", "")

        async def plan() -> None:
            if not use_time:
                raise ValueError("Choose a time first.")
            await scheduler.plan_use(calendar_day(day), time.fromisoformat(use_time))

        error = await attempt(plan)
        return render(plans(error), outlook(), reading())

    @router.post(f"{ui}/plan/{{day}}/cancel")
    async def post_cancel_plan(day: PlanDay) -> HTMLResponse:
        error = await attempt(lambda: scheduler.cancel_use(calendar_day(day)))
        return render(plans(error), outlook(), reading())

    @router.post(f"{ui}/weekend")
    async def post_weekend(request: Request) -> HTMLResponse:
        value = (await _form(request)).get("value", "")
        error = await attempt(lambda: scheduler.set_weekend_ready(_on_or_off(value)))
        return render(plans(error), outlook(), reading())

    # --- Alerts ----------------------------------------------------------

    @router.post(f"{ui}/alerts/{{alert_id}}/acknowledge")
    async def post_acknowledge(alert_id: int) -> HTMLResponse:
        async def acknowledge() -> None:
            watchdog.acknowledge(alert_id)

        error = await attempt(acknowledge)
        return render(alert_list(error), banner())

    # --- Settings --------------------------------------------------------

    @router.post(f"{ui}/settings/band")
    async def post_band(request: Request) -> HTMLResponse:
        form = await _form(request)

        async def save() -> None:
            floor = Temperature(_number(form.get("floor", ""), "The floor"))
            ceiling = Temperature(_number(form.get("ceiling", ""), "The ceiling"))
            TemperatureBand(floor, ceiling)  # refuses a pair that makes no sense
            current = control.band
            changes: list[tuple[_ChangeEdge, Temperature]] = []
            if floor != current.floor:
                changes.append((control.set_floor, floor))
            if ceiling != current.ceiling:
                changes.append((control.set_ceiling, ceiling))
            # Each edge is checked against the other as it stands. When the
            # band moves up, the ceiling has to make room first.
            if ceiling.fahrenheit > current.ceiling.fahrenheit:
                changes.reverse()
            for change, edge in changes:
                await change(edge, Actor.USER)

        error = await attempt(save)
        return render(band(error, saved=error is None))

    @router.post(f"{ui}/settings/hold")
    async def post_hold(request: Request) -> HTMLResponse:
        form = await _form(request)

        async def save() -> None:
            hours = _number(form.get("hours", ""), "The time")
            await scheduler.set_hold_time(timedelta(hours=hours))

        error = await attempt(save)
        return render(hold(error, saved=error is None))

    @router.post(f"{ui}/settings/test-notification")
    async def post_test_notification() -> HTMLResponse:
        error = await attempt(watchdog.send_test_notification)
        if error is None:
            return render(notify("Sent. It should arrive on your phone within a few seconds."))
        return render(notify(error, failed=True))

    return router


def _from_our_pages(request: Request) -> None:
    """Refuse an action that did not come from these pages (see the module docstring).

    Raises:
        HTTPException: 403, if the request changes something and was not
            sent by these pages.
    """
    if request.method in ("GET", "HEAD"):
        return
    if request.headers.get("hx-request") != "true":
        raise HTTPException(status_code=403, detail="Only the hot tub's own pages may do this.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Requests from other sites are refused.")
    origin = request.headers.get("origin")
    if origin is not None:
        # Host names only: nginx passes the host without the port.
        sent_from = urlsplit(origin).hostname
        sent_to = urlsplit(f"//{request.headers.get('host', '')}").hostname
        if sent_from is None or sent_from != sent_to:
            raise HTTPException(status_code=403, detail="Requests from other sites are refused.")


async def _form(request: Request) -> dict[str, str]:
    """Read a submitted form.

    Forms arrive URL-encoded, which the standard library can parse; that
    saves depending on a multipart library for a few one-field forms.
    """
    body = (await request.body()).decode("utf-8", "replace")
    return {key: values[-1].strip() for key, values in parse_qs(body, True).items()}


def _on_or_off(value: str) -> bool:
    if value not in ("on", "off"):
        raise ValueError(f"Expected 'on' or 'off', not {value!r}")
    return value == "on"


def _number(text: str, what: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{what} must be a number.") from None

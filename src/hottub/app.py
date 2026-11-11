"""The composition root: where the application is assembled.

Pattern: Composition Root with constructor injection (Seemann; Fowler,
"Inversion of Control Containers and the Dependency Injection pattern").
This is the only module that knows which concrete classes are in use. Every
other module receives its collaborators through its constructor and names
only interfaces, which is why tests can substitute a simulated spa or an
in-memory repository without patching anything.

There is no dependency-injection framework. The application is small enough
that :func:`build_app` reads top to bottom as the wiring diagram.

Run it with ``python -m hottub``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

import uvicorn
from fastapi import FastAPI

from hottub.config import AppConfig, load_config
from hottub.control import ControlService
from hottub.domain import (
    Clock,
    HotTubError,
    Notifier,
    PlannedUsePolicy,
    SystemClock,
    WeekendReadyPolicy,
)
from hottub.persistence import (
    SqlAlertRepository,
    SqlCommandLog,
    SqlPlanRepository,
    SqlRunIntervalRepository,
    SqlSettingsRepository,
    SqlTemperatureSampleRepository,
    migrate,
    open_database,
)
from hottub.scheduler import READING_TRUSTED_FOR, SchedulerService
from hottub.spa import BalboaSettings, BalboaSpaAdapter, FakeSpaAdapter, SpaPort
from hottub.telemetry import TelemetryRecorder, TelemetryService
from hottub.telemetry.recorder import DEFAULT_TICK_PERIOD
from hottub.watchdog import (
    NotificationOutbox,
    NullNotifier,
    PushoverKeys,
    PushoverNotifier,
    WatchdogService,
)
from hottub.web import create_web_app

_LOGGER = logging.getLogger(__name__)

#: How often the simulated tub is moved forward, in seconds.
_SIMULATION_INTERVAL_S = 1.0

#: How often the history is brought up to date, in seconds. This is also
#: the spacing of the temperature samples.
_RECORDING_INTERVAL_S = DEFAULT_TICK_PERIOD.total_seconds()

#: How often the scheduler compares the spa with the plans, in seconds.
_RECONCILE_INTERVAL_S = 60.0

#: How long after start-up the scheduler makes its first pass, in seconds.
_FIRST_RECONCILE_DELAY_S = 10.0

#: How often the watchdog looks at the spa, in seconds.
_WATCH_INTERVAL_S = 60.0

#: How long after start-up the watchdog makes its first pass, in seconds.
_FIRST_WATCH_DELAY_S = 15.0

#: History older than this is deleted, which bounds the database's size.
#: The longest window the web page shows is 30 days.
HISTORY_KEPT = timedelta(days=90)

#: Old history is deleted at start-up and then once per this many recordings.
_RECORDINGS_PER_PRUNE = 60


def build_app(
    config: AppConfig, *, spa: SpaPort | None = None, clock: Clock | None = None
) -> FastAPI:
    """Assemble the application.

    The database is opened and migrated here, so a broken database stops
    the service at once with a clear error. The spa is contacted only when
    the returned application starts.

    Args:
        config: Where things are and which spa adapter to use.
        spa: A ready-made spa to use instead of the configured one. For
            tests, which want to hold on to a simulated tub and stage
            situations on it.
        clock: Source of time; the system clock unless a test supplies one.
    """
    clock = clock or SystemClock()

    engine = open_database(config.database_path)
    migrate(engine)

    simulated: FakeSpaAdapter | None = None
    if spa is None:
        if config.spa_adapter == "fake":
            _LOGGER.warning("Using the SIMULATED spa; no real tub will be contacted")
            spa = simulated = FakeSpaAdapter(clock)
        else:
            spa = BalboaSpaAdapter(
                clock, BalboaSettings(host=config.spa_host, mac_suffix=config.spa_mac_suffix)
            )
    the_spa = spa

    the_clock = clock

    command_log = SqlCommandLog(engine)
    intervals = SqlRunIntervalRepository(engine)
    samples = SqlTemperatureSampleRepository(engine)

    control = ControlService(
        spa=the_spa,
        settings=SqlSettingsRepository(engine),
        command_log=command_log,
        clock=clock,
        default_band=config.default_band,
    )
    recorder = TelemetryRecorder(the_spa, intervals, samples, clock)
    telemetry = TelemetryService(intervals, samples, clock, config.seed_heating_rate_f_per_hour)

    scheduler = SchedulerService(
        control=control,
        plans=SqlPlanRepository(engine),
        heating_rate=telemetry.heating_rate,
        clock=clock,
        zone=config.timezone,
        planned_use_policy=PlannedUsePolicy(),
        weekend_policy=WeekendReadyPolicy(config.weekend_start, config.blackout),
        recent_water=lambda: telemetry.latest_water(READING_TRUSTED_FOR),
        enabled=config.scheduler_enabled,
    )
    if not config.scheduler_enabled:
        _LOGGER.warning("The scheduler is switched off; it will not send the spa any command")

    notifier: Notifier
    if config.pushover_token and config.pushover_user:
        notifier = PushoverNotifier(
            PushoverKeys(config.pushover_token, config.pushover_user),
            emergency=config.pushover_emergency,
        )
    else:
        _LOGGER.warning("No Pushover keys are configured; alerts appear on the web page only")
        notifier = NullNotifier()
    outbox = NotificationOutbox(notifier)
    watchdog = WatchdogService(
        control=control,
        spa=the_spa,
        alerts=SqlAlertRepository(engine),
        outbox=outbox,
        clock=clock,
        thresholds=config.watchdog,
    )

    def prune_history() -> None:
        """Delete history older than :data:`HISTORY_KEPT`."""
        cutoff = the_clock.now() - HISTORY_KEPT
        scheduler.forget_plans_before(cutoff.date())
        intervals.delete_before(cutoff)
        samples.delete_before(cutoff)
        command_log.delete_before(cutoff)
        watchdog.forget_alerts_before(cutoff)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Start the background work, and stop it when the server stops."""
        # Recording starts before the connection is made, so the very first
        # report from the spa is recorded.
        recorder.start()
        tasks = [
            asyncio.create_task(_connect(the_spa), name="spa-connect"),
            asyncio.create_task(_keep_history(recorder, prune_history), name="history"),
            asyncio.create_task(_run_watchdog(watchdog), name="watchdog"),
        ]
        if config.scheduler_enabled:
            tasks.append(asyncio.create_task(_run_scheduler(scheduler), name="scheduler"))
        if simulated is not None:
            tasks.append(asyncio.create_task(_run_simulation(simulated), name="spa-simulation"))
        # Give the tasks their first turn. The simulated spa connects in it,
        # so it is ready before the first request; the real one carries on
        # in the background.
        await asyncio.sleep(0)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await outbox.close()
            # While the spa can still be asked how things stand.
            recorder.stop()
            await the_spa.disconnect()
            engine.dispose()

    return create_web_app(
        control, telemetry, scheduler, watchdog, base_path=config.base_path, lifespan=lifespan
    )


async def _connect(spa: SpaPort) -> None:
    """Make the first connection to the spa without holding up start-up.

    The web server must come up even when the tub is switched off, so the
    status page can say so. After a failed first attempt the adapter keeps
    retrying by itself (see :meth:`hottub.spa.SpaPort.connect`).
    """
    try:
        await spa.connect()
    except HotTubError as error:
        _LOGGER.warning("The spa is not reachable yet, still trying: %s", error)


async def _keep_history(recorder: TelemetryRecorder, prune: Callable[[], None]) -> None:
    """Bring the history up to date once a minute, and delete what is too old.

    A failure is logged and the loop carries on: one bad minute (a full
    disk, say) must not end recording for good.
    """
    recordings = 0
    while True:
        try:
            if recordings % _RECORDINGS_PER_PRUNE == 0:
                prune()
            if recordings:
                recorder.tick()
        except Exception:
            _LOGGER.exception("Could not bring the history up to date")
        recordings += 1
        await asyncio.sleep(_RECORDING_INTERVAL_S)


async def _run_scheduler(scheduler: SchedulerService) -> None:
    """Have the scheduler compare the spa with the plans once a minute.

    The first pass waits a few seconds for the connection to the spa; a
    pass without one does nothing. A failure is logged and the loop
    carries on, for the same reason as in :func:`_keep_history`.
    """
    await asyncio.sleep(_FIRST_RECONCILE_DELAY_S)
    while True:
        try:
            await scheduler.reconcile()
        except Exception:
            _LOGGER.exception("The scheduler's pass failed")
        await asyncio.sleep(_RECONCILE_INTERVAL_S)


async def _run_watchdog(watchdog: WatchdogService) -> None:
    """Have the watchdog look at the spa once a minute.

    The first pass waits a few seconds, like the scheduler's. A failure is
    logged and the loop carries on: the watchdog of all things must not
    stop watching because one pass went wrong.
    """
    await asyncio.sleep(_FIRST_WATCH_DELAY_S)
    while True:
        try:
            await watchdog.check()
        except Exception:
            _LOGGER.exception("The watchdog's pass failed")
        await asyncio.sleep(_WATCH_INTERVAL_S)


async def _run_simulation(spa: FakeSpaAdapter) -> None:
    """Keep the simulated tub in step with the clock.

    The simulation only moves when told to (it never sleeps, so tests stay
    fast); when it stands in for a real tub, something has to tell it.
    """
    while True:
        await asyncio.sleep(_SIMULATION_INTERVAL_S)
        spa.tick()


def main() -> None:
    """Load the configuration and serve until stopped."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    uvicorn.run(
        build_app(config),
        host=config.web_host,
        port=config.web_port,
        # Keep the format above; systemd's journal adds the timestamps.
        log_config=None,
    )

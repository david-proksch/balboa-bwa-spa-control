"""Configuration: what differs between the Pi and a developer's laptop.

Settings come from a TOML file, by default ``/etc/hottub/hottub.toml``. A
missing default file is not an error; the defaults below describe the Pi.
A few environment variables override the file, for running locally:

=========================  ==================================================
``HOTTUB_CONFIG``          Path of the TOML file to read instead
``HOTTUB_SPA_ADAPTER``     ``balboa`` (the real tub) or ``fake`` (simulated)
``HOTTUB_DATABASE_PATH``   Where to keep the SQLite file
``HOTTUB_SCHEDULER``       ``on``, or ``off`` to send the spa no commands
``HOTTUB_PUSHOVER_TOKEN``  Pushover application token (a secret, see below)
``HOTTUB_PUSHOVER_USER``   Pushover user key (a secret, see below)
=========================  ==================================================

Example file, showing every key with its default::

    [spa]
    adapter = "balboa"
    # host = "192.168.1.50"   # skip discovery; omit to find it by MAC
    # mac_suffix = "ABCDEF"    # which spa to pick if several answer

    [database]
    path = "/var/lib/hottub/hottub.db"

    [web]
    host = "127.0.0.1"
    port = 8100
    base_path = "/hot-tub"

    [defaults]
    floor_f = 85
    ceiling_f = 99
    heating_rate_f_per_hour = 4   # assumed until the tub's own rate is measured

    [schedule]
    enabled = true
    # timezone = "America/Detroit"   # omit to use the computer's own
    weekend_start = "15:00"          # on Friday; the weekend ends Sunday midnight
    blackout_start = "01:00"         # no weekend heating from here...
    blackout_end = "12:00"           # ...to here

    [watchdog]                       # PLAN.md, "Watchdog behaviour"
    stale_after_seconds = 90         # silence from the spa: a warning...
    stale_critical_after_minutes = 15   # ...and then critical
    cold_margin_f = 3                # too cold: this far below the floor
    hot_margin_f = 2                 # too hot: this far above the ceiling
    out_of_band_minutes = 30         # ...for this long
    maximum_f = 104                  # too hot at once
    heat_call_gap_f = 2              # set temperature this far above the water
    expected_rise_f = 1              # the water should gain this much...
    rise_within_minutes = 45         # ...within this time while heating
    fault_check_minutes = 5          # how often the fault log is read

    [pushover]
    emergency = false                # true: alerts repeat until acknowledged

Times of day are wall-clock times in the time zone, written ``"HH:MM"``.
With ``enabled = false`` plans can still be stored and the forecast still
works, but the scheduler never sends the spa a command: for running the
application next to a tub without taking it over.

Secrets
-------
The two Pushover keys are **not** in this file. They come from the
environment variables above, which on the Pi systemd fills from
``/etc/hottub/secrets.env`` (readable by root and the service only). They
are kept out of the text form of :class:`AppConfig`, so they do not show up
in logs or error reports. With neither set, alerts are shown on the web
page only; with just one set the service refuses to start.

Unknown sections or keys are an error, so a typing mistake in the file
stops the service with a message instead of being silently ignored.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal, cast, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hottub.domain import (
    BlackoutWindow,
    HotTubError,
    Temperature,
    TemperatureBand,
    WatchdogThresholds,
)

DEFAULT_CONFIG_PATH = Path("/etc/hottub/hottub.toml")

_LOCALTIME = Path("/etc/localtime")

SpaAdapterName = Literal["balboa", "fake"]


class ConfigError(HotTubError):
    """The configuration file or environment is unusable."""


@dataclass(frozen=True)
class AppConfig:
    """Every setting the application needs to start.

    Attributes:
        spa_adapter: ``"balboa"`` for the real tub, ``"fake"`` for the
            simulated one.
        spa_host: Address of the spa's Wi-Fi module, or ``None`` to find it
            by broadcast.
        spa_mac_suffix: Tail of the module's MAC address, used to pick the
            right spa when discovering.
        database_path: The SQLite file. Its directory must exist.
        web_host: Address the web server listens on. Loopback, because
            nginx is the only client.
        web_port: Port the web server listens on.
        base_path: URL prefix the application is served under.
        default_band: The floor and ceiling used until the owner sets them.
            The default ceiling of 99 °F is what the tub was set to when
            this was written, and is the top of the controller's low range.
        seed_heating_rate_f_per_hour: How fast the heater is assumed to warm
            the water until enough heating is on record to measure it.
            Deliberately on the slow side: too low a figure starts the
            heater early, too high a figure makes the tub late.
        scheduler_enabled: False to keep the scheduler from sending the spa
            any command.
        timezone: The owner's time zone, in which plans and the times below
            are expressed. :func:`load_config` fills in the computer's own;
            the default here is UTC so that tests do not depend on where
            they run.
        weekend_start: When on Friday weekend keep-ready begins.
        blackout: The hours of each weekend day with no heating.
        watchdog: The figures the watchdog's rules use.
        pushover_token: Pushover application token, or ``None``. A secret:
            left out of this object's text form.
        pushover_user: Pushover user key, or ``None``. Likewise a secret.
        pushover_emergency: Send alerts at Pushover's emergency priority,
            which repeats until acknowledged.
    """

    spa_adapter: SpaAdapterName = "balboa"
    spa_host: str | None = None
    spa_mac_suffix: str | None = None
    database_path: Path = Path("/var/lib/hottub/hottub.db")
    web_host: str = "127.0.0.1"
    web_port: int = 8100
    base_path: str = "/hot-tub"
    default_band: TemperatureBand = TemperatureBand(Temperature(85.0), Temperature(99.0))
    seed_heating_rate_f_per_hour: float = 4.0
    scheduler_enabled: bool = True
    timezone: tzinfo = ZoneInfo("UTC")
    weekend_start: time = time(15, 0)
    blackout: BlackoutWindow = BlackoutWindow(time(1, 0), time(12, 0))
    watchdog: WatchdogThresholds = WatchdogThresholds()
    pushover_token: str | None = field(default=None, repr=False)
    pushover_user: str | None = field(default=None, repr=False)
    pushover_emergency: bool = False


#: The sections and keys a configuration file may contain.
_KNOWN_KEYS: dict[str, set[str]] = {
    "spa": {"adapter", "host", "mac_suffix"},
    "database": {"path"},
    "web": {"host", "port", "base_path"},
    "defaults": {"floor_f", "ceiling_f", "heating_rate_f_per_hour"},
    "schedule": {"enabled", "timezone", "weekend_start", "blackout_start", "blackout_end"},
    "watchdog": {
        "stale_after_seconds",
        "stale_critical_after_minutes",
        "cold_margin_f",
        "hot_margin_f",
        "out_of_band_minutes",
        "maximum_f",
        "heat_call_gap_f",
        "expected_rise_f",
        "rise_within_minutes",
        "fault_check_minutes",
    },
    "pushover": {"emergency"},
}


def load_config(environ: Mapping[str, str] | None = None) -> AppConfig:
    """Read the configuration file and apply environment overrides.

    Args:
        environ: The environment to read; defaults to the process's own.
            A parameter so tests need not modify the real environment.

    Raises:
        ConfigError: If a named file is missing, the file is not valid
            TOML, or it contains an unknown key or an unusable value.
    """
    environ = os.environ if environ is None else environ
    explicit_path = environ.get("HOTTUB_CONFIG")
    path = Path(explicit_path) if explicit_path else DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            with path.open("rb") as file:
                document = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"Cannot read {path}: {error}") from error
    elif explicit_path:
        raise ConfigError(f"HOTTUB_CONFIG names {path}, which does not exist")
    else:
        document = {}
    _reject_unknown_keys(document, path)

    spa = document.get("spa", {})
    database = document.get("database", {})
    web = document.get("web", {})
    defaults = document.get("defaults", {})
    schedule = document.get("schedule", {})
    pushover = document.get("pushover", {})
    standard = AppConfig()

    adapter = environ.get("HOTTUB_SPA_ADAPTER") or spa.get("adapter", standard.spa_adapter)
    if adapter not in get_args(SpaAdapterName):
        raise ConfigError(f"The spa adapter must be 'balboa' or 'fake', not {adapter!r}")
    token = _optional_text(environ.get("HOTTUB_PUSHOVER_TOKEN"))
    user = _optional_text(environ.get("HOTTUB_PUSHOVER_USER"))
    if (token is None) != (user is None):
        raise ConfigError("Set both HOTTUB_PUSHOVER_TOKEN and HOTTUB_PUSHOVER_USER, or neither")
    try:
        seed_rate = float(
            defaults.get("heating_rate_f_per_hour", standard.seed_heating_rate_f_per_hour)
        )
        if seed_rate <= 0:
            raise ValueError("heating_rate_f_per_hour must be greater than zero")
        return AppConfig(
            spa_adapter=cast(SpaAdapterName, adapter),
            spa_host=_optional_text(spa.get("host")),
            spa_mac_suffix=_optional_text(spa.get("mac_suffix")),
            database_path=Path(
                environ.get("HOTTUB_DATABASE_PATH") or database.get("path", standard.database_path)
            ),
            web_host=str(web.get("host", standard.web_host)),
            web_port=int(web.get("port", standard.web_port)),
            base_path=_normalise_base_path(str(web.get("base_path", standard.base_path))),
            default_band=TemperatureBand(
                floor=Temperature(
                    float(defaults.get("floor_f", standard.default_band.floor.fahrenheit))
                ),
                ceiling=Temperature(
                    float(defaults.get("ceiling_f", standard.default_band.ceiling.fahrenheit))
                ),
            ),
            seed_heating_rate_f_per_hour=seed_rate,
            scheduler_enabled=_scheduler_enabled(schedule, environ),
            timezone=_timezone(schedule.get("timezone"), environ),
            weekend_start=_time_of_day(schedule, "weekend_start", standard.weekend_start),
            blackout=BlackoutWindow(
                start=_time_of_day(schedule, "blackout_start", standard.blackout.start),
                end=_time_of_day(schedule, "blackout_end", standard.blackout.end),
            ),
            watchdog=_watchdog_thresholds(document.get("watchdog", {})),
            pushover_token=token,
            pushover_user=user,
            pushover_emergency=_boolean(pushover, "emergency", standard.pushover_emergency),
        )
    except (TypeError, ValueError) as error:
        # Includes InvalidTemperatureError, for a floor above the ceiling.
        raise ConfigError(f"Bad value in {path}: {error}") from error


def _reject_unknown_keys(document: Mapping[str, Any], path: Path) -> None:
    for section, content in document.items():
        if section not in _KNOWN_KEYS or not isinstance(content, dict):
            raise ConfigError(f"{path}: unknown section [{section}]")
        for key in content:
            if key not in _KNOWN_KEYS[section]:
                raise ConfigError(f"{path}: unknown key '{key}' in [{section}]")


def _scheduler_enabled(schedule: Mapping[str, Any], environ: Mapping[str, str]) -> bool:
    override = environ.get("HOTTUB_SCHEDULER")
    if override:
        if override not in ("on", "off"):
            raise ValueError(f"HOTTUB_SCHEDULER must be 'on' or 'off', not {override!r}")
        return override == "on"
    return _boolean(schedule, "enabled", True)


def _boolean(section: Mapping[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _watchdog_thresholds(section: Mapping[str, Any]) -> WatchdogThresholds:
    """Build the watchdog's figures from the ``[watchdog]`` section.

    Raises:
        ValueError: If a value is not a number or makes no sense.
    """
    standard = WatchdogThresholds()

    def number(key: str, default: float) -> float:
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{key} must be a number")
        return float(value)

    def minutes(key: str, default: timedelta) -> timedelta:
        return timedelta(minutes=number(key, default.total_seconds() / 60))

    return WatchdogThresholds(
        stale_after=timedelta(
            seconds=number("stale_after_seconds", standard.stale_after.total_seconds())
        ),
        stale_critical_after=minutes("stale_critical_after_minutes", standard.stale_critical_after),
        cold_margin_f=number("cold_margin_f", standard.cold_margin_f),
        hot_margin_f=number("hot_margin_f", standard.hot_margin_f),
        out_of_band_for=minutes("out_of_band_minutes", standard.out_of_band_for),
        absolute_maximum=Temperature(number("maximum_f", standard.absolute_maximum.fahrenheit)),
        heat_call_gap_f=number("heat_call_gap_f", standard.heat_call_gap_f),
        expected_rise_f=number("expected_rise_f", standard.expected_rise_f),
        rise_within=minutes("rise_within_minutes", standard.rise_within),
        fault_check_every=minutes("fault_check_minutes", standard.fault_check_every),
    )


def _time_of_day(schedule: Mapping[str, Any], key: str, default: time) -> time:
    value = schedule.get(key)
    if value is None:
        return default
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f'{key} must be a time of day such as "15:00", not {value!r}') from None


def _timezone(name: object, environ: Mapping[str, str]) -> tzinfo:
    """Return the configured time zone, or else the computer's own.

    The zone must be a named one ("Europe/Berlin"), not a fixed offset:
    the scheduler has to know when daylight saving begins and ends.

    Raises:
        ValueError: If the name is unknown, or none is given and the
            computer's zone cannot be worked out.
    """
    name = name or environ.get("TZ") or _system_timezone_name()
    if not name:
        raise ValueError(
            "cannot tell the computer's time zone; set timezone in [schedule], "
            'for example timezone = "America/Chicago"'
        )
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r}") from None


def _system_timezone_name() -> str | None:
    """Return the name of the zone ``/etc/localtime`` points at, if it can be told.

    On Raspberry Pi OS and macOS alike that file is a link into a
    ``zoneinfo`` directory, and the rest of the path is the zone's name.
    """
    parts = _LOCALTIME.resolve().parts
    # The directory is "zoneinfo" on Linux and "zoneinfo.default" on macOS.
    marks = [index for index, part in enumerate(parts) if part.startswith("zoneinfo")]
    if not marks:
        return None
    return "/".join(parts[marks[-1] + 1 :]) or None


def _optional_text(value: object) -> str | None:
    return None if value is None or value == "" else str(value)


def _normalise_base_path(base_path: str) -> str:
    """Return the prefix as ``""`` or ``"/something"``, with no trailing slash."""
    stripped = base_path.strip("/")
    return f"/{stripped}" if stripped else ""

"""Loading the configuration file and its environment overrides."""

from __future__ import annotations

from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hottub.config import AppConfig, ConfigError, load_config
from hottub.domain import BlackoutWindow, Temperature


def write(tmp_path: Path, text: str) -> dict[str, str]:
    """Write a configuration file and return an environment that names it."""
    path = tmp_path / "hottub.toml"
    path.write_text(text)
    return {"HOTTUB_CONFIG": str(path)}


def test_without_a_file_the_defaults_describe_the_pi() -> None:
    config = load_config({"TZ": "UTC"})

    assert config == AppConfig()
    assert config.spa_adapter == "balboa"
    assert config.base_path == "/hot-tub"
    assert (config.web_host, config.web_port) == ("127.0.0.1", 8100)


def test_a_file_overrides_the_defaults(tmp_path: Path) -> None:
    environ = write(
        tmp_path,
        """
        [spa]
        host = "192.168.1.50"
        mac_suffix = "ABCDEF"
        [database]
        path = "/tmp/tub.db"
        [web]
        port = 9000
        base_path = "tub/"
        [defaults]
        floor_f = 80
        ceiling_f = 102
        heating_rate_f_per_hour = 3.5
        """,
    )

    config = load_config(environ)

    assert config.spa_host == "192.168.1.50"
    assert config.spa_mac_suffix == "ABCDEF"
    assert config.database_path == Path("/tmp/tub.db")
    assert config.web_port == 9000
    assert config.base_path == "/tub"
    assert config.default_band.floor == Temperature(80)
    assert config.default_band.ceiling == Temperature(102)
    assert config.seed_heating_rate_f_per_hour == 3.5


def test_the_environment_overrides_the_file(tmp_path: Path) -> None:
    environ = write(tmp_path, '[spa]\nadapter = "balboa"\n[database]\npath = "/tmp/a.db"\n')
    environ |= {"HOTTUB_SPA_ADAPTER": "fake", "HOTTUB_DATABASE_PATH": "/tmp/b.db"}

    config = load_config(environ)

    assert config.spa_adapter == "fake"
    assert config.database_path == Path("/tmp/b.db")


def test_an_empty_base_path_means_no_prefix(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, '[web]\nbase_path = "/"\n')).base_path == ""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[spa]\nhots = "1.2.3.4"\n', "unknown key 'hots'"),
        ("[sap]\n", "unknown section"),
        ('[spa]\nadapter = "jacuzzi"\n', "must be 'balboa' or 'fake'"),
        ("[defaults]\nfloor_f = 100\n", "must be below ceiling"),
        ("[defaults]\nheating_rate_f_per_hour = 0\n", "greater than zero"),
        ('[web]\nport = "eighty"\n', "Bad value"),
        ("[spa\n", "Cannot read"),
        ('[schedule]\ntimezone = "Mars/Olympus"\n', "unknown time zone"),
        ('[schedule]\nweekend_start = "3pm"\n', "time of day"),
        ('[schedule]\nenabled = "no"\n', "true or false"),
    ],
)
def test_mistakes_in_the_file_are_reported(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_a_named_file_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config({"HOTTUB_CONFIG": str(tmp_path / "missing.toml")})


def test_the_schedule_section_sets_zone_and_times(tmp_path: Path) -> None:
    environ = write(
        tmp_path,
        """
        [schedule]
        enabled = false
        timezone = "Europe/Berlin"
        weekend_start = "16:30"
        blackout_start = "23:00"
        blackout_end = "07:00"
        """,
    )

    config = load_config(environ)

    assert config.scheduler_enabled is False
    assert config.timezone == ZoneInfo("Europe/Berlin")
    assert config.weekend_start == time(16, 30)
    assert config.blackout == BlackoutWindow(time(23, 0), time(7, 0))


def test_the_zone_defaults_to_the_computers_own() -> None:
    assert load_config({"TZ": "America/Detroit"}).timezone == ZoneInfo("America/Detroit")
    # Without TZ it is read from /etc/localtime; which zone depends on the machine.
    assert isinstance(load_config({}).timezone, ZoneInfo)


def test_the_environment_can_switch_the_scheduler_off(tmp_path: Path) -> None:
    assert load_config({"HOTTUB_SCHEDULER": "off"}).scheduler_enabled is False
    assert load_config({}).scheduler_enabled is True
    with pytest.raises(ConfigError, match="'on' or 'off'"):
        load_config({"HOTTUB_SCHEDULER": "maybe"})


def test_the_watchdog_section_sets_its_figures(tmp_path: Path) -> None:
    environ = write(
        tmp_path,
        """
        [watchdog]
        stale_after_seconds = 120
        stale_critical_after_minutes = 20
        cold_margin_f = 5
        out_of_band_minutes = 45
        maximum_f = 103.5
        fault_check_minutes = 10
        """,
    )

    watchdog = load_config(environ | {"TZ": "UTC"}).watchdog

    assert watchdog.stale_after == timedelta(minutes=2)
    assert watchdog.stale_critical_after == timedelta(minutes=20)
    assert watchdog.cold_margin_f == 5.0
    assert watchdog.out_of_band_for == timedelta(minutes=45)
    assert watchdog.absolute_maximum == Temperature(103.5)
    assert watchdog.fault_check_every == timedelta(minutes=10)
    # Left alone: the standard figures.
    assert (watchdog.hot_margin_f, watchdog.rise_within) == (2.0, timedelta(minutes=45))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[watchdog]\ncold_margin_f = 'three'", "cold_margin_f must be a number"),
        ("[watchdog]\nout_of_band_minutes = 0", "greater than zero"),
        ("[watchdog]\nstale_critical_after_minutes = 1", "critical before"),
        ("[watchdog]\nloudness = 11", "unknown key 'loudness'"),
        ("[pushover]\nemergency = 'yes'", "emergency must be true or false"),
        ("[pushover]\ntoken = 'abc'", "unknown key 'token'"),
    ],
)
def test_mistakes_in_the_watchdog_settings_are_reported(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text) | {"TZ": "UTC"})


def test_the_pushover_keys_come_from_the_environment_only(tmp_path: Path) -> None:
    environ = write(tmp_path, "[pushover]\nemergency = true") | {
        "TZ": "UTC",
        "HOTTUB_PUSHOVER_TOKEN": "app-token-secret",
        "HOTTUB_PUSHOVER_USER": "user-key-secret",
    }

    config = load_config(environ)

    assert (config.pushover_token, config.pushover_user) == ("app-token-secret", "user-key-secret")
    assert config.pushover_emergency
    assert "secret" not in repr(config)


def test_without_keys_there_is_no_pushover() -> None:
    config = load_config({"TZ": "UTC"})

    assert (config.pushover_token, config.pushover_user) == (None, None)
    assert not config.pushover_emergency


@pytest.mark.parametrize("name", ["HOTTUB_PUSHOVER_TOKEN", "HOTTUB_PUSHOVER_USER"])
def test_one_pushover_key_without_the_other_is_an_error(name: str) -> None:
    with pytest.raises(ConfigError, match="both") as raised:
        load_config({"TZ": "UTC", name: "half-a-secret"})

    assert "half-a-secret" not in str(raised.value)

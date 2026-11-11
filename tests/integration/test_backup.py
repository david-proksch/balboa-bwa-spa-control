"""Backing up the database, against real files."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hottub import backup
from hottub.domain import Temperature, TemperatureBand
from hottub.persistence import SqlSettingsRepository, migrate, open_database
from hottub.persistence.backup import backup_database

TODAY = date(2026, 11, 11)
BAND = TemperatureBand(Temperature(88), Temperature(101))


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "hottub.db"
    engine = open_database(path)
    migrate(engine)
    SqlSettingsRepository(engine).save_band(BAND)
    engine.dispose()
    return path


def band_in(copy: Path) -> TemperatureBand | None:
    engine = open_database(copy)
    try:
        return SqlSettingsRepository(engine).load_band()
    finally:
        engine.dispose()


def test_the_copy_is_a_usable_database_with_the_same_content(
    database: Path, tmp_path: Path
) -> None:
    copy = backup_database(database, tmp_path / "backups", TODAY)

    assert copy == tmp_path / "backups" / "hottub-2026-11-11.db"
    assert band_in(copy) == BAND


def test_works_while_the_service_has_the_database_open(database: Path, tmp_path: Path) -> None:
    engine = open_database(database)  # as the running service does
    later = TemperatureBand(Temperature(90), Temperature(102))
    SqlSettingsRepository(engine).save_band(later)

    copy = backup_database(database, tmp_path / "backups", TODAY)

    assert band_in(copy) == later
    engine.dispose()


def test_a_second_copy_on_the_same_day_replaces_the_first(database: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backup_database(database, backups, TODAY)

    backup_database(database, backups, TODAY)

    assert [path.name for path in backups.iterdir()] == ["hottub-2026-11-11.db"]


def test_only_the_newest_copies_are_kept(database: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    for age in range(9, -1, -1):
        backup_database(database, backups, TODAY - timedelta(days=age))

    names = sorted(path.name for path in backups.iterdir())

    assert len(names) == 7
    assert (names[0], names[-1]) == ("hottub-2026-11-05.db", "hottub-2026-11-11.db")


def test_other_files_in_the_directory_are_left_alone(database: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "notes.txt").write_text("keep me")

    backup_database(database, backups, TODAY, keep=1)

    assert (backups / "notes.txt").read_text() == "keep me"


def test_a_missing_database_is_an_error_not_an_empty_copy(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no database"):
        backup_database(tmp_path / "nowhere.db", tmp_path / "backups", TODAY)

    assert not (tmp_path / "nowhere.db").exists()
    assert not (tmp_path / "backups").exists()


def test_a_failed_copy_leaves_no_half_written_file(tmp_path: Path) -> None:
    not_a_database = tmp_path / "hottub.db"
    not_a_database.write_bytes(b"this is not SQLite" * 100)

    with pytest.raises(sqlite3.Error):
        backup_database(not_a_database, tmp_path / "backups", TODAY)

    assert list((tmp_path / "backups").iterdir()) == []


class TestCommand:
    def test_copies_the_configured_database(
        self, database: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOTTUB_DATABASE_PATH", str(database))
        monkeypatch.setenv("TZ", "UTC")
        monkeypatch.delenv("HOTTUB_CONFIG", raising=False)

        status = backup.main()

        today = datetime.now(UTC).astimezone().date()
        assert status == 0
        assert (database.parent / "backups" / f"hottub-{today:%Y-%m-%d}.db").is_file()

    def test_reports_failure_with_a_nonzero_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOTTUB_DATABASE_PATH", str(tmp_path / "nowhere.db"))
        monkeypatch.setenv("TZ", "UTC")
        monkeypatch.delenv("HOTTUB_CONFIG", raising=False)

        assert backup.main() == 1

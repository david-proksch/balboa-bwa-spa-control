"""Nightly copies of the database.

The copy is made with SQLite's online backup, which is safe while the
service is running and writing: it yields a consistent snapshot, which a
plain file copy of a live database does not guarantee.

Copies are named for the day they were made (``hottub-2026-11-11.db``), so
running twice on one day replaces that day's copy, and only the newest few
are kept.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import date
from pathlib import Path

#: How many daily copies are kept.
COPIES_KEPT = 7

_PREFIX, _SUFFIX = "hottub-", ".db"


def backup_database(database: Path, directory: Path, day: date, *, keep: int = COPIES_KEPT) -> Path:
    """Copy ``database`` into ``directory`` and delete the oldest copies.

    Args:
        database: The live SQLite file.
        directory: Where the copies go; created if missing.
        day: The day the copy is named for.
        keep: How many copies to leave, newest first.

    Returns:
        The path of the copy just made.

    Raises:
        FileNotFoundError: If ``database`` does not exist. (SQLite would
            otherwise create an empty one and copy that.)
        sqlite3.Error: If the copy fails.
    """
    if not database.is_file():
        raise FileNotFoundError(f"There is no database at {database}")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{_PREFIX}{day:%Y-%m-%d}{_SUFFIX}"
    # Written under another name and renamed when complete, so a copy that
    # was interrupted is never mistaken for a good one.
    partial = target.with_suffix(".partial")
    partial.unlink(missing_ok=True)
    try:
        with (
            contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as source,
            contextlib.closing(sqlite3.connect(partial)) as copy,
        ):
            source.backup(copy)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)

    # The names sort by date, so the oldest come first.
    copies = sorted(directory.glob(f"{_PREFIX}*{_SUFFIX}"))
    for old in copies[: max(0, len(copies) - keep)]:
        old.unlink()
    return target

"""Command: copy the database to its backup directory.

Run by ``hottub-backup.timer`` once a night as ``python -m hottub.backup``.
It reads the same configuration as the service, so it copies whichever
database the service uses, into a ``backups`` directory beside it
(``/var/lib/hottub/backups`` on the Pi).

Restoring a copy is described in ``docs/operations.md``.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime

from hottub.config import load_config
from hottub.domain import HotTubError
from hottub.persistence.backup import backup_database

_LOGGER = logging.getLogger("hottub.backup")

#: The directory, beside the database, that holds the copies.
BACKUP_DIRECTORY_NAME = "backups"


def main() -> int:
    """Make tonight's copy. Returns the process exit status."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        config = load_config()
        database = config.database_path
        # Named for the local day, as the owner would look for it.
        copy = backup_database(
            database, database.parent / BACKUP_DIRECTORY_NAME, datetime.now().astimezone().date()
        )
    except (HotTubError, OSError, sqlite3.Error) as error:
        _LOGGER.error("The database was not backed up: %s", error)
        return 1
    _LOGGER.info("Database copied to %s (%d bytes)", copy, copy.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())

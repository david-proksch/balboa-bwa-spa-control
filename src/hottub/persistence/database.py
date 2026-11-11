"""Opening the SQLite database and bringing its schema up to date.

The database is one file (``/var/lib/hottub/hottub.db`` on the Pi). There is
no server and nothing to administer; a backup is a copy of the file.

Schema changes are versioned with Alembic. :func:`migrate` is called at
every start-up and applies whatever migrations the file has not seen yet,
so deploying a new release never needs a manual database step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event

#: Where the migration scripts live, inside the installed package.
MIGRATIONS_DIRECTORY = Path(__file__).parent / "migrations"


def open_database(path: Path) -> Engine:
    """Open (creating if necessary) the database file at ``path``.

    The parent directory must exist. The schema is not touched; call
    :func:`migrate` next.
    """
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        # Write-ahead logging: readers are never blocked by a write, and
        # with synchronous=NORMAL a commit does not force the SD card to
        # flush each time. A power cut can lose the last few commits but
        # cannot corrupt the file, which is the right trade for history
        # data on a Raspberry Pi.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def alembic_config() -> Config:
    """Return the Alembic configuration, built in code.

    There is deliberately no ``alembic.ini``: the application must be able
    to migrate from wherever it is installed. To create a new migration
    while developing, see ``migrations/README.md``.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY))
    return config


def migrate(engine: Engine) -> None:
    """Apply every migration the database has not yet had. Safe to repeat."""
    config = alembic_config()
    with engine.begin() as connection:
        # Picked up by migrations/env.py, so Alembic uses this connection
        # instead of opening its own.
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

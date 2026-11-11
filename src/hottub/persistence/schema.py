"""The database tables, as SQLAlchemy Core definitions.

This file is what the repositories query against. It does **not** create the
tables: the migrations in ``migrations/versions`` do, so that a database made
by an older release can be brought up to date in place. When you change a
table here, add a migration that makes the same change;
``tests/integration/test_database.py`` fails if the two disagree.

Tables are added phase by phase (PLAN.md, section 2); only the ones in use
exist.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Time,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp column that only accepts and returns aware UTC datetimes.

    SQLite has no timestamp type and SQLAlchemy stores datetimes there as
    text without an offset. Left alone, a value would come back naive and
    could be mistaken for local time. This type converts to UTC on the way
    in, refuses naive values, and marks values as UTC on the way out.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Convert an aware datetime to naive UTC for storage."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Refusing to store a datetime without a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Mark a stored value as the UTC instant it is."""
        return None if value is None else value.replace(tzinfo=UTC)


metadata = MetaData()

#: The owner's settings, one row per setting. Values are text; the
#: repository converts them. A key/value table means a new setting needs no
#: migration.
settings = Table(
    "settings",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
)

#: The audit log: one row per command sent to the spa. Append-only.
command_log = Table(
    "command_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("issued_at", UtcDateTime, nullable=False, index=True),
    Column("actor", String, nullable=False),
    Column("command", String, nullable=False),
    Column("argument", String, nullable=False),
    Column("succeeded", Boolean, nullable=False),
    Column("result", String, nullable=False),
)

#: When the heater and pumps ran. ``ended_at`` is the last moment the device
#: was confirmed running; ``is_open`` marks the interval of a device that
#: was still running when last seen (at most one per device).
run_interval = Table(
    "run_interval",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("device", String, nullable=False),
    Column("speed", String, nullable=True),
    Column("started_at", UtcDateTime, nullable=False),
    Column("ended_at", UtcDateTime, nullable=False, index=True),
    Column("is_open", Boolean, nullable=False),
)

#: One row a minute. ``water_f`` is null when the spa had no reading.
temperature_sample = Table(
    "temperature_sample",
    metadata,
    Column("at", UtcDateTime, primary_key=True),
    Column("water_f", Float, nullable=True),
    Column("set_f", Float, nullable=False),
)

#: The planned uses, at most one per calendar day. ``day`` and ``use_time``
#: are the owner's wall clock, not UTC: a plan for "6 PM on the 14th" means
#: that whatever the offset from UTC is on the day.
planned_use = Table(
    "planned_use",
    metadata,
    Column("day", Date, primary_key=True),
    Column("use_time", Time, nullable=False),
    Column("created_at", UtcDateTime, nullable=False),
    Column("heating_started_at", UtcDateTime, nullable=True),
)

#: The watchdog's alerts. ``cleared_at`` is null while an alert is active.
alert = Table(
    "alert",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String, nullable=False),
    Column("severity", String, nullable=False),
    Column("message", String, nullable=False),
    Column("raised_at", UtcDateTime, nullable=False, index=True),
    Column("cleared_at", UtcDateTime, nullable=True),
)

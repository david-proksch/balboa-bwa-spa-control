"""SQLite implementations of the domain's repository interfaces.

Pattern: Repository (Fowler, PoEAA). These classes are the only code that
builds SQL. Each method is one short transaction; nothing is cached, so
what a method returns is what is in the file.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, Row, delete, func, insert, literal, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from hottub.domain import (
    Actor,
    Alert,
    AlertKind,
    CommandRecord,
    Device,
    PlannedUse,
    PumpSpeed,
    RunInterval,
    Severity,
    SpaFault,
    Temperature,
    TemperatureBand,
    TemperatureSample,
)
from hottub.persistence.schema import (
    UtcDateTime,
    alert,
    command_log,
    planned_use,
    run_interval,
    settings,
    temperature_sample,
)

_FLOOR_KEY = "floor_f"
_CEILING_KEY = "ceiling_f"
_WEEKEND_READY_KEY = "weekend_ready"
_REST_TO_RESTORE_KEY = "rest_to_restore"
_USE_DURATION_KEY = "use_duration_minutes"
_LAST_FAULT_KEY = "last_fault_seen"
_TRUE, _FALSE = "true", "false"


class SqlSettingsRepository:
    """Implements :class:`hottub.domain.SettingsRepository` over SQLite."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, whose schema must already be migrated."""
        self._engine = engine

    def load_band(self) -> TemperatureBand | None:
        """Return the stored floor and ceiling, or ``None`` if never set."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(settings.c.key, settings.c.value).where(
                    settings.c.key.in_([_FLOOR_KEY, _CEILING_KEY])
                )
            )
            values = {key: float(value) for key, value in rows}
        if _FLOOR_KEY not in values or _CEILING_KEY not in values:
            return None
        return TemperatureBand(
            floor=Temperature(values[_FLOOR_KEY]), ceiling=Temperature(values[_CEILING_KEY])
        )

    def save_band(self, band: TemperatureBand) -> None:
        """Store the floor and ceiling together, replacing earlier values."""
        rows = [
            {"key": _FLOOR_KEY, "value": repr(band.floor.fahrenheit)},
            {"key": _CEILING_KEY, "value": repr(band.ceiling.fahrenheit)},
        ]
        statement = sqlite_insert(settings)
        statement = statement.on_conflict_do_update(
            index_elements=[settings.c.key], set_={"value": statement.excluded.value}
        )
        # One transaction, so a reader never sees a new floor with an old
        # ceiling.
        with self._engine.begin() as connection:
            connection.execute(statement, rows)


class SqlCommandLog:
    """Implements :class:`hottub.domain.CommandLog` over SQLite."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, whose schema must already be migrated."""
        self._engine = engine

    def append(self, record: CommandRecord) -> None:
        """Add a record."""
        with self._engine.begin() as connection:
            connection.execute(
                insert(command_log).values(
                    issued_at=record.issued_at,
                    actor=record.actor.value,
                    command=record.command,
                    argument=record.argument,
                    succeeded=record.succeeded,
                    result=record.result,
                )
            )

    def recent(self, limit: int) -> Sequence[CommandRecord]:
        """Return up to ``limit`` records, newest first."""
        statement = (
            select(command_log)
            # The id breaks ties between commands issued in the same instant.
            .order_by(command_log.c.issued_at.desc(), command_log.c.id.desc())
            .limit(limit)
        )
        with self._engine.connect() as connection:
            return [
                CommandRecord(
                    issued_at=row.issued_at,
                    actor=Actor(row.actor),
                    command=row.command,
                    argument=row.argument,
                    succeeded=row.succeeded,
                    result=row.result,
                )
                for row in connection.execute(statement)
            ]

    def delete_before(self, instant: datetime) -> None:
        """Delete records issued before ``instant``."""
        with self._engine.begin() as connection:
            connection.execute(delete(command_log).where(command_log.c.issued_at < instant))


class SqlRunIntervalRepository:
    """Implements :class:`hottub.domain.RunIntervalRepository` over SQLite."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, whose schema must already be migrated."""
        self._engine = engine

    def start(self, device: Device, speed: PumpSpeed | None, at: datetime) -> None:
        """Open an interval for ``device`` starting, and so far ending, at ``at``."""
        with self._engine.begin() as connection:
            connection.execute(
                insert(run_interval).values(
                    device=device.value,
                    speed=None if speed is None else speed.value,
                    started_at=at,
                    ended_at=at,
                    is_open=True,
                )
            )

    def extend_open(self, until: datetime) -> None:
        """Move the end of every open interval forward to ``until``."""
        with self._engine.begin() as connection:
            connection.execute(
                update(run_interval)
                .where(run_interval.c.is_open, run_interval.c.ended_at < until)
                .values(ended_at=until)
            )

    def finish(self, device: Device, at: datetime) -> None:
        """Close the open interval of ``device``, ending it at ``at``."""
        with self._engine.begin() as connection:
            connection.execute(
                update(run_interval)
                .where(run_interval.c.is_open, run_interval.c.device == device.value)
                # Never before its own start, whatever clock produced ``at``.
                .values(
                    ended_at=func.max(run_interval.c.started_at, literal(at, UtcDateTime())),
                    is_open=False,
                )
            )

    def finish_all(self) -> None:
        """Close every open interval where its end currently stands."""
        with self._engine.begin() as connection:
            connection.execute(
                update(run_interval).where(run_interval.c.is_open).values(is_open=False)
            )

    def overlapping(self, start: datetime, end: datetime) -> Sequence[RunInterval]:
        """Return the intervals, open or closed, that overlap the window."""
        statement = (
            select(run_interval)
            .where(run_interval.c.ended_at > start, run_interval.c.started_at < end)
            .order_by(run_interval.c.started_at, run_interval.c.id)
        )
        with self._engine.connect() as connection:
            return [
                RunInterval(
                    device=Device(row.device),
                    speed=None if row.speed is None else PumpSpeed(row.speed),
                    started_at=row.started_at,
                    ended_at=row.ended_at,
                )
                for row in connection.execute(statement)
            ]

    def delete_before(self, instant: datetime) -> None:
        """Delete closed intervals that ended before ``instant``."""
        with self._engine.begin() as connection:
            connection.execute(
                delete(run_interval).where(
                    run_interval.c.ended_at < instant, ~run_interval.c.is_open
                )
            )


class SqlTemperatureSampleRepository:
    """Implements :class:`hottub.domain.TemperatureSampleRepository` over SQLite."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, whose schema must already be migrated."""
        self._engine = engine

    def add(self, sample: TemperatureSample) -> None:
        """Store a sample; a second sample for the same instant replaces the first."""
        statement = sqlite_insert(temperature_sample).values(
            at=sample.at,
            water_f=None if sample.water is None else sample.water.fahrenheit,
            set_f=sample.set_temperature.fahrenheit,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[temperature_sample.c.at],
            set_={"water_f": statement.excluded.water_f, "set_f": statement.excluded.set_f},
        )
        with self._engine.begin() as connection:
            connection.execute(statement)

    def between(self, start: datetime, end: datetime) -> Sequence[TemperatureSample]:
        """Return the samples taken from ``start`` to ``end``, oldest first."""
        statement = (
            select(temperature_sample)
            .where(temperature_sample.c.at >= start, temperature_sample.c.at <= end)
            .order_by(temperature_sample.c.at)
        )
        with self._engine.connect() as connection:
            return [
                TemperatureSample(
                    at=row.at,
                    water=None if row.water_f is None else Temperature(row.water_f),
                    set_temperature=Temperature(row.set_f),
                )
                for row in connection.execute(statement)
            ]

    def earliest(self) -> datetime | None:
        """Return when the oldest stored sample was taken, if there is one."""
        with self._engine.connect() as connection:
            earliest: datetime | None = connection.execute(
                select(temperature_sample.c.at).order_by(temperature_sample.c.at).limit(1)
            ).scalar()
        return earliest

    def delete_before(self, instant: datetime) -> None:
        """Delete samples taken before ``instant``."""
        with self._engine.begin() as connection:
            connection.execute(delete(temperature_sample).where(temperature_sample.c.at < instant))


class SqlPlanRepository:
    """Implements :class:`hottub.domain.PlanRepository` over SQLite.

    The planned uses have their own table. The two switches are rows of
    the ``settings`` table, stored as ``"true"`` or ``"false"``.
    """

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, whose schema must already be migrated."""
        self._engine = engine

    def planned_use(self, day: date) -> PlannedUse | None:
        """Return the use planned for ``day``, if there is one."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(planned_use).where(planned_use.c.day == day)
            ).one_or_none()
        return None if row is None else _planned_use(row)

    def planned_uses_between(self, first_day: date, last_day: date) -> Sequence[PlannedUse]:
        """Return the uses planned from ``first_day`` to ``last_day``, earliest first."""
        statement = (
            select(planned_use)
            .where(planned_use.c.day >= first_day, planned_use.c.day <= last_day)
            .order_by(planned_use.c.day)
        )
        with self._engine.connect() as connection:
            return [_planned_use(row) for row in connection.execute(statement)]

    def save_planned_use(self, use: PlannedUse) -> None:
        """Store ``use``, replacing any other plan for the same day."""
        statement = sqlite_insert(planned_use).values(
            day=use.day,
            use_time=use.use_time,
            created_at=use.created_at,
            heating_started_at=use.heating_started_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[planned_use.c.day],
            set_={
                "use_time": statement.excluded.use_time,
                "created_at": statement.excluded.created_at,
                "heating_started_at": statement.excluded.heating_started_at,
            },
        )
        with self._engine.begin() as connection:
            connection.execute(statement)

    def mark_heating_started(self, day: date, at: datetime) -> None:
        """Record that heating for the use on ``day`` began at ``at``, if not yet recorded."""
        with self._engine.begin() as connection:
            connection.execute(
                update(planned_use)
                .where(planned_use.c.day == day, planned_use.c.heating_started_at.is_(None))
                .values(heating_started_at=at)
            )

    def delete_planned_use(self, day: date) -> None:
        """Remove the plan for ``day``; does nothing if there is none."""
        with self._engine.begin() as connection:
            connection.execute(delete(planned_use).where(planned_use.c.day == day))

    def delete_planned_uses_before(self, day: date) -> None:
        """Remove the plans for days earlier than ``day``."""
        with self._engine.begin() as connection:
            connection.execute(delete(planned_use).where(planned_use.c.day < day))

    def weekend_ready(self) -> bool:
        """Return whether weekend keep-ready is switched on (off until set)."""
        return self._flag(_WEEKEND_READY_KEY)

    def set_weekend_ready(self, enabled: bool) -> None:
        """Switch weekend keep-ready on or off."""
        self._set_flag(_WEEKEND_READY_KEY, enabled)

    def use_duration(self) -> timedelta | None:
        """Return how long a planned use holds the ceiling, if the owner has chosen."""
        with self._engine.connect() as connection:
            minutes = connection.execute(
                select(settings.c.value).where(settings.c.key == _USE_DURATION_KEY)
            ).scalar()
        return None if minutes is None else timedelta(minutes=int(minutes))

    def set_use_duration(self, duration: timedelta) -> None:
        """Store how long a planned use holds the ceiling, to the whole minute."""
        self._set(_USE_DURATION_KEY, str(round(duration.total_seconds() / 60)))

    def rest_to_restore(self) -> bool:
        """Return whether the scheduler owes the spa a return to Rest mode."""
        return self._flag(_REST_TO_RESTORE_KEY)

    def set_rest_to_restore(self, owed: bool) -> None:
        """Record whether a return to Rest mode is owed."""
        self._set_flag(_REST_TO_RESTORE_KEY, owed)

    def _flag(self, key: str) -> bool:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(settings.c.value).where(settings.c.key == key)
            ).scalar()
        return value == _TRUE

    def _set_flag(self, key: str, value: bool) -> None:
        self._set(key, _TRUE if value else _FALSE)

    def _set(self, key: str, value: str) -> None:
        statement = sqlite_insert(settings).values(key=key, value=value)
        statement = statement.on_conflict_do_update(
            index_elements=[settings.c.key], set_={"value": statement.excluded.value}
        )
        with self._engine.begin() as connection:
            connection.execute(statement)


def _planned_use(row: Row[Any]) -> PlannedUse:
    return PlannedUse(
        day=row.day,
        use_time=row.use_time,
        created_at=row.created_at,
        heating_started_at=row.heating_started_at,
    )


class SqlAlertRepository:
    """Implements :class:`hottub.domain.AlertRepository` over SQLite.

    The alerts have their own table. The last fault dealt with is one row
    of the ``settings`` table, stored as JSON.
    """

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, whose schema must already be migrated."""
        self._engine = engine

    def add(self, new: Alert) -> Alert:
        """Store a new alert and return it with its number filled in."""
        with self._engine.begin() as connection:
            result = connection.execute(
                insert(alert).values(
                    kind=new.kind.value,
                    severity=new.severity.value,
                    message=new.message,
                    raised_at=new.raised_at,
                    cleared_at=new.cleared_at,
                )
            )
        key = result.inserted_primary_key
        assert key is not None
        return replace(new, id=key[0])

    def get(self, alert_id: int) -> Alert | None:
        """Return the alert with that number, if there is one."""
        with self._engine.connect() as connection:
            row = connection.execute(select(alert).where(alert.c.id == alert_id)).first()
        return None if row is None else _alert(row)

    def active(self) -> Sequence[Alert]:
        """Return the alerts that have not cleared, oldest first."""
        statement = (
            select(alert)
            .where(alert.c.cleared_at.is_(None))
            .order_by(alert.c.raised_at, alert.c.id)
        )
        with self._engine.connect() as connection:
            return [_alert(row) for row in connection.execute(statement)]

    def escalate(self, alert_id: int, severity: Severity, message: str) -> None:
        """Change an alert's severity and message."""
        with self._engine.begin() as connection:
            connection.execute(
                update(alert)
                .where(alert.c.id == alert_id)
                .values(severity=severity.value, message=message)
            )

    def clear(self, alert_id: int, at: datetime) -> None:
        """Mark an alert as over at ``at``, unless it already is."""
        with self._engine.begin() as connection:
            connection.execute(
                update(alert)
                .where(alert.c.id == alert_id, alert.c.cleared_at.is_(None))
                .values(cleared_at=at)
            )

    def raised_since(self, instant: datetime) -> Sequence[Alert]:
        """Return every alert raised at or after ``instant``, newest first."""
        statement = (
            select(alert)
            .where(alert.c.raised_at >= instant)
            .order_by(alert.c.raised_at.desc(), alert.c.id.desc())
        )
        with self._engine.connect() as connection:
            return [_alert(row) for row in connection.execute(statement)]

    def delete_cleared_before(self, instant: datetime) -> None:
        """Delete alerts that cleared before ``instant``; active ones are kept."""
        with self._engine.begin() as connection:
            connection.execute(delete(alert).where(alert.c.cleared_at < instant))

    def last_fault_seen(self) -> SpaFault | None:
        """Return the newest fault-log entry already dealt with, if any."""
        with self._engine.connect() as connection:
            stored = connection.execute(
                select(settings.c.value).where(settings.c.key == _LAST_FAULT_KEY)
            ).scalar()
        if stored is None:
            return None
        fields = json.loads(stored)
        return SpaFault(
            code=fields["code"],
            message=fields["message"],
            occurred_at=datetime.fromisoformat(fields["occurred_at"]),
        )

    def remember_fault(self, fault: SpaFault) -> None:
        """Note ``fault`` as dealt with."""
        stored = json.dumps(
            {
                "code": fault.code,
                "message": fault.message,
                "occurred_at": fault.occurred_at.astimezone(UTC).isoformat(),
            }
        )
        statement = sqlite_insert(settings).values(key=_LAST_FAULT_KEY, value=stored)
        statement = statement.on_conflict_do_update(
            index_elements=[settings.c.key], set_={"value": statement.excluded.value}
        )
        with self._engine.begin() as connection:
            connection.execute(statement)


def _alert(row: Row[Any]) -> Alert:
    return Alert(
        kind=AlertKind(row.kind),
        severity=Severity(row.severity),
        message=row.message,
        raised_at=row.raised_at,
        cleared_at=row.cleared_at,
        id=row.id,
    )

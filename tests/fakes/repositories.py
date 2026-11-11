"""In-memory versions of the repository interfaces in ``hottub.domain``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from hottub.domain import (
    Alert,
    CommandRecord,
    Device,
    PlannedUse,
    PumpSpeed,
    RunInterval,
    Severity,
    SpaFault,
    TemperatureBand,
    TemperatureSample,
)


class InMemorySettingsRepository:
    """Implements ``hottub.domain.SettingsRepository`` with an attribute."""

    def __init__(self, band: TemperatureBand | None = None) -> None:
        self._band = band

    def load_band(self) -> TemperatureBand | None:
        return self._band

    def save_band(self, band: TemperatureBand) -> None:
        self._band = band


class InMemoryCommandLog:
    """Implements ``hottub.domain.CommandLog`` with a list."""

    def __init__(self) -> None:
        self.records: list[CommandRecord] = []
        """Every record, oldest first, for tests to inspect."""

    def append(self, record: CommandRecord) -> None:
        self.records.append(record)

    def recent(self, limit: int) -> Sequence[CommandRecord]:
        return list(reversed(self.records))[:limit]

    def delete_before(self, instant: datetime) -> None:
        self.records = [record for record in self.records if record.issued_at >= instant]


@dataclass
class _StoredInterval:
    device: Device
    speed: PumpSpeed | None
    started_at: datetime
    ended_at: datetime
    is_open: bool

    def as_interval(self) -> RunInterval:
        return RunInterval(self.device, self.speed, self.started_at, self.ended_at)


class InMemoryRunIntervalRepository:
    """Implements ``hottub.domain.RunIntervalRepository`` with a list."""

    def __init__(self, intervals: Sequence[RunInterval] = ()) -> None:
        """Start with ``intervals`` already on record, all closed."""
        self._rows = [
            _StoredInterval(i.device, i.speed, i.started_at, i.ended_at, is_open=False)
            for i in intervals
        ]

    @property
    def all(self) -> list[RunInterval]:
        """Every interval in the order it was started, for tests to inspect."""
        return [row.as_interval() for row in self._rows]

    @property
    def open_devices(self) -> set[Device]:
        """The devices that have an open interval, for tests to inspect."""
        return {row.device for row in self._rows if row.is_open}

    def start(self, device: Device, speed: PumpSpeed | None, at: datetime) -> None:
        self._rows.append(_StoredInterval(device, speed, at, at, is_open=True))

    def extend_open(self, until: datetime) -> None:
        for row in self._rows:
            if row.is_open and row.ended_at < until:
                row.ended_at = until

    def finish(self, device: Device, at: datetime) -> None:
        for row in self._rows:
            if row.is_open and row.device is device:
                row.ended_at = max(row.started_at, at)
                row.is_open = False

    def finish_all(self) -> None:
        for row in self._rows:
            row.is_open = False

    def overlapping(self, start: datetime, end: datetime) -> Sequence[RunInterval]:
        return [
            row.as_interval()
            for row in sorted(self._rows, key=lambda row: row.started_at)
            if row.ended_at > start and row.started_at < end
        ]

    def delete_before(self, instant: datetime) -> None:
        self._rows = [row for row in self._rows if row.is_open or row.ended_at >= instant]


class InMemoryTemperatureSampleRepository:
    """Implements ``hottub.domain.TemperatureSampleRepository`` with a dictionary."""

    def __init__(self, samples: Sequence[TemperatureSample] = ()) -> None:
        """Start with ``samples`` already on record."""
        self._samples = {sample.at: sample for sample in samples}

    @property
    def all(self) -> list[TemperatureSample]:
        """Every sample, oldest first, for tests to inspect."""
        return [self._samples[at] for at in sorted(self._samples)]

    def add(self, sample: TemperatureSample) -> None:
        self._samples[sample.at] = sample

    def between(self, start: datetime, end: datetime) -> Sequence[TemperatureSample]:
        return [sample for sample in self.all if start <= sample.at <= end]

    def earliest(self) -> datetime | None:
        return min(self._samples, default=None)

    def delete_before(self, instant: datetime) -> None:
        self._samples = {at: s for at, s in self._samples.items() if at >= instant}


class InMemoryPlanRepository:
    """Implements ``hottub.domain.PlanRepository`` with a dictionary and two flags."""

    def __init__(self) -> None:
        self._uses: dict[date, PlannedUse] = {}
        self._weekend_ready = False
        self._rest_to_restore = False
        self._use_duration: timedelta | None = None

    def planned_use(self, day: date) -> PlannedUse | None:
        return self._uses.get(day)

    def planned_uses_between(self, first_day: date, last_day: date) -> Sequence[PlannedUse]:
        return [self._uses[day] for day in sorted(self._uses) if first_day <= day <= last_day]

    def save_planned_use(self, use: PlannedUse) -> None:
        self._uses[use.day] = use

    def mark_heating_started(self, day: date, at: datetime) -> None:
        use = self._uses.get(day)
        if use is not None and use.heating_started_at is None:
            self._uses[day] = replace(use, heating_started_at=at)

    def delete_planned_use(self, day: date) -> None:
        self._uses.pop(day, None)

    def delete_planned_uses_before(self, day: date) -> None:
        self._uses = {d: use for d, use in self._uses.items() if d >= day}

    def weekend_ready(self) -> bool:
        return self._weekend_ready

    def set_weekend_ready(self, enabled: bool) -> None:
        self._weekend_ready = enabled

    def use_duration(self) -> timedelta | None:
        return self._use_duration

    def set_use_duration(self, duration: timedelta) -> None:
        self._use_duration = duration

    def rest_to_restore(self) -> bool:
        return self._rest_to_restore

    def set_rest_to_restore(self, owed: bool) -> None:
        self._rest_to_restore = owed


class InMemoryAlertRepository:
    """Implements ``hottub.domain.AlertRepository`` with a dictionary."""

    def __init__(self) -> None:
        self._alerts: dict[int, Alert] = {}
        self._last_fault: SpaFault | None = None

    @property
    def all(self) -> list[Alert]:
        """Every alert in the order it was raised, for tests to inspect."""
        return list(self._alerts.values())

    def add(self, alert: Alert) -> Alert:
        stored = replace(alert, id=len(self._alerts) + 1)
        assert stored.id is not None
        self._alerts[stored.id] = stored
        return stored

    def get(self, alert_id: int) -> Alert | None:
        return self._alerts.get(alert_id)

    def active(self) -> Sequence[Alert]:
        return [alert for alert in self._alerts.values() if alert.active]

    def escalate(self, alert_id: int, severity: Severity, message: str) -> None:
        self._alerts[alert_id] = replace(self._alerts[alert_id], severity=severity, message=message)

    def clear(self, alert_id: int, at: datetime) -> None:
        if self._alerts[alert_id].active:
            self._alerts[alert_id] = replace(self._alerts[alert_id], cleared_at=at)

    def raised_since(self, instant: datetime) -> Sequence[Alert]:
        return [a for a in reversed(self._alerts.values()) if a.raised_at >= instant]

    def delete_cleared_before(self, instant: datetime) -> None:
        self._alerts = {
            alert_id: alert
            for alert_id, alert in self._alerts.items()
            if alert.cleared_at is None or alert.cleared_at >= instant
        }

    def last_fault_seen(self) -> SpaFault | None:
        return self._last_fault

    def remember_fault(self, fault: SpaFault) -> None:
        self._last_fault = fault

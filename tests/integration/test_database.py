"""The SQLite database: migrations and repositories, against a real file."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect

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
from hottub.persistence.schema import metadata

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "test.db")
    migrate(engine)
    yield engine
    engine.dispose()


def record(issued_at: datetime = NOON, argument: str = "high") -> CommandRecord:
    return CommandRecord(
        issued_at=issued_at,
        actor=Actor.USER,
        command="set_pump1",
        argument=argument,
        succeeded=True,
        result=f"pump 1 is {argument}",
    )


class TestMigrations:
    def test_create_the_tables(self, engine: Engine) -> None:
        expected = {
            "settings",
            "command_log",
            "run_interval",
            "temperature_sample",
            "planned_use",
            "alert",
        }

        assert expected <= set(inspect(engine).get_table_names())

    def test_produce_exactly_the_schema_the_code_expects(self, engine: Engine) -> None:
        # If this fails, schema.py was changed without a matching migration
        # (or the other way round). See persistence/migrations/README.md.
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), metadata)

        assert differences == []

    def test_can_be_run_again_on_an_up_to_date_database(self, engine: Engine) -> None:
        SqlCommandLog(engine).append(record())

        migrate(engine)

        assert len(SqlCommandLog(engine).recent(10)) == 1

    def test_data_survives_closing_and_reopening_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "kept.db"
        first = open_database(path)
        migrate(first)
        SqlSettingsRepository(first).save_band(TemperatureBand(Temperature(80), Temperature(102)))
        first.dispose()

        second = open_database(path)
        migrate(second)

        assert SqlSettingsRepository(second).load_band() == TemperatureBand(
            Temperature(80), Temperature(102)
        )
        second.dispose()


class TestSettingsRepository:
    def test_has_no_band_until_one_is_saved(self, engine: Engine) -> None:
        assert SqlSettingsRepository(engine).load_band() is None

    def test_returns_the_band_last_saved(self, engine: Engine) -> None:
        repository = SqlSettingsRepository(engine)

        repository.save_band(TemperatureBand(Temperature(85), Temperature(99)))
        repository.save_band(TemperatureBand(Temperature(88.5), Temperature(101)))

        assert repository.load_band() == TemperatureBand(Temperature(88.5), Temperature(101))


class TestCommandLog:
    def test_returns_records_exactly_as_appended(self, engine: Engine) -> None:
        log = SqlCommandLog(engine)
        failed = CommandRecord(
            issued_at=NOON,
            actor=Actor.SCHEDULER,
            command="set_temperature",
            argument="99 °F",
            succeeded=False,
            result="The spa is not connected",
        )

        log.append(failed)

        assert list(log.recent(10)) == [failed]

    def test_returns_the_newest_first_up_to_the_limit(self, engine: Engine) -> None:
        log = SqlCommandLog(engine)
        log.append(record(NOON + timedelta(minutes=1), "low"))
        log.append(record(NOON + timedelta(minutes=3), "off"))
        log.append(record(NOON + timedelta(minutes=2), "high"))

        assert [entry.argument for entry in log.recent(2)] == ["off", "high"]

    def test_orders_records_from_the_same_instant_by_insertion(self, engine: Engine) -> None:
        log = SqlCommandLog(engine)
        log.append(record(NOON, "low"))
        log.append(record(NOON, "high"))

        assert [entry.argument for entry in log.recent(2)] == ["high", "low"]

    def test_stores_times_as_utc_instants(self, engine: Engine) -> None:
        log = SqlCommandLog(engine)
        seven_am_in_new_york = datetime(2026, 1, 5, 7, 0, tzinfo=timezone(timedelta(hours=-5)))

        log.append(record(seven_am_in_new_york))

        [entry] = log.recent(1)
        assert entry.issued_at == NOON
        assert entry.issued_at.utcoffset() == timedelta(0)

    def test_refuses_a_time_without_a_timezone(self, engine: Engine) -> None:
        with pytest.raises(Exception, match="without a timezone"):
            SqlCommandLog(engine).append(record(datetime(2026, 1, 5, 12, 0)))

    def test_deletes_records_older_than_a_given_time(self, engine: Engine) -> None:
        log = SqlCommandLog(engine)
        log.append(record(NOON - timedelta(days=91), "low"))
        log.append(record(NOON, "high"))

        log.delete_before(NOON - timedelta(days=90))

        assert [entry.argument for entry in log.recent(10)] == ["high"]


MINUTE = timedelta(minutes=1)
ALL_TIME = (NOON - timedelta(days=365), NOON + timedelta(days=365))


class TestRunIntervalRepository:
    def test_an_open_interval_grows_as_it_is_extended(self, engine: Engine) -> None:
        intervals = SqlRunIntervalRepository(engine)

        intervals.start(Device.PUMP1, PumpSpeed.LOW, NOON)
        intervals.extend_open(NOON + MINUTE)

        assert intervals.overlapping(*ALL_TIME) == [
            RunInterval(Device.PUMP1, PumpSpeed.LOW, NOON, NOON + MINUTE)
        ]

    def test_extending_moves_only_open_intervals_and_only_forward(self, engine: Engine) -> None:
        intervals = SqlRunIntervalRepository(engine)
        intervals.start(Device.PUMP2, None, NOON)
        intervals.finish(Device.PUMP2, NOON + 5 * MINUTE)
        intervals.start(Device.HEATER, None, NOON + 10 * MINUTE)
        intervals.extend_open(NOON + 20 * MINUTE)

        intervals.extend_open(NOON + 15 * MINUTE)

        assert intervals.overlapping(*ALL_TIME) == [
            RunInterval(Device.PUMP2, None, NOON, NOON + 5 * MINUTE),
            RunInterval(Device.HEATER, None, NOON + 10 * MINUTE, NOON + 20 * MINUTE),
        ]

    def test_finishing_closes_only_the_named_device(self, engine: Engine) -> None:
        intervals = SqlRunIntervalRepository(engine)
        intervals.start(Device.HEATER, None, NOON)
        intervals.start(Device.PUMP1, PumpSpeed.LOW, NOON)

        intervals.finish(Device.HEATER, NOON + 30 * MINUTE)
        intervals.extend_open(NOON + 60 * MINUTE)

        assert intervals.overlapping(*ALL_TIME) == [
            RunInterval(Device.HEATER, None, NOON, NOON + 30 * MINUTE),
            RunInterval(Device.PUMP1, PumpSpeed.LOW, NOON, NOON + 60 * MINUTE),
        ]

    def test_an_interval_never_ends_before_it_starts(self, engine: Engine) -> None:
        intervals = SqlRunIntervalRepository(engine)
        intervals.start(Device.PUMP2, None, NOON)

        intervals.finish(Device.PUMP2, NOON - MINUTE)

        assert intervals.overlapping(*ALL_TIME) == [RunInterval(Device.PUMP2, None, NOON, NOON)]

    def test_finishing_all_leaves_each_end_where_it_stood(self, engine: Engine) -> None:
        intervals = SqlRunIntervalRepository(engine)
        intervals.start(Device.HEATER, None, NOON)
        intervals.start(Device.PUMP2, None, NOON)
        intervals.extend_open(NOON + 10 * MINUTE)

        intervals.finish_all()
        intervals.extend_open(NOON + 60 * MINUTE)

        assert [interval.ended_at for interval in intervals.overlapping(*ALL_TIME)] == [
            NOON + 10 * MINUTE,
            NOON + 10 * MINUTE,
        ]

    def test_returns_what_overlaps_the_window(self, engine: Engine) -> None:
        intervals = SqlRunIntervalRepository(engine)
        for start_minute, end_minute in [(0, 10), (20, 30), (40, 50), (60, 70)]:
            intervals.start(Device.PUMP2, None, NOON + start_minute * MINUTE)
            intervals.finish(Device.PUMP2, NOON + end_minute * MINUTE)

        found = intervals.overlapping(NOON + 25 * MINUTE, NOON + 60 * MINUTE)

        # The second straddles the start; the fourth begins as the window ends.
        assert [interval.started_at for interval in found] == [
            NOON + 20 * MINUTE,
            NOON + 40 * MINUTE,
        ]

    def test_deletes_old_closed_intervals_but_never_an_open_one(self, engine: Engine) -> None:
        intervals = SqlRunIntervalRepository(engine)
        intervals.start(Device.PUMP2, None, NOON)
        intervals.finish(Device.PUMP2, NOON + 10 * MINUTE)
        intervals.start(Device.PUMP1, PumpSpeed.HIGH, NOON + 20 * MINUTE)
        intervals.finish(Device.PUMP1, NOON + 30 * MINUTE)
        intervals.start(Device.HEATER, None, NOON)

        intervals.delete_before(NOON + 15 * MINUTE)

        intervals.extend_open(NOON + MINUTE)
        assert [interval.device for interval in intervals.overlapping(*ALL_TIME)] == [
            Device.HEATER,
            Device.PUMP1,
        ]


def sample(at: datetime, water: float | None = 97.0) -> TemperatureSample:
    return TemperatureSample(
        at=at,
        water=None if water is None else Temperature(water),
        set_temperature=Temperature(99.0),
    )


class TestTemperatureSampleRepository:
    def test_returns_samples_exactly_as_stored_oldest_first(self, engine: Engine) -> None:
        samples = SqlTemperatureSampleRepository(engine)
        samples.add(sample(NOON + MINUTE, 98.0))
        samples.add(sample(NOON, None))

        assert samples.between(NOON, NOON + MINUTE) == [
            sample(NOON, None),
            sample(NOON + MINUTE, 98.0),
        ]

    def test_returns_only_the_samples_in_the_period_asked_for(self, engine: Engine) -> None:
        samples = SqlTemperatureSampleRepository(engine)
        for minute in range(10):
            samples.add(sample(NOON + minute * MINUTE))

        found = samples.between(NOON + 3 * MINUTE, NOON + 5 * MINUTE)

        assert [s.at for s in found] == [NOON + n * MINUTE for n in (3, 4, 5)]

    def test_a_second_sample_for_the_same_instant_replaces_the_first(self, engine: Engine) -> None:
        samples = SqlTemperatureSampleRepository(engine)
        samples.add(sample(NOON, 97.0))

        samples.add(sample(NOON, 98.0))

        assert samples.between(NOON, NOON) == [sample(NOON, 98.0)]

    def test_knows_how_far_back_it_goes(self, engine: Engine) -> None:
        samples = SqlTemperatureSampleRepository(engine)
        assert samples.earliest() is None

        samples.add(sample(NOON))
        samples.add(sample(NOON - timedelta(days=3)))

        assert samples.earliest() == NOON - timedelta(days=3)

    def test_deletes_samples_older_than_a_given_time(self, engine: Engine) -> None:
        samples = SqlTemperatureSampleRepository(engine)
        samples.add(sample(NOON - timedelta(days=91)))
        samples.add(sample(NOON))

        samples.delete_before(NOON - timedelta(days=90))

        assert samples.earliest() == NOON


TUESDAY = date(2026, 1, 6)


def planned(day: date = TUESDAY, use_time: time = time(18, 0)) -> PlannedUse:
    return PlannedUse(day=day, use_time=use_time, created_at=NOON)


class TestPlanRepository:
    def test_has_no_plan_until_one_is_saved(self, engine: Engine) -> None:
        assert SqlPlanRepository(engine).planned_use(TUESDAY) is None

    def test_returns_a_plan_exactly_as_saved(self, engine: Engine) -> None:
        plans = SqlPlanRepository(engine)

        plans.save_planned_use(planned())

        assert plans.planned_use(TUESDAY) == planned()

    def test_a_second_plan_for_the_day_replaces_the_first(self, engine: Engine) -> None:
        plans = SqlPlanRepository(engine)
        plans.save_planned_use(planned())
        plans.mark_heating_started(TUESDAY, NOON)

        plans.save_planned_use(planned(use_time=time(21, 30)))

        # The new plan has not begun heating, whatever the old one had done.
        assert plans.planned_use(TUESDAY) == planned(use_time=time(21, 30))

    def test_the_start_of_heating_is_recorded_once(self, engine: Engine) -> None:
        plans = SqlPlanRepository(engine)
        plans.save_planned_use(planned())

        plans.mark_heating_started(TUESDAY, NOON)
        plans.mark_heating_started(TUESDAY, NOON + timedelta(minutes=1))

        stored = plans.planned_use(TUESDAY)
        assert stored is not None
        assert stored.heating_started_at == NOON

    def test_marking_a_day_with_no_plan_does_nothing(self, engine: Engine) -> None:
        plans = SqlPlanRepository(engine)

        plans.mark_heating_started(TUESDAY, NOON)

        assert plans.planned_use(TUESDAY) is None

    def test_returns_the_plans_for_a_run_of_days_in_order(self, engine: Engine) -> None:
        plans = SqlPlanRepository(engine)
        days = [TUESDAY + timedelta(days=offset) for offset in (3, 0, 1, 2)]
        for day in days:
            plans.save_planned_use(planned(day))

        found = plans.planned_uses_between(days[1], days[3])

        assert [use.day for use in found] == [days[1], days[2], days[3]]

    def test_deletes_one_plan_or_all_the_old_ones(self, engine: Engine) -> None:
        plans = SqlPlanRepository(engine)
        days = [TUESDAY + timedelta(days=offset) for offset in range(4)]
        for day in days:
            plans.save_planned_use(planned(day))

        plans.delete_planned_use(days[3])
        plans.delete_planned_use(days[3])  # already gone: not an error
        plans.delete_planned_uses_before(days[1])

        assert [use.day for use in plans.planned_uses_between(days[0], days[3])] == days[1:3]

    def test_the_switches_are_off_until_set_and_survive_reopening(self, tmp_path: Path) -> None:
        path = tmp_path / "plans.db"
        first = open_database(path)
        migrate(first)
        plans = SqlPlanRepository(first)
        assert (plans.weekend_ready(), plans.rest_to_restore()) == (False, False)
        plans.set_weekend_ready(True)
        plans.set_rest_to_restore(True)
        first.dispose()

        second = open_database(path)
        reopened = SqlPlanRepository(second)

        assert (reopened.weekend_ready(), reopened.rest_to_restore()) == (True, True)
        reopened.set_weekend_ready(False)
        assert (reopened.weekend_ready(), reopened.rest_to_restore()) == (False, True)
        second.dispose()

    def test_has_no_hold_time_until_the_owner_chooses_one(self, engine: Engine) -> None:
        plans = SqlPlanRepository(engine)
        assert plans.use_duration() is None

        plans.set_use_duration(timedelta(hours=3))
        plans.set_use_duration(timedelta(minutes=90))

        assert SqlPlanRepository(engine).use_duration() == timedelta(minutes=90)

    def test_the_switches_do_not_disturb_the_band(self, engine: Engine) -> None:
        band = TemperatureBand(floor=Temperature(88), ceiling=Temperature(101))
        SqlSettingsRepository(engine).save_band(band)

        SqlPlanRepository(engine).set_weekend_ready(True)

        assert SqlSettingsRepository(engine).load_band() == band


def cold(raised_at: datetime = NOON, severity: Severity = Severity.CRITICAL) -> Alert:
    return Alert(AlertKind.WATER_TOO_COLD, severity, "The water is at 80 °F.", raised_at)


class TestAlertRepository:
    def test_a_stored_alert_gets_a_number_and_comes_back_as_stored(self, engine: Engine) -> None:
        alerts = SqlAlertRepository(engine)

        first, second = alerts.add(cold()), alerts.add(cold(NOON + timedelta(minutes=1)))

        assert (first.id, second.id) == (1, 2)
        assert alerts.get(1) == first
        assert first == Alert(
            AlertKind.WATER_TOO_COLD, Severity.CRITICAL, "The water is at 80 °F.", NOON, None, 1
        )
        assert alerts.get(99) is None

    def test_active_alerts_are_those_not_cleared_oldest_first(self, engine: Engine) -> None:
        alerts = SqlAlertRepository(engine)
        late = alerts.add(cold(NOON + timedelta(hours=1)))
        early = alerts.add(cold(NOON))
        over = alerts.add(cold(NOON + timedelta(minutes=30)))
        assert over.id is not None

        alerts.clear(over.id, NOON + timedelta(hours=2))

        assert alerts.active() == [early, late]
        cleared = alerts.get(over.id)
        assert cleared is not None
        assert cleared.cleared_at == NOON + timedelta(hours=2)
        assert not cleared.active

    def test_clearing_twice_keeps_the_first_time(self, engine: Engine) -> None:
        alerts = SqlAlertRepository(engine)
        stored = alerts.add(cold())
        assert stored.id is not None

        alerts.clear(stored.id, NOON + timedelta(hours=1))
        alerts.clear(stored.id, NOON + timedelta(hours=5))

        again = alerts.get(stored.id)
        assert again is not None
        assert again.cleared_at == NOON + timedelta(hours=1)

    def test_an_alert_can_be_made_more_severe(self, engine: Engine) -> None:
        alerts = SqlAlertRepository(engine)
        stored = alerts.add(
            Alert(AlertKind.STALE_DATA, Severity.WARNING, "The spa has stopped reporting.", NOON)
        )
        assert stored.id is not None

        alerts.escalate(stored.id, Severity.CRITICAL, "The spa has not reported for 15 minutes.")

        worse = alerts.get(stored.id)
        assert worse is not None
        assert (worse.severity, worse.raised_at) == (Severity.CRITICAL, NOON)
        assert worse.message == "The spa has not reported for 15 minutes."

    def test_history_is_newest_first_from_a_given_time(self, engine: Engine) -> None:
        alerts = SqlAlertRepository(engine)
        stored = [alerts.add(cold(NOON + timedelta(days=day))) for day in range(4)]

        assert alerts.raised_since(NOON + timedelta(days=2)) == [stored[3], stored[2]]

    def test_deletes_old_cleared_alerts_but_never_an_active_one(self, engine: Engine) -> None:
        alerts = SqlAlertRepository(engine)
        old_active = alerts.add(cold(NOON - timedelta(days=200)))
        old_cleared = alerts.add(cold(NOON - timedelta(days=200)))
        recent_cleared = alerts.add(cold(NOON - timedelta(days=2)))
        assert old_cleared.id is not None and recent_cleared.id is not None
        alerts.clear(old_cleared.id, NOON - timedelta(days=199))
        alerts.clear(recent_cleared.id, NOON - timedelta(days=1))

        alerts.delete_cleared_before(NOON - timedelta(days=90))

        left = alerts.raised_since(NOON - timedelta(days=365))
        assert [alert.id for alert in left] == [recent_cleared.id, old_active.id]

    def test_remembers_the_last_fault_dealt_with(self, tmp_path: Path) -> None:
        path = tmp_path / "alerts.db"
        first = open_database(path)
        migrate(first)
        alerts = SqlAlertRepository(first)
        assert alerts.last_fault_seen() is None
        alerts.remember_fault(SpaFault(19, "Priming Mode", NOON - timedelta(days=8)))
        alerts.remember_fault(SpaFault(16, "The water flow is low", NOON))
        first.dispose()

        second = open_database(path)

        assert SqlAlertRepository(second).last_fault_seen() == SpaFault(
            16, "The water flow is low", NOON
        )
        second.dispose()

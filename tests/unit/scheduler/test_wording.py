"""How times are said to the owner."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from hottub.domain import local_instant
from hottub.scheduler.wording import clock_time

ZONE = ZoneInfo("America/Detroit")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return local_instant(date(2026, 1, day), time(hour, minute), ZONE)


NOW = local(6, 8)  # Tuesday 08:00


@pytest.mark.parametrize(
    ("instant", "text"),
    [
        (local(6, 0), "12:00 AM"),
        (local(6, 9, 5), "9:05 AM"),
        (local(6, 12), "12:00 PM"),
        (local(6, 14, 40), "2:40 PM"),
        (local(6, 23, 59), "11:59 PM"),
        (local(7, 0), "tomorrow 12:00 AM"),
        (local(7, 9), "tomorrow 9:00 AM"),
        (local(8, 9), "Thu 9:00 AM"),
        (local(9, 15), "Fri 3:00 PM"),
        (local(5, 20), "Mon 8:00 PM"),  # yesterday
    ],
)
def test_clock_time(instant: datetime, text: str) -> None:
    assert clock_time(instant, NOW, ZONE) == text


def test_tomorrow_is_judged_on_the_owners_calendar() -> None:
    # 22:00 on Tuesday in Detroit is Wednesday in UTC; 23:00 is still "today".
    assert clock_time(local(6, 23), local(6, 22), ZONE) == "11:00 PM"

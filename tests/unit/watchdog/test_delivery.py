"""Delivering notifications in the background, with retries."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from hottub.domain import Notification, NotificationError
from hottub.watchdog import NotificationOutbox, NullNotifier
from hottub.watchdog.delivery import RETRY_DELAYS
from tests.fakes.notifier import RecordingNotifier

HELLO = Notification("Hot tub: test", "Hello")


class Waits:
    """Stands in for ``asyncio.sleep``: notes each wait and returns at once."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def temporary() -> NotificationError:
    return NotificationError("Pushover could not be reached")


async def test_a_notification_is_delivered_in_the_background() -> None:
    notifier = RecordingNotifier()
    outbox = NotificationOutbox(notifier)

    outbox.post(HELLO)
    assert notifier.sent == []  # accepted, not yet sent: the caller did not wait

    await outbox.drain()
    assert notifier.sent == [HELLO]


async def test_a_failed_send_is_tried_again_after_a_wait() -> None:
    notifier = RecordingNotifier(failures=[temporary(), temporary()])
    waits = Waits()
    outbox = NotificationOutbox(notifier, sleep=waits)

    outbox.post(HELLO)
    await outbox.drain()

    assert notifier.sent == [HELLO]
    assert notifier.attempts == 3
    assert waits.seconds == [60, 120]


async def test_gives_up_after_an_hour() -> None:
    notifier = RecordingNotifier(failures=[temporary() for _ in range(20)])
    waits = Waits()
    outbox = NotificationOutbox(notifier, sleep=waits)

    outbox.post(HELLO)
    await outbox.drain()

    assert notifier.sent == []
    assert notifier.attempts == len(RETRY_DELAYS) + 1
    assert sum(waits.seconds) == 3600


async def test_a_refusal_that_cannot_pass_is_not_tried_again() -> None:
    refused = NotificationError("Pushover answered 400: user key is invalid", permanent=True)
    notifier = RecordingNotifier(failures=[refused])
    waits = Waits()
    outbox = NotificationOutbox(notifier, sleep=waits)

    outbox.post(HELLO)
    await outbox.drain()

    assert notifier.attempts == 1
    assert waits.seconds == []


async def test_one_stuck_notification_does_not_hold_up_the_next() -> None:
    notifier = RecordingNotifier(failures=[temporary()])
    outbox = NotificationOutbox(notifier, retry_delays=[timedelta(hours=1)])
    second = Notification("Hot tub: second", "Hello again")

    outbox.post(HELLO)  # fails, then waits an hour for real
    outbox.post(second)
    while not notifier.sent:
        await _one_turn()

    assert notifier.sent == [second]
    await outbox.close()


async def test_closing_abandons_what_is_still_waiting() -> None:
    notifier = RecordingNotifier(failures=[temporary()])
    outbox = NotificationOutbox(notifier, retry_delays=[timedelta(hours=1)])
    outbox.post(HELLO)
    while notifier.attempts == 0:
        await _one_turn()

    await outbox.close()
    await outbox.drain()  # returns: nothing is left

    assert notifier.sent == []


async def test_with_no_notifier_configured_nothing_is_attempted() -> None:
    notifier = NullNotifier()
    outbox = NotificationOutbox(notifier)

    outbox.post(HELLO)
    await outbox.drain()
    await notifier.send(HELLO)  # accepted and dropped

    assert not notifier.configured
    assert outbox.notifier is notifier


async def _one_turn() -> None:
    await asyncio.sleep(0)

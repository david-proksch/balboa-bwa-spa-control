"""Getting notifications out without holding anything up.

Patterns
--------
* **Null Object** (Woolf): :class:`NullNotifier` stands in when no
  Pushover keys are configured, so nothing else has to ask "is there a
  notifier?" before using it.
* **Outbox**: :class:`NotificationOutbox` accepts a notification at once
  and delivers it in the background, trying again when delivery fails.
  The watchdog's once-a-minute pass and the control of the spa therefore
  never wait for the internet.

The outbox is in memory. A notification still being retried when the
service stops is lost; the alert itself is in the database and on the web
page regardless.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from hottub.domain import Notification, NotificationError, Notifier

_LOGGER = logging.getLogger(__name__)

#: The waits between attempts to deliver one notification. They add up to
#: an hour; after the last one the notification is given up.
RETRY_DELAYS: Sequence[timedelta] = tuple(
    timedelta(minutes=minutes) for minutes in (1, 2, 4, 8, 15, 15, 15)
)


class NullNotifier(Notifier):
    """A notifier that delivers nothing, for when none is configured."""

    @property
    def configured(self) -> bool:
        """Never: notifications go nowhere."""
        return False

    async def send(self, notification: Notification) -> None:
        """Drop the notification."""
        _LOGGER.debug("No notifier is configured; not sent: %s", notification.title)


class NotificationOutbox:
    """Delivers notifications in the background, with retries.

    Args:
        notifier: Where the notifications go.
        retry_delays: The waits between attempts.
        sleep: Waits for a number of seconds. A parameter so that tests
            need not wait an hour.
    """

    def __init__(
        self,
        notifier: Notifier,
        *,
        retry_delays: Sequence[timedelta] = RETRY_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire the outbox to its notifier; starts nothing."""
        self._notifier = notifier
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._deliveries: set[asyncio.Task[None]] = set()

    @property
    def notifier(self) -> Notifier:
        """The notifier behind the outbox, for a send that must report back."""
        return self._notifier

    def post(self, notification: Notification) -> None:
        """Accept a notification for delivery and return at once.

        Must be called from within the running event loop.
        """
        if not self._notifier.configured:
            return
        delivery = asyncio.create_task(self._deliver(notification), name="notification")
        # The set keeps the task alive; a task nobody refers to can be
        # garbage-collected before it finishes.
        self._deliveries.add(delivery)
        delivery.add_done_callback(self._deliveries.discard)

    async def drain(self) -> None:
        """Wait until every notification accepted so far is sent or given up."""
        while self._deliveries:
            await asyncio.gather(*self._deliveries, return_exceptions=True)

    async def close(self) -> None:
        """Abandon the deliveries still under way. Called at shutdown."""
        for delivery in list(self._deliveries):
            delivery.cancel()
        await asyncio.gather(*self._deliveries, return_exceptions=True)

    async def _deliver(self, notification: Notification) -> None:
        for delay in (*self._retry_delays, None):
            try:
                await self._notifier.send(notification)
            except NotificationError as error:
                if error.permanent or delay is None:
                    _LOGGER.error(
                        "Giving up on the notification '%s': %s", notification.title, error
                    )
                    return
                _LOGGER.warning(
                    "Could not send the notification '%s' (%s); trying again in %g min",
                    notification.title,
                    error,
                    delay.total_seconds() / 60,
                )
                await self._sleep(delay.total_seconds())
            else:
                _LOGGER.info("Notification sent: %s", notification.title)
                return

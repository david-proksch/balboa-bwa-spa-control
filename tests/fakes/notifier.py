"""A notifier that keeps what it is given, and can be told to fail."""

from __future__ import annotations

from hottub.domain import Notification, NotificationError, Notifier


class RecordingNotifier(Notifier):
    """Implements ``hottub.domain.Notifier`` with a list.

    Args:
        failures: Errors to raise, one per attempt, before attempts start
            to succeed.
    """

    def __init__(self, failures: list[NotificationError] | None = None) -> None:
        self.sent: list[Notification] = []
        """Every notification delivered, for tests to inspect."""
        self.attempts = 0
        self._failures = list(failures or [])

    @property
    def configured(self) -> bool:
        return True

    @property
    def titles(self) -> list[str]:
        return [notification.title for notification in self.sent]

    async def send(self, notification: Notification) -> None:
        self.attempts += 1
        if self._failures:
            raise self._failures.pop(0)
        self.sent.append(notification)

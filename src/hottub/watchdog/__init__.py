"""Watchdog: notices when something is wrong with the tub and says so.

:class:`WatchdogService` is the public face. The application calls its
``check`` method once a minute; the web handlers ask it for the alerts.
The rules it applies live in ``hottub.domain`` (``alerts.py``). How a
critical alert reaches the owner's phone is behind the ``Notifier``
interface: :class:`PushoverNotifier` when keys are configured,
:class:`NullNotifier` when they are not.

The watchdog only reads the spa. It never sends it a command.
"""

from hottub.watchdog.delivery import NotificationOutbox, NullNotifier
from hottub.watchdog.pushover import PushoverKeys, PushoverNotifier
from hottub.watchdog.service import WatchdogService

__all__ = [
    "NotificationOutbox",
    "NullNotifier",
    "PushoverKeys",
    "PushoverNotifier",
    "WatchdogService",
]

"""Sending notifications to the owner's phone through Pushover.

Pattern: Adapter (GoF) for the :class:`~hottub.domain.Notifier` port. One
notification is one HTTPS POST to Pushover's message API
(https://pushover.net/api).

The keys
--------
Pushover needs two secrets: the application's token and the owner's user
key. They are held in :class:`PushoverKeys`, whose text form shows
neither, and they are sent only in the body of the request. Nothing in
this module logs a request, and error messages quote only what Pushover
answered, which does not repeat the keys.

Priorities
----------
Every notification is sent at Pushover's *high* priority, which sounds
even during the phone's quiet hours: only critical alerts are sent at
all. With ``emergency`` set they are sent at *emergency* priority
instead, which repeats every minute for up to an hour until acknowledged
on the phone.

The HTTP request uses the standard library and runs in a worker thread,
so no HTTP client library is needed and the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from hottub.domain import Notification, NotificationError, Notifier

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

#: Seconds to wait for Pushover before the attempt counts as failed.
_TIMEOUT_S = 15.0

_HIGH_PRIORITY = "1"
_EMERGENCY_PRIORITY = "2"
#: For emergency priority: repeat every 60 seconds, for at most an hour.
_EMERGENCY_RETRY_S = "60"
_EMERGENCY_EXPIRE_S = "3600"

#: Sends a form to a URL and returns the HTTP status and the body.
#: Raises OSError when no answer was received.
HttpPost = Callable[[str, Mapping[str, str]], tuple[int, str]]


@dataclass(frozen=True)
class PushoverKeys:
    """The two secrets Pushover needs. Neither appears when this is printed.

    Attributes:
        token: The application's API token.
        user: The owner's user key.
    """

    token: str = field(repr=False)
    user: str = field(repr=False)


class PushoverNotifier(Notifier):
    """Sends notifications through Pushover.

    Args:
        keys: The application token and user key.
        emergency: Send at emergency priority, which repeats until
            acknowledged, instead of high priority.
        url: The API address; a parameter so tests can point it elsewhere.
        post: Performs the HTTP request; a parameter for the same reason.
    """

    def __init__(
        self,
        keys: PushoverKeys,
        *,
        emergency: bool = False,
        url: str = PUSHOVER_URL,
        post: HttpPost | None = None,
    ) -> None:
        """Remember the settings; contacts nobody."""
        self._keys = keys
        self._emergency = emergency
        self._url = url
        self._post = post or _post_form

    @property
    def configured(self) -> bool:
        """Always: there are keys."""
        return True

    async def send(self, notification: Notification) -> None:
        """Send one notification.

        Raises:
            NotificationError: If Pushover could not be reached or answered
                with an error. It is ``permanent`` when Pushover rejected
                the request itself (wrong keys, for example), since
                sending the same again cannot succeed.
        """
        form = {
            "token": self._keys.token,
            "user": self._keys.user,
            "title": notification.title,
            "message": notification.message,
            "priority": _EMERGENCY_PRIORITY if self._emergency else _HIGH_PRIORITY,
        }
        if self._emergency:
            form["retry"] = _EMERGENCY_RETRY_S
            form["expire"] = _EMERGENCY_EXPIRE_S
        try:
            status, body = await asyncio.to_thread(self._post, self._url, form)
        except OSError as error:
            # Covers refused connections, DNS failures and timeouts. Only
            # the kind of error is quoted, never the request.
            raise NotificationError(
                f"Pushover could not be reached ({type(error).__name__})"
            ) from None
        if status == 200:
            return
        # 4xx means the request itself is wrong; anything else may pass.
        raise NotificationError(
            f"Pushover answered {status}: {_reasons(body)}", permanent=400 <= status < 500
        )


def _reasons(body: str) -> str:
    """Pick the error texts out of Pushover's answer."""
    try:
        errors = json.loads(body).get("errors")
    except (ValueError, AttributeError):
        errors = None
    if isinstance(errors, list) and errors:
        return "; ".join(str(error) for error in errors)
    return "no reason given"


def _post_form(url: str, form: Mapping[str, str]) -> tuple[int, str]:
    """POST ``form`` to ``url`` and return the status and body.

    Raises:
        OSError: If no HTTP answer was received.
    """
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        # An answer with an error status is still an answer.
        return error.code, error.read().decode("utf-8", "replace")

"""The Pushover adapter, against a stub and against a local HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hottub.domain import Notification, NotificationError
from hottub.watchdog import PushoverKeys, PushoverNotifier
from hottub.watchdog.pushover import PUSHOVER_URL

KEYS = PushoverKeys(token="app-token-secret", user="user-key-secret")
ALERT = Notification("Hot tub: Water too cold", "The water is at 80 °F.")


class StubPost:
    """Stands in for the HTTP request: notes the form and answers as told."""

    def __init__(self, status: int = 200, body: str = '{"status":1}') -> None:
        self.status, self.body = status, body
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, form: Mapping[str, str]) -> tuple[int, str]:
        self.calls.append((url, dict(form)))
        return self.status, self.body


class TestRequest:
    async def test_posts_the_message_at_high_priority(self) -> None:
        post = StubPost()

        await PushoverNotifier(KEYS, post=post).send(ALERT)

        assert post.calls == [
            (
                PUSHOVER_URL,
                {
                    "token": "app-token-secret",
                    "user": "user-key-secret",
                    "title": "Hot tub: Water too cold",
                    "message": "The water is at 80 °F.",
                    "priority": "1",
                },
            )
        ]

    async def test_emergency_priority_repeats_until_acknowledged(self) -> None:
        post = StubPost()

        await PushoverNotifier(KEYS, emergency=True, post=post).send(ALERT)

        (_, form) = post.calls[0]
        assert (form["priority"], form["retry"], form["expire"]) == ("2", "60", "3600")

    def test_is_configured(self) -> None:
        assert PushoverNotifier(KEYS).configured


class TestFailures:
    async def test_a_rejected_request_cannot_be_retried(self) -> None:
        post = StubPost(
            400, '{"user":"invalid","errors":["user identifier is invalid"],"status":0}'
        )

        with pytest.raises(NotificationError, match="400: user identifier is invalid") as raised:
            await PushoverNotifier(KEYS, post=post).send(ALERT)

        assert raised.value.permanent

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_a_server_error_may_pass(self, status: int) -> None:
        post = StubPost(status, "<html>Bad gateway</html>")

        with pytest.raises(NotificationError, match="no reason given") as raised:
            await PushoverNotifier(KEYS, post=post).send(ALERT)

        assert not raised.value.permanent

    async def test_no_answer_at_all_may_pass(self) -> None:
        def unreachable(url: str, form: Mapping[str, str]) -> tuple[int, str]:
            raise TimeoutError(f"timed out sending {dict(form)}")

        with pytest.raises(NotificationError, match="could not be reached") as raised:
            await PushoverNotifier(KEYS, post=unreachable).send(ALERT)

        assert not raised.value.permanent
        # The error names its kind only; what was being sent stays out of it.
        assert "secret" not in str(raised.value)
        assert raised.value.__cause__ is None


class TestSecrets:
    def test_the_keys_do_not_appear_when_printed(self) -> None:
        assert "secret" not in repr(KEYS)
        assert "secret" not in str(KEYS)

    async def test_an_error_does_not_quote_the_keys(self) -> None:
        post = StubPost(400, '{"token":"invalid","errors":["application token is invalid"]}')

        with pytest.raises(NotificationError) as raised:
            await PushoverNotifier(KEYS, post=post).send(ALERT)

        assert "secret" not in str(raised.value)


class _Pushover(BaseHTTPRequestHandler):
    """A stand-in for api.pushover.net that accepts one known token."""

    received: list[dict[str, str]]

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        type(self).received.append(form)
        if form.get("token") == KEYS.token:
            self._answer(200, {"status": 1, "request": "abc"})
        else:
            self._answer(400, {"status": 0, "errors": ["application token is invalid"]})

    def _answer(self, status: int, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def server() -> Iterator[HTTPServer]:
    _Pushover.received = []
    server = HTTPServer(("127.0.0.1", 0), _Pushover)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def url_of(server: HTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_port}/1/messages.json"


class TestOverRealHttp:
    """The request the standard library actually makes, to a local server."""

    async def test_delivers_a_form_the_server_understands(self, server: HTTPServer) -> None:
        await PushoverNotifier(KEYS, url=url_of(server)).send(ALERT)

        assert _Pushover.received == [
            {
                "token": "app-token-secret",
                "user": "user-key-secret",
                "title": "Hot tub: Water too cold",
                "message": "The water is at 80 °F.",
                "priority": "1",
            }
        ]

    async def test_reports_the_servers_refusal(self, server: HTTPServer) -> None:
        wrong = PushoverKeys(token="not-the-token", user="user-key-secret")

        with pytest.raises(NotificationError, match="application token is invalid") as raised:
            await PushoverNotifier(wrong, url=url_of(server)).send(ALERT)

        assert raised.value.permanent

    async def test_reports_a_server_that_is_not_there(self, server: HTTPServer) -> None:
        url = url_of(server)
        server.shutdown()
        server.server_close()

        with pytest.raises(NotificationError, match="could not be reached") as raised:
            await PushoverNotifier(KEYS, url=url).send(ALERT)

        assert not raised.value.permanent

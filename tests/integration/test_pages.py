"""The web pages, end to end: HTTP in, HTML out, simulated spa behind.

It is Monday 5 January 2026, noon UTC (the application's time zone here);
the simulated tub is at 99 °F, set to 99 °F.

The pages are checked for what they say and offer, not for their exact
markup, so the templates can be rearranged without rewriting these tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hottub.app import build_app
from hottub.config import AppConfig
from hottub.domain import Alert, AlertKind, Severity, SpaFault, fault_alert
from hottub.persistence import SqlAlertRepository, open_database
from hottub.spa import FakeSpaAdapter
from tests.fakes.clock import FakeClock

NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
PAGES = "/hot-tub"
UI = "/hot-tub/ui"
API = "/hot-tub/api/v1"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOON)


@pytest.fixture
def spa(clock: FakeClock) -> FakeSpaAdapter:
    return FakeSpaAdapter(clock)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(spa_adapter="fake", database_path=tmp_path / "pages.db")


@pytest.fixture
def client(config: AppConfig, spa: FakeSpaAdapter, clock: FakeClock) -> Iterator[TestClient]:
    with TestClient(build_app(config, spa=spa, clock=clock)) as client:
        yield client


def store_alert(config: AppConfig, alert: Alert) -> None:
    engine = open_database(config.database_path)
    try:
        SqlAlertRepository(engine).add(alert)
    finally:
        engine.dispose()


def post(client: TestClient, address: str, **form: str) -> str:
    """Submit a form as a browser would and return the HTML that comes back."""
    response = client.post(f"{UI}/{address}", data=form, headers=FROM_PAGE)
    assert response.status_code == 200
    return str(response.text)


#: What HTMX adds to everything the pages send. Without it an action is refused.
FROM_PAGE = {"HX-Request": "true"}

FLOW = fault_alert(SpaFault(16, "The water flow is low", NOON), NOON)


class TestDashboard:
    def test_shows_the_tub_the_schedule_and_every_part(self, client: TestClient) -> None:
        response = client.get(f"{PAGES}/")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        page = response.text
        for part in ("reading", "outlook", "plans", "controls", "runtime", "banner"):
            assert f'id="{part}"' in page
        assert "Water temperature" in page
        assert "Idle: the tub may cool to 85 °F." in page
        assert "Range" in page and "85 to 99 °F" in page
        assert "24 hours" in page and "30 days" in page

    def test_the_address_without_a_slash_leads_to_it(self, client: TestClient) -> None:
        response = client.get(PAGES, follow_redirects=False)

        assert response.status_code == 308
        assert response.headers["location"] == "/hot-tub/"

    def test_loads_only_files_this_service_serves(self, client: TestClient) -> None:
        # No address on another site: the Pi may have no internet, and the
        # pages must not depend on anyone else's server.
        page = client.get(f"{PAGES}/").text

        assert "http://" not in page and "https://" not in page
        for address in ("static/app.css", "static/app.js", "static/vendor/htmx-2.0.4.min.js"):
            assert f"{PAGES}/{address}" in page
            assert client.get(f"{PAGES}/{address}").status_code == 200

    def test_before_the_spa_is_reached_it_says_so_and_offers_no_controls(
        self, config: AppConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unreachable = FakeSpaAdapter(clock)
        monkeypatch.setattr(unreachable, "connect", unreachable.disconnect)

        with TestClient(build_app(config, spa=unreachable, clock=clock)) as client:
            page = client.get(f"{PAGES}/").text

        assert "The spa has not been reached yet. Still trying." in page
        assert "disabled" in page

    def test_a_spa_gone_quiet_is_shown_with_its_last_values(
        self, client: TestClient, spa: FakeSpaAdapter, clock: FakeClock
    ) -> None:
        spa.drop_connection()
        clock.advance(minutes=5)

        reading = client.get(f"{UI}/reading").text

        assert "last heard from 5 minutes ago" in reading
        assert ">99<" in reading


class TestParts:
    @pytest.mark.parametrize("part", ["reading", "controls", "outlook", "runtime", "banner"])
    def test_each_self_refreshing_part_is_a_fragment_with_its_own_address(
        self, client: TestClient, part: str
    ) -> None:
        response = client.get(f"{UI}/{part}")

        assert response.status_code == 200
        html = response.text
        assert html.lstrip().startswith("<") and "<html" not in html
        assert f'id="{part}"' in html
        assert f'hx-get="{UI}/{part}"' in html  # so the swapped-in part refreshes again

    def test_the_parts_are_not_in_the_api_reference(self, client: TestClient) -> None:
        paths = client.get("/hot-tub/api/openapi.json").json()["paths"]

        assert not any("/ui/" in path for path in paths)


class TestControls:
    def test_a_button_changes_the_tub_and_returns_the_controls_and_reading(
        self, client: TestClient
    ) -> None:
        html = post(client, "pump1", value="high")

        assert 'id="controls"' in html
        assert 'id="reading"' in html and "hx-swap-oob" in html
        assert client.get(f"{API}/status").json()["pump1"] == "high"

    def test_pump_2_and_heat_mode(self, client: TestClient) -> None:
        post(client, "pump2", value="on")
        post(client, "heat-mode", value="rest")

        status = client.get(f"{API}/status").json()
        assert (status["pump2"], status["heat_mode"]) == ("on", "rest")

    def test_the_pressed_button_is_marked(self, client: TestClient) -> None:
        html = post(client, "pump2", value="on")

        on_button = html[
            html.index('hx-post="/hot-tub/ui/pump2" hx-vals=\'{"value": "on"}\'') - 80 :
        ]
        assert 'aria-pressed="true"' in on_button[:80]

    def test_a_spa_that_cannot_be_reached_is_reported_in_the_part(
        self, client: TestClient, spa: FakeSpaAdapter
    ) -> None:
        spa.drop_connection()

        html = post(client, "pump1", value="high")

        assert 'role="alert"' in html
        assert "disconnected" in html

    def test_a_value_that_is_not_a_choice_is_reported_not_obeyed(self, client: TestClient) -> None:
        html = post(client, "pump1", value="turbo")

        assert 'role="alert"' in html
        assert client.get(f"{API}/status").json()["pump1"] == "off"


class TestPlans:
    def test_planning_a_use_shows_its_timetable_and_updates_the_schedule(
        self, client: TestClient
    ) -> None:
        html = post(client, "plan/today", use_time="20:00")

        assert '<option value="20:00" selected>8:00 PM</option>' in html
        assert "Heater starts at about 6:45 PM; ready by 7:00 PM; kept warm until 11:00 PM." in html
        assert "Cancel" in html
        # The schedule part comes along, already knowing about the plan.
        assert 'id="outlook"' in html
        assert "Heater will start at about 6:45 PM" in html
        assert client.get(f"{API}/plan/today").status_code == 200

    def test_tomorrow_is_planned_separately(self, client: TestClient) -> None:
        html = post(client, "plan/tomorrow", use_time="06:30")

        assert '<option value="06:30" selected>6:30 AM</option>' in html
        assert client.get(f"{API}/plan/tomorrow").json()["use_time"] == "06:30:00"
        assert client.get(f"{API}/plan/today").status_code == 404

    def test_the_time_is_chosen_from_a_list_of_quarter_hours(self, client: TestClient) -> None:
        page = client.get(f"{PAGES}/").text

        assert 'type="time"' not in page
        assert '<option value="19:00">7:00 PM</option>' in page
        assert '<option value="" selected>Choose a time</option>' in page

    def test_cancelling_removes_the_plan(self, client: TestClient) -> None:
        post(client, "plan/today", use_time="20:00")

        html = post(client, "plan/today/cancel")

        assert "Heater starts" not in html
        assert client.get(f"{API}/plan/today").status_code == 404

    @pytest.mark.parametrize(
        ("use_time", "message"),
        [("03:00", "already be over"), ("", "Choose a time first."), ("soon", "Invalid")],
    )
    def test_a_time_that_cannot_be_planned_is_explained(
        self, client: TestClient, use_time: str, message: str
    ) -> None:
        html = post(client, "plan/today", use_time=use_time)

        assert 'role="alert"' in html
        assert message in html
        assert client.get(f"{API}/plan/today").status_code == 404

    def test_the_weekend_switch(self, client: TestClient) -> None:
        on = post(client, "weekend", value="on")
        assert 'aria-checked="true"' in on
        assert client.get(f"{API}/plan/weekend").json()["enabled"] is True
        assert "Friday 3:00 PM to Sunday midnight" in on

        off = post(client, "weekend", value="off")
        assert 'aria-checked="false"' in off
        assert client.get(f"{API}/plan/weekend").json()["enabled"] is False

    def test_only_today_and_tomorrow_exist(self, client: TestClient) -> None:
        assert (
            client.post(
                f"{UI}/plan/friday", data={"use_time": "18:00"}, headers=FROM_PAGE
            ).status_code
            == 422
        )


class TestAlerts:
    def test_all_is_well_at_first(self, client: TestClient) -> None:
        page = client.get(f"{PAGES}/alerts").text

        assert "Nothing is wrong." in page
        assert "No earlier alerts." in page
        assert 'role="alert"' not in client.get(f"{UI}/banner").text

    def test_an_active_alert_is_on_every_page(self, config: AppConfig, client: TestClient) -> None:
        store_alert(config, FLOW)

        for address in ("/", "/alerts", "/settings"):
            page = client.get(f"{PAGES}{address}").text
            assert "The spa logged: The water flow is low (code 16)." in page

    def test_a_fault_is_acknowledged_with_its_button(
        self, config: AppConfig, client: TestClient
    ) -> None:
        store_alert(config, FLOW)
        assert f"{UI}/alerts/1/acknowledge" in client.get(f"{PAGES}/alerts").text

        html = post(client, "alerts/1/acknowledge")

        assert "Nothing is wrong." in html
        assert 'id="banner"' in html  # the banner is emptied in the same answer
        assert client.get(f"{API}/alerts").json()["active"] == []

    def test_an_alert_that_clears_by_itself_has_no_button(
        self, config: AppConfig, client: TestClient
    ) -> None:
        store_alert(config, Alert(AlertKind.NOT_HEATING, Severity.CRITICAL, "No heat.", NOON))

        page = client.get(f"{PAGES}/alerts").text

        assert "No heat." in page
        assert "Clears by itself" in page
        assert "/acknowledge" not in page

    def test_past_alerts_are_listed(self, config: AppConfig, client: TestClient) -> None:
        raised = NOON - timedelta(days=2)
        store_alert(
            config,
            Alert(
                AlertKind.STALE_DATA,
                Severity.WARNING,
                "Quiet.",
                raised,
                raised + timedelta(minutes=4),
            ),
        )

        page = client.get(f"{PAGES}/alerts").text

        assert "Jan 3, 12:00 PM" in page and "Jan 3, 12:04 PM" in page
        assert "Nothing is wrong." in page

    def test_text_from_the_spa_cannot_inject_markup(
        self, config: AppConfig, client: TestClient
    ) -> None:
        nasty = SpaFault(99, "<script>alert(1)</script>", NOON)
        store_alert(config, fault_alert(nasty, NOON))

        page = client.get(f"{PAGES}/alerts").text

        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestSettings:
    def test_shows_the_current_values(self, client: TestClient) -> None:
        page = client.get(f"{PAGES}/settings").text

        assert 'name="floor" value="85"' in page
        assert 'name="ceiling" value="99"' in page
        assert 'name="hours" value="3"' in page
        assert "No Pushover keys are configured" in page

    @pytest.mark.parametrize(
        ("floor", "ceiling"), [("101", "103"), ("80", "95"), ("90", "95"), ("85", "104")]
    )
    def test_the_range_is_saved_whichever_way_it_moves(
        self, client: TestClient, floor: str, ceiling: str
    ) -> None:
        html = post(client, "settings/band", floor=floor, ceiling=ceiling)

        assert "Saved." in html
        status = client.get(f"{API}/status").json()
        assert (status["floor_f"], status["ceiling_f"]) == (float(floor), float(ceiling))

    @pytest.mark.parametrize(
        ("floor", "ceiling", "message"),
        [
            ("99", "90", "Floor (99 °F) must be below ceiling (90 °F)"),
            ("warm", "99", "The floor must be a number."),
            ("85", "120", "104"),
        ],
    )
    def test_a_range_that_makes_no_sense_is_explained_and_not_saved(
        self, client: TestClient, floor: str, ceiling: str, message: str
    ) -> None:
        html = post(client, "settings/band", floor=floor, ceiling=ceiling)

        assert 'role="alert"' in html
        assert message in html
        assert "Saved." not in html
        status = client.get(f"{API}/status").json()
        assert (status["floor_f"], status["ceiling_f"]) == (85.0, 99.0)

    def test_the_hold_time_is_saved_in_hours(self, client: TestClient) -> None:
        html = post(client, "settings/hold", hours="2.5")

        assert "Saved." in html
        assert 'value="2.5"' in html
        assert client.get(f"{API}/plan/hold").json() == {"minutes": 150}

    def test_a_hold_time_out_of_range_is_explained(self, client: TestClient) -> None:
        html = post(client, "settings/hold", hours="24")

        assert "between 30 minutes and 12 hours" in html
        assert client.get(f"{API}/plan/hold").json() == {"minutes": 180}

    def test_with_keys_a_test_notification_can_be_sent(
        self, config: AppConfig, spa: FakeSpaAdapter, clock: FakeClock
    ) -> None:
        keyed = replace(config, pushover_token="app-token-secret", pushover_user="user-secret")

        with TestClient(build_app(keyed, spa=spa, clock=clock)) as client:
            page = client.get(f"{PAGES}/settings").text

        assert "Send a test notification" in page
        assert "secret" not in page

    def test_without_keys_there_is_no_button_and_a_try_says_why(self, client: TestClient) -> None:
        assert "Send a test notification" not in client.get(f"{PAGES}/settings").text

        html = post(client, "settings/test-notification")

        assert "No Pushover keys are configured" in html


class TestOnlyThePagesMayAct:
    """Another website's page must not be able to press the buttons."""

    def pump(self, client: TestClient, **headers: str) -> int:
        response = client.post(f"{UI}/pump1", data={"value": "high"}, headers=headers)
        return int(response.status_code)

    def test_a_plain_form_post_is_refused(self, client: TestClient) -> None:
        # What a form on another site would send: no HTMX header.
        assert self.pump(client) == 403
        assert client.get(f"{API}/status").json()["pump1"] == "off"

    def test_a_request_the_browser_marks_as_cross_site_is_refused(self, client: TestClient) -> None:
        assert self.pump(client, **FROM_PAGE, **{"Sec-Fetch-Site": "cross-site"}) == 403

    @pytest.mark.parametrize(
        "origin", ["http://evil.example", "null", "http://james-brown.local.evil.example"]
    )
    def test_a_request_from_another_origin_is_refused(
        self, client: TestClient, origin: str
    ) -> None:
        assert self.pump(client, **FROM_PAGE, Origin=origin) == 403
        assert client.get(f"{API}/status").json()["pump1"] == "off"

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Sec-Fetch-Site": "same-origin"},
            {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
            # nginx passes the host without the port.
            {"Origin": "http://testserver:8080"},
        ],
    )
    def test_the_pages_own_requests_are_accepted(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        assert self.pump(client, **FROM_PAGE, **headers) == 200
        assert client.get(f"{API}/status").json()["pump1"] == "high"

    def test_fetching_a_part_needs_nothing(self, client: TestClient) -> None:
        assert client.get(f"{UI}/reading").status_code == 200
        assert client.get(f"{PAGES}/").status_code == 200

    def test_the_json_api_is_unaffected(self, client: TestClient) -> None:
        assert client.put(f"{API}/pumps/1", json={"speed": "high"}).status_code == 200

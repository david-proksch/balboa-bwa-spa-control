"""The watchdog: raises an alert when something is wrong, and clears it when it is not.

Pattern: Service Layer (Fowler, PoEAA). :class:`WatchdogService` is the
entry point for everything about alerts. The application calls
:meth:`WatchdogService.check` once a minute; the web handlers ask for the
alerts and acknowledge them.

What a pass does
----------------
1. Looks at the spa as last reported (nothing is requested from it).
2. Applies the stale-data rule. If the spa has gone quiet, that is the
   one thing known, so the other rules are not consulted: their alerts
   are neither raised nor cleared, and their counts start again when
   data returns.
3. Otherwise applies each remaining rule (``hottub.domain.alerts``) and
   brings the stored alerts in line with the verdicts: raise, make more
   severe, or clear. An alert is raised once, however long its cause
   lasts.
4. Every few minutes, asks the spa for the newest entry of its fault log
   and raises an alert if it is one not seen before. The very first
   entry ever seen is only noted: it happened before anyone was watching.

Telling the owner
-----------------
Every change is written to the log. Critical alerts are also pushed to
the owner's phone, once when raised (or when a warning becomes critical)
and once when cleared. Sending is handed to a
:class:`~hottub.watchdog.delivery.NotificationOutbox`, so a pass never
waits for the internet.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from hottub.control import ControlService
from hottub.domain import (
    Alert,
    AlertCheck,
    AlertKind,
    AlertNotAcknowledgeableError,
    AlertNotFoundError,
    AlertRepository,
    Clock,
    HotTubError,
    Notification,
    NotificationError,
    Observation,
    Severity,
    StaleDataCheck,
    Standing,
    Verdict,
    WatchdogThresholds,
    fault_alert,
    standard_checks,
)
from hottub.spa import SpaPort
from hottub.watchdog.delivery import NotificationOutbox

_LOGGER = logging.getLogger(__name__)

_TITLE_PREFIX = "Hot tub"


class WatchdogService:
    """Watches the spa and keeps the list of alerts.

    Args:
        control: Used to read the spa's state and the band.
        spa: Used to ask for the fault log, which is not part of the state.
        alerts: Where the alerts are stored.
        outbox: Delivers notifications in the background.
        clock: Source of the current time.
        thresholds: The figures the rules use.
        checks: The rules applied to the spa's state while its data is
            fresh; the standard ones unless a test supplies others.
    """

    def __init__(
        self,
        control: ControlService,
        spa: SpaPort,
        alerts: AlertRepository,
        outbox: NotificationOutbox,
        clock: Clock,
        thresholds: WatchdogThresholds | None = None,
        checks: Sequence[AlertCheck] | None = None,
    ) -> None:
        """Wire the service to its collaborators; does no I/O."""
        thresholds = thresholds or WatchdogThresholds()
        self._control = control
        self._spa = spa
        self._alerts = alerts
        self._outbox = outbox
        self._clock = clock
        self._thresholds = thresholds
        self._stale = StaleDataCheck(thresholds)
        self._checks = list(checks) if checks is not None else standard_checks(thresholds)
        self._watching_since = clock.now()
        self._next_fault_check_at = self._watching_since

    # --- Watching ------------------------------------------------------------

    async def check(self) -> None:
        """Make one pass. Never raises for a spa that cannot be reached."""
        observation = self._observe()

        stale = self._stale.evaluate(observation)
        self._apply(AlertKind.STALE_DATA, stale, observation.now)
        if stale.standing is Standing.PROBLEM:
            for check in self._checks:
                check.suspend()
            return

        for check in self._checks:
            self._apply(check.kind, check.evaluate(observation), observation.now)
        await self._check_fault_log(observation.now)

    def _observe(self) -> Observation:
        status = self._control.status()
        return Observation(
            now=self._clock.now(),
            state=status.state,
            band=status.band,
            last_message_at=status.last_message_at,
            watching_since=self._watching_since,
        )

    def _apply(self, kind: AlertKind, verdict: Verdict, now: datetime) -> None:
        """Bring the stored alert of ``kind`` in line with ``verdict``."""
        current = next((alert for alert in self._alerts.active() if alert.kind is kind), None)
        if verdict.standing is Standing.UNKNOWN:
            return
        if verdict.standing is Standing.OK:
            if current is not None:
                self._clear(current, now)
            return

        assert verdict.severity is not None
        if current is None:
            self._raise(Alert(kind, verdict.severity, verdict.message, raised_at=now))
        elif current.severity is Severity.WARNING and verdict.severity is Severity.CRITICAL:
            assert current.id is not None
            self._alerts.escalate(current.id, verdict.severity, verdict.message)
            _LOGGER.error("Alert now critical: %s: %s", kind.title, verdict.message)
            self._push(kind.title, verdict.message)
        # Otherwise the alert stands as it is: raised once, not once a minute.

    def _raise(self, alert: Alert) -> Alert:
        stored = self._alerts.add(alert)
        if stored.severity is Severity.CRITICAL:
            _LOGGER.error("Alert raised: %s: %s", stored.kind.title, stored.message)
            self._push(stored.kind.title, stored.message)
        else:
            _LOGGER.warning("Alert raised: %s: %s", stored.kind.title, stored.message)
        return stored

    def _clear(self, alert: Alert, now: datetime) -> None:
        assert alert.id is not None
        self._alerts.clear(alert.id, now)
        _LOGGER.info("Alert cleared: %s", alert.kind.title)
        if alert.severity is Severity.CRITICAL:
            self._push(f"{alert.kind.title}: cleared", f"No longer the case: {alert.message}")

    def _push(self, title: str, message: str) -> None:
        self._outbox.post(Notification(f"{_TITLE_PREFIX}: {title}", message))

    async def _check_fault_log(self, now: datetime) -> None:
        if now < self._next_fault_check_at:
            return
        try:
            fault = await self._spa.latest_fault()
        except HotTubError as error:
            # Asked again on the next pass: the Wi-Fi module often drops a
            # request (docs/balboa-notes.md, "Stalls"), and the log keeps.
            _LOGGER.debug("Could not read the spa's fault log: %s", error)
            return
        self._next_fault_check_at = now + self._thresholds.fault_check_every
        if fault is None:
            return
        seen = self._alerts.last_fault_seen()
        if seen is not None and (seen.code, seen.occurred_at) == (fault.code, fault.occurred_at):
            return
        self._alerts.remember_fault(fault)
        if seen is None:
            _LOGGER.info(
                "Newest entry in the spa's fault log, from before the watch began: %s",
                fault.message,
            )
            return
        self._raise(fault_alert(fault, now))

    # --- Alerts --------------------------------------------------------------

    def active_alerts(self) -> Sequence[Alert]:
        """Return the alerts that stand now, oldest first."""
        return self._alerts.active()

    def history(self, days: int) -> Sequence[Alert]:
        """Return every alert raised in the last ``days`` days, newest first.

        Includes the ones still active.
        """
        return self._alerts.raised_since(self._clock.now() - timedelta(days=days))

    def acknowledge(self, alert_id: int) -> None:
        """Clear an alert that only the owner can clear (a spa fault).

        Raises:
            AlertNotFoundError: If there is no alert with that number.
            AlertNotAcknowledgeableError: If the alert is already over, or
                is of a kind that clears by itself when its cause is gone.
        """
        alert = self._alerts.get(alert_id)
        if alert is None:
            raise AlertNotFoundError(f"There is no alert number {alert_id}")
        if not alert.active:
            raise AlertNotAcknowledgeableError(f"Alert {alert_id} is already over")
        if not alert.kind.needs_acknowledging:
            raise AlertNotAcknowledgeableError(
                f"A '{alert.kind.title}' alert clears by itself when its cause is gone"
            )
        self._alerts.clear(alert_id, self._clock.now())
        _LOGGER.info("Alert acknowledged: %s: %s", alert.kind.title, alert.message)

    def forget_alerts_before(self, instant: datetime) -> None:
        """Delete the alerts that cleared before ``instant``."""
        self._alerts.delete_cleared_before(instant)

    # --- Notifications -------------------------------------------------------

    @property
    def notifications_configured(self) -> bool:
        """Whether critical alerts are pushed to a phone."""
        return self._outbox.notifier.configured

    async def send_test_notification(self) -> None:
        """Send a notification now and report whether it went.

        Unlike an alert's notification this is sent directly, not through
        the outbox, so the owner learns at once whether the keys work.

        Raises:
            NotificationError: If no notifier is configured (``permanent``)
                or the notification could not be delivered.
        """
        if not self.notifications_configured:
            raise NotificationError(
                "No Pushover keys are configured, so there is nowhere to send to",
                permanent=True,
            )
        await self._outbox.notifier.send(
            Notification(
                f"{_TITLE_PREFIX}: test",
                "This is a test. Critical alerts from the hot tub will arrive like this.",
            )
        )
        _LOGGER.info("Test notification sent")

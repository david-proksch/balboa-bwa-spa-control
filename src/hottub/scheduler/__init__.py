"""Scheduler: planned uses, weekend keep-ready, and acting on them.

:class:`SchedulerService` is the public face. The web handlers call it to
store plans and to ask what will happen next; the application calls its
``reconcile`` method once a minute. The rules it applies live in
``hottub.domain`` (``schedule.py``); the spa is changed only through
``hottub.control``.
"""

from hottub.scheduler.service import (
    LOOKAHEAD,
    READING_TRUSTED_FOR,
    Outlook,
    SchedulerService,
    WeekendReady,
)

__all__ = ["LOOKAHEAD", "READING_TRUSTED_FOR", "Outlook", "SchedulerService", "WeekendReady"]

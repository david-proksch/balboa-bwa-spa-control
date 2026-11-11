"""Hot tub controller.

A modular monolith that connects to a Balboa (BWA) spa over Wi-Fi and adds
scheduling, runtime history and alerts on top of it. The architecture and the
reasons behind it are in ``PLAN.md``; what we know about the spa hardware is in
``docs/balboa-notes.md``.

Package map (dependencies point downwards)::

    app          composition root: builds and wires everything
    web          the pages and the JSON API; handlers translate and delegate
    scheduler    plans, and the once-a-minute pass that acts on them
    watchdog     alerts when something is wrong; Pushover notifications
    control      use cases for reading and changing the spa; audit log
    telemetry    history: records what ran when; runtime and heating rate
    spa          talking to the tub: the SpaPort interface and its adapters
    persistence  the SQLite database behind the repository interfaces
    domain       the vocabulary and rules of the problem; imports nothing
                 but the standard library

Packages on the same level do not import each other: ``scheduler`` and
``watchdog``, ``control`` and ``telemetry``, ``spa`` and ``persistence``.
``config`` (settings from file and environment) is used by ``app`` and by
``backup``, the nightly database backup command.
"""

__version__ = "0.1.0"

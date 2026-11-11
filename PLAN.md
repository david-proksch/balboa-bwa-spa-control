# Hot Tub Controller — Architecture & Deployment Plan

Status: **built and in use** on `james-brown` since 2026-11-11; phase 9 (a
week's soak) is what remains. Section 7 records what each phase delivered.
Target host: Raspberry Pi `james-brown`, served at `/hot-tub`.
Spa: Balboa Water Group (BWA) Wi-Fi module. (Its address and MAC address are
replaced throughout by placeholders: `192.168.1.50` and suffix `ABCDEF`.)

Revision 3: review decisions recorded in section 1. Revision 2 changed the
design from seven microservices to a **modular monolith** (one application
process behind nginx); the reasoning is in section 3.

---

## 1. Decisions

### Settled

| Topic | Decision |
|-------|----------|
| **Architecture** | Modular monolith: one application process behind nginx. |
| **Heat modes** | Ready and Rest only, default Ready. Ready-in-Rest is out of scope: it is not offered, and if the spa reports it, it is displayed as Rest. |
| **Default band** | Until changed in the UI: ceiling 99 °F (what the tub was set to, and the top of the controller's low range), floor 85 °F. Configurable in `hottub.toml`. |
| **Ceiling / floor** | The maximum and minimum temperatures the tub should be. The tub is heated to the ceiling when it needs to be ready and is allowed to fall to the floor otherwise; the software never commands a set temperature outside that band. The "target" shown in the UI is whichever of the two is currently commanded. |
| **Planned use vs. blackout** | A planned use overrides the 01:00–12:00 blackout. The blackout applies only to weekend keep-ready. The UI notes when an override is in effect. |
| **Heater** | 240 V. Heating rate is seeded at a conservative 4 °F/hour (a slower assumed rate starts the heater earlier, so the tub is not late), then replaced by the rate measured from recorded history: total rise over total time across heater runs of at least 30 minutes in the last 30 days, once there are 2 hours of them and the result is between 1 and 15 °F/hour. |
| **Alert delivery** | Watchdog alerts are shown on the web page and critical ones are pushed to your phone through Pushover. |
| **Host** | Fresh Raspberry Pi OS install. `install.sh` installs nginx and everything else needed. |

### Assumed unless you say otherwise

| Topic | Assumption | Why it matters |
|-------|------------|----------------|
| **"Friday afternoon" start** | Friday 15:00, set in the configuration file (`[schedule] weekend_start`). Ends Sunday 24:00. | Needs a concrete time. |
| **Length of a planned use** | The tub is held at the ceiling until 3 hours after the planned use time, then let go (owner's choice, 2026-11-11). The hold time can be changed, from 30 minutes to 12 hours, with `PUT /plan/hold` and so from the web page. | The request gave a start but no end. |
| **Schedules while in Rest mode** | If the mode is Rest when a schedule needs heat, the scheduler switches to Ready for the heating window and restores Rest afterwards. | In Rest the heater only runs during filter cycles, so the tub would not be ready on time. |
| **Access control** | LAN-only, no login. | If `james-brown` is ever reachable from outside the LAN, add basic auth at nginx first. |
| **Units** | Fahrenheit. | |

---

## 2. Does this need a database?

Yes. Runtime over 1/7/14/30 days cannot be derived from the spa (it only reports
its current state), and the schedules must survive a reboot.

**SQLite** is sufficient: no server process, a single file
(`/var/lib/hottub/hottub.db`), trivial backup, and the write volume is a few rows
per hour.

| Table | Contents |
|-------|----------|
| `run_interval` | `device` (heater, pump1, pump2), `started_at`, `ended_at`, `is_open`, and for pump 1 the speed. `ended_at` is never empty: it is the last moment the device was confirmed running (see below). |
| `temperature_sample` | One row per minute: water temp, set temp. |
| `settings` | Ceiling, floor, the weekend keep-ready switch, the hold time after a planned use, and the scheduler's note that it owes the spa a return to Rest. (The blackout window, the weekend start, the time zone and the heating-rate seed are in the configuration file, `[schedule]` and `[defaults]`.) |
| `planned_use` | One row per day: date, use time, created-at, and when heating for it began (empty until it has). |
| `command_log` | Every command sent to the spa: what, when, by whom (user or scheduler), result. |
| `alert` | Watchdog alerts: kind, severity, message, `raised_at`, `cleared_at` (empty while active). The newest fault-log entry already dealt with is a row in `settings`. |

Runtime is stored as **intervals**, not as per-second samples: a row is
written when a device starts, and its end is moved forward once a minute
while the spa confirms the device is still running. A 30-day total is then a
sum over intervals clipped to the window. Because the stored end is always a
moment the spa actually reported, a crash, a power cut or a lost connection
cannot inflate the total: the interval simply ends at the last confirmed
moment, and start-up closes whatever was left open. The record under-reports
during an outage rather than guessing.

History older than 90 days (intervals, samples and the command log) is
deleted at start-up and hourly. Schema changes are versioned with Alembic
migrations.

---

## 3. System overview

```
 Browser ──► nginx (/hot-tub/) ──► hottub  (one process, 127.0.0.1:8100)
                                     │
        ┌────────────────────────────┴─────────────────────────────┐
        │  web        HTML pages (Jinja2 + HTMX) and JSON API      │
        │  control    climate · pumps · heat mode  (use cases)     │
        │  scheduler  policies + one-minute reconciliation loop    │
        │  telemetry  interval recorder + runtime queries          │
        │  watchdog   stale data, temperature and fault alerts     │
        │  spa        SpaPort ── BalboaSpaAdapter / FakeSpaAdapter │
        │  persistence  repositories over SQLite                   │
        │  domain     model, rules, events (no framework imports)  │
        └────────────────────────────┬─────────────────────────────┘
                                     │  single TCP connection (port 4257)
                                     ▼
                              BWA Wi-Fi module (…ABCDEF)
```

### Why a monolith

- The BWA Wi-Fi module is unreliable with several simultaneous clients, so only one process can own the connection. Separate temperature, pump and heat-mode services would have been thin HTTP wrappers around it.
- One process removes the internal network links and their timeouts, retries and partial-outage states.
- One systemd unit, one log stream, one deploy, about 60–80 MB of memory.
- One tub, one user, one maintainer: there is no independent scaling or release cadence to gain.

Accepted trade-offs: a crash restarts everything (systemd does so within
seconds, and the spa holds its last set temperature meanwhile), and the single
process has both LAN access and database write access.

The module boundaries are kept strict so that this decision is reversible: each
module exposes a small public interface in its `__init__.py`, and an
`import-linter` rule in CI fails the build if one module reaches into another's
internals or if `domain` imports a framework.

### Modules

| Module | Responsibility |
|--------|----------------|
| **domain** | Value objects (`Temperature`, `PumpSpeed`, `HeatMode`, `SpaState`), domain events, the repository interfaces, heating policies, the blackout window, walk-back arithmetic. Pure Python. |
| **spa** | `SpaPort` interface. `BalboaSpaAdapter` handles discovery (UDP broadcast on port 30303, matching MAC suffix `AB:CD:EF`), the connection, reconnect with back-off, and translation from `pybalboa` types to ours. `FakeSpaAdapter` simulates a tub for development and tests. |
| **control** | Use cases: set ceiling, set floor, read temperature, pump 1 off/low/high, pump 2 on/off, set heat mode. Validates, logs and executes commands. |
| **scheduler** | Planned use with walk-back, weekend keep-ready, blackout. Runs as a background task. |
| **telemetry** | Listens for state changes, records run intervals and temperature samples, answers runtime queries, estimates the heating rate. |
| **watchdog** | Watches the status stream for stale data, out-of-band water temperature, a heater that is not raising the temperature, and spa fault codes. Raises and clears alerts. See section 4. |
| **persistence** | Repositories, schema, migrations. |
| **web** | Pages and JSON API. Handlers are thin and call `control`, `scheduler` and `telemetry`. |
| **app** | Composition root: reads config, builds the objects, wires them together, starts background tasks. The only place that knows every concrete class. |

### JSON API (under `/hot-tub/api/v1`)

The capabilities you listed remain individually addressable, so scripts or a
future home-automation hub can use them without the UI.

| Endpoint | Purpose |
|----------|---------|
| `GET /status` | Current temp, target, ceiling, floor, heat mode, pumps, heater on/off, connection health. |
| `PUT /temperature/ceiling`, `PUT /temperature/floor` | Set ceiling / floor. The setting is saved even when the spa is unreachable. If the spa was set to the edge that moved it follows; if its set temperature is left outside the band it is pulled to the nearest edge. |
| `PUT /pumps/1` | `off`, `low` or `high`. |
| `PUT /pumps/2` | `off` or `on`. |
| `PUT /heat-mode` | `ready` or `rest`. |
| `GET /runtime?days=1,7,14,30` | Heater and pump hours per window (a window of N days is the N × 24 hours up to now), pump 1 split by speed, and `recording_since` so the page can mark a window that reaches back further than the record. |
| `GET /heating-rate` | Degrees per hour the heater gains, and whether that is measured or the configured assumption. |
| `GET/PUT/DELETE /plan/today`, `/plan/tomorrow` | Planned use: `PUT {"use_time": "18:00"}`. The answer is the timetable: heater start, ready time, end of the hold, and `late` if the time cannot be met. Tomorrow exists so that a morning soak can be planned the evening before. |
| `GET/PUT /plan/hold` | How long after a planned use time the tub is kept at the ceiling: `{"minutes": 180}`. Applies to plans already made. |
| `GET/PUT /plan/weekend` | Weekend keep-ready toggle. |
| `GET /plan/next-action` | What the scheduler wants now and what it will do next, each as data and as a sentence; whether a planned use is overriding the blackout; warnings. |
| `GET /alerts` | Active watchdog alerts, plus with `?days=` every alert raised in that time; and whether notifications are configured. |
| `POST /alerts/{id}/acknowledge` | Clear a spa-fault alert. The other kinds clear by themselves (409 if tried). |
| `POST /alerts/test-notification` | Send a test notification now: 204 if it went, 409 if no keys are configured, 502 if Pushover could not be reached or refused. |
| `GET /commands?limit=` | The audit log of commands sent to the spa, newest first. |
| `GET /healthz` | Liveness for systemd and the deploy script. Answers 200 whether or not the spa is reachable. |

FastAPI's generated OpenAPI page at `/hot-tub/api/docs` is the living reference.

The pages are at `/hot-tub/` (dashboard), `/hot-tub/alerts` and
`/hot-tub/settings`. They fetch and post HTML fragments under `/hot-tub/ui/`,
which are for the pages only and are not part of the API.

### Technology

- Python 3.11 (ships with Raspberry Pi OS Bookworm), FastAPI + uvicorn, Pydantic.
- `pybalboa` for the Balboa protocol, behind `SpaPort`.
- SQLite through SQLAlchemy Core, Alembic for migrations.
- UI: server-rendered Jinja2 templates with HTMX 2.0.4, which is copied into the repository (`web/static/vendor`, with its checksum), so the pages load nothing from the internet. No Node build step on the Pi. How the pages are put together is described at the top of `hottub/web/pages.py`.
- Tooling: `uv`, `pytest`, `ruff`, `mypy --strict`, `import-linter`.

---

## 4. Design patterns and where they apply

Patterns are used where they remove a real problem, and each use is named in the
module docstring so a maintainer can look it up.

| Pattern (source) | Where | What it buys |
|------------------|-------|--------------|
| **Ports and Adapters** (Cockburn) | Whole application: `domain` at the centre, `spa`, `persistence` and `web` as adapters. | Domain rules are unit-testable in milliseconds, without a tub or a database. |
| **Gateway** (Fowler, PoEAA) | `spa` module | One place that knows how to talk to the spa. |
| **Adapter / Anti-Corruption Layer** (GoF / Evans) | `BalboaSpaAdapter`, `FakeSpaAdapter` behind `SpaPort` | No other module sees `pybalboa` types; the library can be swapped. |
| **Command** (GoF) | `SetTemperature`, `SetPump1`, `SetPump2`, `SetHeatMode` in `control` | One way to run and audit any command. (Serialised execution on the single connection is the spa adapter's job.) |
| **Observer** (GoF) | In-process event bus; `spa` publishes `SpaStateChanged`; `telemetry`, `watchdog` and the live UI subscribe. | Recording history and monitoring are decoupled from control. |
| **Strategy + Null Object** (GoF / Woolf) | `AlertCheck` implementations, one per watchdog rule; `Notifier` port with `PushoverNotifier` and a `NullNotifier` fallback | Each rule is tested alone. The watchdog does not know how alerts are delivered; another channel is another `Notifier`. |
| **Strategy** (GoF) | `HeatingPolicy`: `PlannedUsePolicy`, `WeekendReadyPolicy`, `IdlePolicy` | Each scheduling rule is isolated and tested alone; a new rule is a new class. |
| **Specification** (Evans/Fowler) | `BlackoutWindow.contains(instant)` | Time-window rules are explicit and composable. |
| **Repository** (Fowler, PoEAA) | `RunIntervalRepository`, `PlanRepository`, `SettingsRepository` | SQL stays out of domain code; in-memory versions for tests. |
| **Service Layer** (Fowler, PoEAA) | `control`, `scheduler`, `telemetry` application services | HTTP handlers contain no business logic. |
| **Dependency Injection / Composition Root** (Fowler; Seemann) | `app` | Wiring is in one file; tests substitute fakes without patching. |
| **Retry with back-off** (Nygard) | Spa reconnect loop | A powered-off or unreachable spa degrades the UI instead of hanging it. |
| **Reconciliation loop** | Scheduler | See below. |

### Scheduler behaviour

The scheduler does not set timers that fire once. Every minute it computes the
**desired** set temperature from the current time and the stored plans, compares
it with the **actual** spa state, and issues a command only if they differ. This
is idempotent, so a reboot, a missed tick, or someone changing the temperature
at the tub's own panel is corrected on the next pass.

Policies are evaluated in priority order; the first that applies wins:

1. **Planned use** — ready time is use time minus 1 hour.
   `heater start = ready time − (ceiling − current temp) ÷ heating rate − 15 min margin`.
   From heater start until the hold time (3 hours unless changed) after the
   use time, desired = ceiling.
   Until heating begins the start is recomputed every minute, so a cooling
   tub moves it earlier. Once heating has begun the start is recorded and
   fixed; otherwise the warming water would move it later again and the
   heater would be switched off and on. If the spa reports no water
   temperature, the last one recorded within the hour is used; only when
   there is none (hours in Rest mode, say) is the floor assumed. The spa
   does drop its reading for some seconds now and then, and assuming the
   floor at once made heating start hours early (seen on the tub,
   2026-11-11). A use is accepted even when it cannot be met in
   time; the tub heats at once and the answer says when it will be warm.
2. **Weekend keep-ready** (if enabled) — Friday 15:00 to Sunday 24:00, desired =
   ceiling, except during the 01:00–12:00 blackout, when desired = floor.
3. **Idle** — desired = floor.

Each pass also sees to the heat mode. While heat is wanted, a spa found in
Rest is switched to Ready, and the scheduler stores a note that it did so;
when heat is no longer wanted it switches back to Rest and clears the note.
A spa the owner keeps in Ready is never put in Rest.

A pass does nothing while the spa is unreachable (that is the watchdog's
business). After a command fails the scheduler waits 1, 2, 4, 8 and then
15 minutes between attempts, so the audit log is not filled; a change of
plan by the owner is tried at once.

Because idle means the floor, **the scheduler takes the set temperature
over**: a tub left at 99 °F is set to the floor on the first pass. To run
the application without that, set `[schedule] enabled = false` (or
`HOTTUB_SCHEDULER=off`): plans are stored and forecast, but no command is
sent. `hottub.dev.toml` does so.

The time zone is `[schedule] timezone`, or the machine's own if that is not
given. The weekend start and the blackout hours are in the same section.

The UI shows the computed next action in plain language ("Heater will start at
2:40 PM to be ready by 6:00 PM") and warns when a requested time cannot be met.

All times are local, timezone-aware, and tested across daylight-saving changes.
The clock is injected, so tests run a whole weekend in milliseconds.

### Watchdog behaviour

No polling for temperature is needed: the spa pushes its status continuously
over the open connection (confirmed in phase 0). The watchdog evaluates that
stream on a one-minute timer. The one exception is the fault log, which the spa
only sends on request, so the watchdog asks for the latest entry every five
minutes. Every threshold below is a setting (`[watchdog]` in the configuration file).

| Check | Raised when | Cleared when | Severity |
|-------|-------------|--------------|----------|
| **Stale data** | No status message for 90 seconds. (Not 30: the Wi-Fi module routinely goes silent for about 20 seconds, see `docs/balboa-notes.md`, "Stalls".) The spa adapter tears the connection down and re-establishes it after 60 seconds of silence, since a silent socket is not reported as closed. | A status message arrives. | Warning; critical after 15 minutes. |
| **Water too cold** | A valid reading more than 3 °F below the floor for 30 minutes with the heater off. | Reading back at or above the floor. | Critical. |
| **Water too hot** | A valid reading more than 2 °F above the ceiling for 30 minutes and not cooling down, or at/above 104 °F at any time. | Reading back at or below the ceiling. | Critical. |
| **Not heating** | The spa reports the heater on, or the set temperature is more than 2 °F above the water, and the water has not risen 1 °F in 45 minutes. | Temperature rises, or heat is no longer called for. | Critical. |
| **Spa fault** | A new entry appears in the controller's fault log (flow, overheat, sensor and similar). "Priming Mode" (code 19), logged at every power-up, is raised as a warning only. The entry found when the watch first begins is only noted: it predates the watch. | Acknowledged (`POST /alerts/{id}/acknowledge`, later a button in the UI); the log is a history, so the spa never reports a fault as cleared. | Critical. |

Rules that keep it from crying wolf:

- A reading is **valid** only when the spa reports one and is in Ready mode or has had a pump running recently. In Rest mode the sensor is not in moving water between filter cycles, so the temperature is treated as unknown and the temperature checks are suspended, not failed.
- Each check needs its condition to hold for the full duration before raising, and an alert is raised once, not once per minute.
- "Water too cold" waits while the heater is running: water on its way up from a low temperature (a refill, a raised floor, the scheduler heating from the floor) is not a fault, and a heater that runs without warming the water is the "not heating" check's business. "Too hot" waits while the tub is merely cooling down, which is when the heater is off and the water is no warmer than when it first exceeded the limit; that covers a lowered ceiling without having to remember that it was lowered.
- Between the raising limit and the band's edge nothing changes, so a reading that hovers at the limit does not raise and clear in turn.
- The checks' timers are in memory: after a restart a condition must hold for its full duration again. Alerts themselves are stored, so one that is active is not raised a second time.
- While data is stale, the other checks are suspended: unknown is not the same as bad.

Alerts appear as a banner on every page and in an alert history view, and are
written to the journal.

**Push delivery (Pushover).** `PushoverNotifier` implements the `Notifier` port
with one HTTPS POST to the Pushover API.

- Only **critical** alerts are pushed: once when raised and once when cleared. Warnings stay on the web page.
- Critical alerts are sent at Pushover's high priority, which bypasses the phone's quiet hours. A setting can promote them to emergency priority, which repeats until acknowledged; it is off by default so a 3 AM Wi-Fi blip does not ring until answered.
- A failed send is retried with back-off for up to an hour and logged; the alert is on the web page regardless. Sending never blocks the watchdog or spa control.
- `POST /alerts/test-notification` sends a test at once and reports whether it went; the settings page will have a button for it.
- The request is made with Python's standard library, so there is no HTTP client dependency.
- The keys are read from the environment variables `HOTTUB_PUSHOVER_TOKEN` and `HOTTUB_PUSHOVER_USER` (on the Pi, from `/etc/hottub/secrets.env`). They are left out of the configuration's text form and of every log and error message. With one set and not the other the service refuses to start.
- If no Pushover keys are configured, `NullNotifier` is used and the app runs as before.
- Limit: nothing can be pushed when the Pi or the internet connection is down. An external check-in service (a "dead man's switch") would cover that and is listed in section 8.

The watchdog only reports. It never changes spa settings, and freeze protection
remains the spa controller's own job, which works with the Pi switched off.

### Things the spa itself does that the code must tolerate

Confirmed against the real controller in phase 0; details in
`docs/balboa-notes.md`.

- The spa has two temperature ranges (low 50–99 °F, high 80–104 °F) and accepts a set temperature only within the active one. `BalboaSpaAdapter` hides this: asked for a temperature, it switches range first when needed, then sets it. Ceiling and floor settings are validated against 50–104 °F overall.
- The library signals an update only when something changed, so a silent connection is detected from the time of the last message received, not from missing events.
- A command is confirmed by the following status update, not by the send succeeding.

- In Rest mode the spa may report no water temperature until a pump has run. The model treats temperature as optional and the UI shows "—".
- Pump 1 low speed cannot be switched off while the spa is heating or filtering. The API reports the actual resulting state, not the requested one.
- Pumps switch themselves off after a built-in timeout. Telemetry closes the interval when it sees the change.
- If the connection is lost, open run intervals are closed at the last confirmed time, so outages do not inflate runtime.
- If our software stops entirely, the spa keeps its last set temperature and its own freeze protection. No failure of ours can leave the heater in an unsafe state.

---

## 5. Repository layout

```
hottub/
├── PLAN.md
├── README.md                  what it is, how to run it locally, how to deploy
├── docs/
│   ├── operations.md          runbook: logs, restart, deploy, backup, common faults
│   └── balboa-notes.md        protocol quirks learned along the way
├── pyproject.toml             pinned dependencies, tool configuration
├── src/hottub/
│   ├── app.py                 composition root
│   ├── config.py              settings from /etc/hottub/hottub.toml + environment
│   ├── domain/
│   ├── spa/
│   ├── control/
│   ├── scheduler/
│   ├── telemetry/
│   ├── watchdog/
│   ├── persistence/           includes migrations/
│   └── web/                   routes, templates/, static/
├── tests/
│   ├── unit/                  mirrors src/hottub, no I/O
│   ├── integration/           whole app against FakeSpaAdapter and a temp database
│   └── fakes/                 fake clock, in-memory repositories
└── deploy/
    ├── deploy.sh              run on your computer: build a release, copy it, install it, check it
    ├── install.sh             run on the Pi by deploy.sh, with sudo; first install and upgrades
    ├── hottub.service         hardened systemd unit
    ├── hottub-backup.service / .timer
    ├── hottub.toml            the Pi's settings, installed once
    ├── secrets.env            empty template for the Pushover keys, installed once
    └── nginx/                 hot-tub.conf (the route), james-brown.conf (the server block)
```

### Documentation standard

- Module docstring on every file: purpose, the pattern it implements, what it collaborates with.
- Docstrings on every public class and function, stating units (°F, UTC vs local) and failure behaviour.
- Comments explain why, particularly for spa quirks, never what the next line does.
- Full type hints, checked with `mypy --strict`.
- An ADR for each settled decision in section 1.
- `docs/operations.md` written for someone who has never seen the code.

---

## 6. Deployment on `james-brown`

### Who creates the `hottub` user

`deploy/install.sh` creates it, together with the directories, the virtual
environment, the systemd units and the nginx location. `deploy/deploy.sh`, run
on your own computer, builds a release, copies it to the Pi and runs
`install.sh` there; the same command installs the first time and upgrades
afterwards. The script is idempotent
(safe to run twice) and every step is commented, so it doubles as the written
instructions. It is run once, with sudo, by your own admin account on the Pi.
Day-to-day commands, going back a version and restoring a backup are in
`docs/operations.md`.

The account is a **system account** with no password, no login shell, and no
group memberships beyond its own:

```
sudo useradd --system --home-dir /opt/hottub --shell /usr/sbin/nologin hottub
```

It needs **no** sudo rights, **no** Linux capabilities, and none of the usual Pi
groups (`gpio`, `i2c`, `video`, `sudo`). Everything it does is unprivileged:
listening on a port above 1024 on localhost, an outbound TCP connection to the
spa, and a UDP broadcast for discovery.

### Filesystem

| Path | Owner : group | Mode | Purpose |
|------|---------------|------|---------|
| `/opt/hottub/` (code, venv) | `root:hottub` | `750`, files `640` | Read-only to the service. A compromised process cannot rewrite its own code. Each release is a fresh environment `venv-<date>-<time>`; `venv` is a link to the one in use, and the one before is kept for going back. |
| `/etc/hottub/hottub.toml` | `root:hottub` | `640` | Read-only to the service. |
| `/etc/hottub/secrets.env` | `root:hottub` | `640` | Pushover keys. Read-only to the service. |
| `/var/lib/hottub/` (database, backups) | `hottub:hottub` | `700` | The only writable location. |
| Logs | journald | — | `journalctl -u hottub`. |

Deploys are done by your admin account over SSH with sudo, never by `hottub`.

### systemd confinement

File permissions keep `hottub` out of other users' files, but the real boundary
is systemd sandboxing. The unit as installed is `deploy/hottub.service`; this is
its substance:

```ini
[Service]
User=hottub
Group=hottub
ExecStart=/opt/hottub/venv/bin/python -m hottub
EnvironmentFile=/etc/hottub/secrets.env
StateDirectory=hottub              # creates /var/lib/hottub with correct ownership
UMask=0077

# Filesystem: whole OS read-only, home directories invisible
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/hottub
PrivateTmp=true
PrivateDevices=true
ProtectProc=invisible
ProcSubset=pid

# Privileges: none, and no way to gain any
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictNamespaces=true
RestrictRealtime=true

# Kernel surface
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
SystemCallFilter=@system-service
SystemCallArchitectures=native

# Network: IP sockets only. Outbound is not restricted by address, because
# Pushover's servers have no fixed IP range that systemd could allow-list.
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK

# Resources
MemoryMax=200M
TasksMax=64
Restart=on-failure
RestartSec=5
```

Network exposure after the Pushover change: the service still accepts
connections only on `127.0.0.1` (nginx is the sole way in), but it may open
outbound connections to any address. In the code, the only outbound
destinations are the spa and `api.pushover.net`.

The Pushover application token and user key live in `/etc/hottub/secrets.env`
(`root:hottub`, mode `640`), loaded with `EnvironmentFile=`. They are kept out
of `hottub.toml`, out of the repository, and out of logs.

The install script finishes by running `systemd-analyze security hottub` and the
result goes into the runbook.

### Reverse proxy

```nginx
location /hot-tub/ {
    proxy_pass http://127.0.0.1:8100;   # no trailing slash: the /hot-tub prefix is passed on
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Prefix /hot-tub;
    proxy_buffering off;            # live status updates
}
```

### Host prerequisites

The Pi is a fresh install, so `install.sh` installs nginx and Python's venv
support from the Raspberry Pi OS package repositories and checks the items below.
On a Pi whose nginx nobody has configured it also installs the port-80 server
block (`deploy/nginx/james-brown.conf`); an nginx that is already set up is left
alone apart from adding `/etc/nginx/apps.d/hot-tub.conf`.

- A Pushover account, the app on your phone, and an application token created for "Hot Tub". You enter the token and your user key on the Pi; they are never committed.
- Time zone set and NTP active (schedules depend on wall-clock time).
- A DHCP reservation for the spa on your router. Discovery still runs at start-up, but a stable address makes recovery faster.
- Nightly copy of the database using SQLite's online backup, kept for 7 days (`hottub-backup.timer` runs `python -m hottub.backup` as the same user in the same kind of sandbox, without network).

---

## 7. Build phases

Each phase ends with something you can run and check.

| Phase | Deliverable | Verified by |
|-------|-------------|-------------|
| **0. Spike — done 2026-11-11** | `scripts/spike_discover.py`, a read-only script that discovers the spa and prints what the controller exposes. | Run against the real spa; findings recorded in `docs/balboa-notes.md` and folded into this plan. |
| **1. Foundation — done 2026-11-11** | Repo skeleton, domain model, `SpaPort`, `FakeSpaAdapter`, tooling, import rules. | 47 unit tests, ruff, mypy strict and import rules all pass. |
| **2. Spa adapter — done 2026-11-11, commands verified on the tub except the range switch** | `BalboaSpaAdapter`: discovery, connection, reconnect, state events, commands. | 48 tests against a stub of the library; reading verified against the real spa with `scripts/check_adapter.py`. Each command sent to the real tub with `scripts/try_commands.py`. |
| **3. Control + API — done 2026-11-11** | Temperature, pump 1, pump 2, heat mode; command log; SQLite database with migrations; configuration file; `python -m hottub`. | 76 tests: control use cases against the simulated spa, the database against a real file, the API end to end. Every action sent to the real tub through the API and confirmed (0.2 to 0.3 s each), then restored. |
| **4. Telemetry — done 2026-11-11** | Interval recording, once-a-minute temperature samples, `GET /runtime`, `GET /heating-rate`, 90-day pruning. | 76 more tests (256 in all) on synthetic and simulated histories: outages, silent stalls, a crash, a sleeping computer, intervals across window edges. Run against the real tub read-only: samples recorded each minute; the tub was idle, so a real heater or pump interval has not been recorded yet. |
| **5. Scheduler — done 2026-11-11, running against the real tub** | Policies, walk-back, reconciliation loop, plan storage, `/plan` endpoints, `[schedule]` configuration. | 185 more tests (447 in all). Tables of instants for the rules: weekend edges, blackout edges, both daylight-saving changes, uses that cannot be met. The scheduler run minute by minute against the simulated tub: a planned use, a whole weekend, Rest mode borrowed and returned (also across a restart), outages, a spa that refuses. |
| **6. Watchdog — done 2026-11-11; Pushover not yet tried with real keys** | The five checks, alert storage, `Notifier` port, `PushoverNotifier`, `[watchdog]` thresholds, `/alerts` endpoints. | 142 more tests (589 in all). Each rule fed a tub minute by minute, including Rest-mode and stale-data suppression and no repeat alerts; the watchdog run against the simulated tub (outage, failed heater, fault log, restart); the notifier against a stub and against a local HTTP server standing in for Pushover, including retry on failure. Running against the real tub read-only. Still to do, with your help: a test notification to your phone, and pulling the spa's network connection to see "stale data" raised. |
| **7. Web UI — done 2026-11-11** | Three pages: dashboard (water, set temperature, heater, range, schedule, planned use for today and tomorrow, weekend switch, pumps, heat mode, running time 1/7/14/30), alerts (active, acknowledge, 30-day history), settings (floor, ceiling, hold time, test notification). Alert banner on every page. | 73 more tests (662 in all): what each part shows, and every form end to end over HTTP against the simulated tub, including errors shown in place and escaping of text from the spa. Walked through in the browser at desktop and phone widths, in light and dark. |
| **8. Deployment — done 2026-11-11, installed on `james-brown`** | `deploy.sh`, `install.sh`, systemd units, nginx config, nightly backup (`python -m hottub.backup`), runbook (`docs/operations.md`). | 16 more tests (678 in all), for the backup and for a fault found while the scheduler ran against the real tub (see section 4, planned use). A release was built and installed into a clean environment on the development machine, where the pages, the API and the backup command all work from the installed package. Installed with `deploy/deploy.sh` on 2026-11-11; the pages answer over the home network and the service reaches the spa. Still to record in the runbook's installation record: the `systemd-analyze security` score and a reboot test. |
| **9. Soak** | One week of real use. | Compare predicted ready times with actual; tune margin, heating rate and watchdog thresholds; review any false alerts. |

---

## 8. Not included unless you ask

- User accounts or login (see the access-control assumption in section 1).
- Ready-in-Rest as a selectable or displayed mode.
- Lights, blower, filter-cycle configuration.
- Electricity cost estimates (the runtime data would support adding this later).
- "Tub is ready" notifications. Only critical watchdog alerts are pushed.
- SMS or email delivery.
- An external check-in service that alerts you when the Pi itself goes silent.
- Remote access from outside the LAN.

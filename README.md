# Hot Tub Controller

A web app and controller for a Balboa (BWA) hot tub. It connects to the spa
over Wi-Fi and adds what the spa's own panel lacks: scheduling ("ready by
6 PM"), a weekend keep-ready mode with an overnight energy blackout, heater
and pump runtime history, and alerts when something is wrong.

It runs as a single service on a Raspberry Pi (`james-brown`) and is served
at `/hot-tub`.

## Where to read first

| Document | What it tells you |
|----------|-------------------|
| [PLAN.md](PLAN.md) | Architecture, design decisions and their reasons, build phases, deployment. |
| [docs/operations.md](docs/operations.md) | Runbook for the Pi: where things are, logs, deploying, backups, what to do when something is wrong. |
| [docs/balboa-notes.md](docs/balboa-notes.md) | What we know about this particular spa and the `pybalboa` library, including its surprises. |

## Status

In use: running on the Pi since 11 November 2026. What remains is a week of
real use to tune it (phase 9). Build phases are listed in PLAN.md section 7.

| Phase | State |
|-------|-------|
| 0. Spa discovery spike | Done |
| 1. Foundation: domain model, spa interface, simulated spa | Done |
| 2. Real spa adapter | Done; reading and commands verified on the tub (range switch untested) |
| 3. Control and JSON API | Done; every action verified on the tub |
| 4. Telemetry: runtime history and heating rate | Done; recording real heater and pump runs |
| 5. Scheduler: planned use, weekend keep-ready | Done; running against the real tub |
| 6. Watchdog: alerts, Pushover | Done; watching the real tub. Pushover not yet tried with real keys |
| 7. Web UI: dashboard, alerts, settings | Done |
| 8. Deployment to the Pi | Done; installed and running. Reboot test and confinement score not yet recorded |
| 9. Soak: a week of real use | Next |

## Deploying to the Pi

```bash
deploy/deploy.sh
```

This runs the tests, builds a release, copies it to `james-brown.local` and
installs it there (it asks for your password on the Pi). The same command
installs the first time and upgrades afterwards. Stop the controller on
your own machine first: only one program can talk to the tub at a time.

Looking after it once it is installed, including logs, backups and going
back a version, is in [docs/operations.md](docs/operations.md).

## Code layout

```
src/hottub/
  app.py        composition root: builds everything and wires it together
  config.py     settings from /etc/hottub/hottub.toml and the environment
  backup.py     the nightly database backup command (python -m hottub.backup)
  web/          the pages (Jinja2 templates + HTMX) and the JSON API; handlers only translate and delegate
  scheduler/    plans (planned use, weekend keep-ready) and the once-a-minute pass that acts on them
  watchdog/     alerts: notices a silent, cold, hot, non-heating or faulted tub; Pushover
  control/      use cases: band, pumps, heat mode; the audit log of commands
  telemetry/    history: records when heater and pumps ran, reports runtime and heating rate
  spa/          SpaPort (the interface to a tub) and its adapters
  persistence/  SQLite: schema, migrations, repositories
  domain/       vocabulary, rules and repository interfaces; standard library only
tests/
  unit/         mirrors src/hottub; no network, no disk, no waiting
  integration/  the database on a real file; the whole app over HTTP with a simulated spa
  fakes/        test doubles: a clock the test controls, in-memory repositories
deploy/
  deploy.sh           run on your computer: build, copy to the Pi, install, check
  install.sh          run on the Pi by deploy.sh; every step is explained in it
  hottub.service      the service, confined by systemd; plus the nightly backup's units
  nginx/              the /hot-tub/ route
scripts/
  spike_discover.py   read-only report of what the real spa exposes
  check_adapter.py    read-only check of our adapter against the real spa
  try_commands.py     CHANGES THE TUB: sends each command once and restores it
```

Imports only point downwards in that list, and only `spa` may use the Balboa
library, only `persistence` the database libraries. `uv run lint-imports`
enforces this.

Every module starts with a docstring saying what it is for and which design
pattern it follows.

## Working on it

Requires [uv](https://docs.astral.sh/uv/); it installs the right Python
(3.11, matching Raspberry Pi OS) and all dependencies.

```bash
uv sync                  # create .venv and install everything
uv run pytest            # tests
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy              # type check (strict)
uv run lint-imports      # architecture rules: who may import whom
```

All five checks must pass before a change is committed.

## Running it locally

With a simulated tub, which needs no hardware:

```bash
HOTTUB_CONFIG=hottub.sim.toml uv run python -m hottub
```

Then open <http://127.0.0.1:8101/hot-tub/>. The simulated tub has its own
port and its own database (`hottub-sim.db`), so it can run beside an
instance that is talking to the real tub.

Against the real tub (the machine must be on the same network):

First copy `hottub.dev.example.toml` to `hottub.dev.toml` and put in your
spa's MAC suffix (the six characters after `BWGSpa_` in the network name its
Wi-Fi module advertises). Git ignores `hottub.dev.toml`.

```bash
HOTTUB_CONFIG=hottub.dev.toml uv run python -m hottub
```

`hottub.dev.toml` switches the scheduler off, so this only watches the tub
and does what you ask through the API. With the scheduler on
(`HOTTUB_SCHEDULER=on`) the application **takes over the set temperature**:
with nothing planned it sets the tub to the floor within seconds of
starting.

Then open <http://127.0.0.1:8100/hot-tub/> for the pages, or
<http://127.0.0.1:8100/hot-tub/api/docs> for the API reference, where every
endpoint can be tried, or:

```bash
curl http://127.0.0.1:8100/hot-tub/api/v1/status
```

```bash
curl "http://127.0.0.1:8100/hot-tub/api/v1/runtime?days=1,7,14,30"
```

To plan a use for 6 PM today, and to see what the scheduler will do about it:

```bash
curl -X PUT -H "Content-Type: application/json" -d '{"use_time": "18:00"}' http://127.0.0.1:8100/hot-tub/api/v1/plan/today
```

```bash
curl http://127.0.0.1:8100/hot-tub/api/v1/plan/next-action
```

Alerts, with the last week's history:

```bash
curl "http://127.0.0.1:8100/hot-tub/api/v1/alerts?days=7"
```

Critical alerts are pushed to a phone when the two Pushover keys are in the
environment (`HOTTUB_PUSHOVER_TOKEN`, `HOTTUB_PUSHOVER_USER`). Never put
them in a file in this repository. Without them alerts appear in the API
only. To check that the keys work:

```bash
curl -X POST http://127.0.0.1:8100/hot-tub/api/v1/alerts/test-notification
```

History is only recorded while the application is running, so runtime
totals cover the time it was up.

Only one program can talk to the tub at a time. Stop the application before
running the scripts below, and the other way round.

The database is `hottub-dev.db` in the project directory. Delete it to start
afresh; it is created and migrated at start-up
(`src/hottub/persistence/migrations/README.md`).

## Talking to the real spa

```bash
uv run python scripts/spike_discover.py
```

This finds the spa on the local network and prints its configuration and
current state, using the Balboa library directly.

```bash
uv run python scripts/check_adapter.py
```

This does the same through our own adapter (`hottub.spa.balboa`), which is
what the application uses. Run it when the application cannot see the tub.

Neither script changes anything on the spa.

```bash
uv run python scripts/try_commands.py --yes
```

This one **does** change the tub: it sends each command once (set
temperature, both pumps, heat mode) and puts everything back. Use it after
changing the adapter's command code, with the owner's agreement.

Do **not** run `python -m pybalboa` against the tub: that command tests every
control by switching it, including the set temperature and the pumps.

## Licence

MIT; see [LICENSE](LICENSE). The bundled HTMX file is under its own
BSD Zero-Clause licence (`src/hottub/web/static/vendor/README.md`).

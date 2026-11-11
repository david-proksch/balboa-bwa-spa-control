# Operations runbook

How to look after the hot tub controller on the Pi (`james-brown`). For
what the program is and why it is built the way it is, see `PLAN.md`; for
the spa's own quirks, `docs/balboa-notes.md`.

Commands are run on the Pi, logged in as yourself, unless they say "on
your computer". Nobody logs in as `hottub`: that account has no password
and no shell, and exists only for the service to run as.

## Where everything is

| What | Where | Who may change it |
|------|-------|-------------------|
| The program (Python environment) | `/opt/hottub/venv` → `/opt/hottub/venv-<date>-<time>` | root, by deploying |
| Settings | `/etc/hottub/hottub.toml` | root (`sudoedit`) |
| Pushover keys | `/etc/hottub/secrets.env` | root (`sudoedit`) |
| Database | `/var/lib/hottub/hottub.db` | the service |
| Database backups | `/var/lib/hottub/backups/hottub-YYYY-MM-DD.db` | the service |
| Service definition | `/etc/systemd/system/hottub.service` | root, by deploying |
| Backup job | `/etc/systemd/system/hottub-backup.service` and `.timer` | root, by deploying |
| Web server route | `/etc/nginx/apps.d/hot-tub.conf` | root, by deploying |
| Logs | the systemd journal | — |
| This file | `/opt/hottub/docs/operations.md` | root, by deploying |

The floor, ceiling, hold time, weekend switch and the plans are not in a
file: they are set on the web pages and stored in the database.

## Everyday commands

Is it running, and since when:

```bash
systemctl status hottub
```

Follow the log as it is written (Ctrl-C to stop):

```bash
journalctl -u hottub -f
```

The last hour of the log:

```bash
journalctl -u hottub --since "1 hour ago"
```

Only warnings and errors since yesterday:

```bash
journalctl -u hottub -p warning --since yesterday
```

Restart it (the plans and settings are in the database and survive):

```bash
sudo systemctl restart hottub
```

Stop it, for example before talking to the tub with the scripts in
`scripts/` from your computer; only one program can hold the tub's
connection at a time:

```bash
sudo systemctl stop hottub
```

```bash
sudo systemctl start hottub
```

Which version is installed:

```bash
sudo cat /opt/hottub/venv/RELEASE
```

Ask the service directly, bypassing nginx:

```bash
curl http://127.0.0.1:8100/hot-tub/api/v1/status
```

## Deploying a new version

On your computer, in the repository, with the change committed:

```bash
deploy/deploy.sh
```

It runs the tests, builds a release, copies it to the Pi and runs
`install.sh` there with sudo. `install.sh` builds the new version beside
the old one and switches over only when it loads, so a failed build leaves
the running service untouched. The service is down for a few seconds
during the switch. Your settings, keys and database are not touched;
database changes a new version needs are applied when it starts.

What `install.sh` does, step by step, is written in the script itself
(`deploy/install.sh`), which is the reference for how the Pi is set up.

## Going back to the previous version

The previous Python environment is kept. List them (the link shows the one
in use):

```bash
ls -l /opt/hottub
```

Point the link at the older one and restart:

```bash
sudo ln -sfn /opt/hottub/venv-<older date-time> /opt/hottub/venv
```

```bash
sudo systemctl restart hottub
```

This works as long as the newer version did not change the database's
structure. If it did (the release notes or `git log` for
`persistence/migrations/versions` will show a new file), the older program
cannot read the newer database: restore last night's backup as well (next
section), or fix forward by deploying a corrected version.

## The database

### Backups

A copy is made every night at about 03:30 and the last seven are kept.

When the backup last ran and will next run:

```bash
systemctl list-timers hottub-backup
```

What it did:

```bash
journalctl -u hottub-backup -n 20
```

Make a copy now:

```bash
sudo systemctl start hottub-backup
```

The copies (the directory is closed to everyone but the service, hence sudo):

```bash
sudo ls -l /var/lib/hottub/backups
```

The copies are on the same SD card as the original, so they protect
against a damaged database or a bad release, not against a dead card. To
keep one elsewhere, copy it off the Pi from your computer:

```bash
ssh -t james-brown.local 'sudo cat /var/lib/hottub/backups/hottub-YYYY-MM-DD.db' > hottub-YYYY-MM-DD.db
```

### Restoring the database

Stop the service, put the copy in place with the service as its owner,
remove the leftovers of the old database's write-ahead log, and start
again:

```bash
sudo systemctl stop hottub
```

```bash
sudo install -o hottub -g hottub -m 600 /var/lib/hottub/backups/hottub-YYYY-MM-DD.db /var/lib/hottub/hottub.db
```

```bash
sudo rm -f /var/lib/hottub/hottub.db-wal /var/lib/hottub/hottub.db-shm
```

```bash
sudo systemctl start hottub
```

What is lost is whatever changed since that copy: plans made, settings
changed, and the running-time history of the days between.

### Starting with an empty database

Stop the service, delete `/var/lib/hottub/hottub.db` (and `-wal`, `-shm`),
start the service. It creates a new database with the default floor and
ceiling from `hottub.toml` and no plans or history.

## Settings

```bash
sudoedit /etc/hottub/hottub.toml
```

```bash
sudo systemctl restart hottub
```

Every key is described at the top of the program's `config.py`:

```bash
less /opt/hottub/venv/lib/python3*/site-packages/hottub/config.py
```

A mistake in the file (an unknown key, a bad value) stops the service from
starting, with a message saying what is wrong:

```bash
journalctl -u hottub -n 20
```

Useful ones:

- `[schedule] enabled = false` stops the scheduler from sending the tub any
  command, while everything else carries on. Use it to take manual control
  of the set temperature for a while.
- `[spa] host = "192.168.1.50"` skips discovery and connects to that
  address. Only worth it if discovery is unreliable, and only with a DHCP
  reservation for the spa on the router.
- `[watchdog]` holds every threshold the alerts use.

## Pushover notifications

Critical alerts are pushed to your phone once the two keys are entered.

1. In Pushover, create an application named "Hot Tub" and note its API
   token. Your user key is on your Pushover dashboard.
2. On the Pi:

   ```bash
   sudoedit /etc/hottub/secrets.env
   ```

   Remove the `#` from both lines and put the values after the `=`.
3. Restart and test:

   ```bash
   sudo systemctl restart hottub
   ```

   Then open Settings on the web page and press "Send a test notification".

The keys are read by root and the service only, and the program never
writes them to its log. If the service will not start after editing the
file, the usual cause is one key set without the other.

To make alerts repeat on the phone until acknowledged, set
`emergency = true` under `[pushover]` in `hottub.toml`.

## When something is wrong

### The page says the spa cannot be reached

The service is running but has lost the tub. In order of likelihood:

1. The spa's Wi-Fi module is having one of its silences. Under a minute is
   normal and is not even mentioned on the page. Wait.
2. The spa is off or its breaker has tripped. Look at the tub's own panel.
3. The spa has left the Wi-Fi. Check the router's client list for the
   module (its MAC address ends in the six characters after `BWGSpa_` in
   its network name).
4. Something else is holding the tub's only connection: the Balboa phone
   app, or this program running on another computer. Close it.

The service keeps retrying by itself; nothing needs restarting. What it is
doing shows in the log:

```bash
journalctl -u hottub --since "15 min ago" | grep -i spa
```

### The pages do not load

From the Pi, ask the service directly:

```bash
curl -i http://127.0.0.1:8100/hot-tub/api/v1/healthz
```

- **It answers.** The service is fine; the trouble is nginx or the
  network. Check `systemctl status nginx` and `sudo nginx -t`, and that a
  server block includes `/etc/nginx/apps.d/*.conf` (`sudo nginx -T | grep
  apps.d`). A "502 Bad Gateway" page from nginx means nginx is fine and
  the service is not answering, which is the next case.
- **It does not answer.** `systemctl status hottub` and `journalctl -u
  hottub -n 50` say why. See below.

### The service will not start, or keeps restarting

```bash
journalctl -u hottub -n 50
```

| The log says | Cause | What to do |
|--------------|-------|------------|
| `Bad value in /etc/hottub/hottub.toml` or `unknown key` | A mistake in the settings | Fix the file; the message names the key. |
| `Set both HOTTUB_PUSHOVER_TOKEN and HOTTUB_PUSHOVER_USER` | One Pushover key without the other | Fix `/etc/hottub/secrets.env`. |
| `Failed to load environment files` | `/etc/hottub/secrets.env` is missing | Run the deploy again; it recreates it empty. |
| `unable to open database file`, `readonly database` | `/var/lib/hottub` is not the service's | `sudo chown -R hottub:hottub /var/lib/hottub && sudo chmod 700 /var/lib/hottub` |
| `database disk image is malformed` | A damaged database | Restore a backup (above). |
| `address already in use` | Something else is on port 8100 | `sudo ss -ltnp | grep 8100` to see what. |
| `Operation not permitted`, `Permission denied` on something unexpected | The sandbox forbids something a new version needs | See "The sandbox" below. |

systemd restarts the service five seconds after a crash. If it crashes
repeatedly it gives up for a while; after fixing the cause:

```bash
sudo systemctl reset-failed hottub && sudo systemctl start hottub
```

### The tub was not warm in time

Open the dashboard. The Schedule card says what the scheduler wants and
the Planned use card gives the timetable it worked to. Then:

- How fast does it think the tub heats? `curl
  http://127.0.0.1:8100/hot-tub/api/v1/heating-rate`. Until about two hours
  of heating are on record it assumes the figure in `hottub.toml`
  (`[defaults] heating_rate_f_per_hour`, 4 unless changed). If the tub is
  slower than that, lower the figure.
- Did it send the command, and when? `curl
  "http://127.0.0.1:8100/hot-tub/api/v1/commands?limit=20"` lists every
  command with its time, who sent it and what the spa answered.
- Was the spa reachable at the time? Look for alerts on the Alerts page.

### An alert that will not go away

Spa-fault alerts stay until acknowledged on the Alerts page, because the
spa never says that a fault is over. The other kinds clear by themselves
when their cause is gone; if one does not, its cause is still there.

## The sandbox

The service runs under systemd's confinement: the whole system is
read-only to it except `/var/lib/hottub`, it has no privileges, and it
sees no home directories or devices. The settings are in
`/etc/systemd/system/hottub.service`, each group with a comment.

How tightly it is confined (lower is tighter; `install.sh` prints the last
line of this at the end of every deploy):

```bash
systemd-analyze security hottub
```

If a future version needs something the sandbox forbids, the log shows a
"not permitted" error. Loosen only the one setting involved, in
`deploy/hottub.service` in the repository, and deploy, so that the
repository stays the record of what is installed. To try a change on the
Pi first:

```bash
sudo systemctl edit hottub
```

which writes an override file without touching the installed unit; delete
it again with `sudo systemctl revert hottub`.

## After a power cut or reboot

Nothing to do. The service and the backup timer start by themselves, the
service finds the tub again, and the scheduler's next pass (within a
minute of the tub answering) puts the set temperature where the plans say
it should be. If heating for a planned use was under way, it carries on.
A backup missed while the Pi was off is made shortly after it comes up.

To check after a reboot:

```bash
systemctl is-active hottub nginx hottub-backup.timer
```

## Setting up a new Pi, or doing it by hand

`deploy/deploy.sh` from your computer does everything on a freshly
installed Raspberry Pi OS: it needs only that you can SSH in and use sudo.
Before the first deploy:

- Set the time zone (`sudo raspi-config`, or `sudo timedatectl
  set-timezone America/Detroit`) and check the clock is synchronised
  (`timedatectl`). The schedules follow the Pi's clock.
- Give the spa a DHCP reservation on the router, so its address stays put.
- Stop the controller anywhere else it is running.

`deploy/install.sh` is the list of manual steps, in order, each with its
reason: packages, the `hottub` user, directories, the Python environment,
settings, the systemd units, nginx. To do it by hand, copy a release
(`build/release` after a deploy attempt) to the Pi and follow the script
step by step.

## Removing it

```bash
sudo systemctl disable --now hottub hottub-backup.timer
```

```bash
sudo rm /etc/systemd/system/hottub.service /etc/systemd/system/hottub-backup.service /etc/systemd/system/hottub-backup.timer /etc/nginx/apps.d/hot-tub.conf
```

```bash
sudo systemctl daemon-reload && sudo nginx -t && sudo systemctl reload nginx
```

Then, if the data is no longer wanted: `sudo rm -rf /opt/hottub
/etc/hottub /var/lib/hottub` and `sudo userdel hottub`. The tub keeps
whatever set temperature it last had; set it at the tub's own panel.

## Installation record

Filled in after the first install on `james-brown`:

- Date installed: 2026-11-11
- Raspberry Pi OS and Python version:
- `systemd-analyze security hottub` result:
- Reboot test (service, nginx and timer came back; tub reconnected):
- Test notification received:
- "Stale data" alert seen with the spa's network unplugged:

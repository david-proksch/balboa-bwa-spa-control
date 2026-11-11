#!/usr/bin/env bash
#
# Install or upgrade the hot tub controller on this machine (the Pi).
#
# You do not normally run this yourself: deploy/deploy.sh, run on your own
# computer, copies a release here and runs it. To run it by hand, from the
# release directory on the Pi:
#
#     sudo bash install.sh
#
# It is safe to run again. The first run sets everything up; later runs
# install the new version and leave your settings, your Pushover keys and
# the database alone. Each step below says what it does and why, so this
# file is also the written record of how the Pi is set up. Day-to-day
# commands are in docs/operations.md.
#
# What it changes on the system:
#   - installs the packages nginx, python3-venv and curl, if missing
#   - creates the system user "hottub" (no password, no shell, no groups)
#   - /opt/hottub          the program (read-only to the service)
#   - /etc/hottub          settings and the Pushover keys
#   - /var/lib/hottub      the database and its backups
#   - /etc/systemd/system  hottub.service, hottub-backup.service and .timer
#   - /etc/nginx/apps.d/hot-tub.conf, and on a Pi with no nginx before,
#     a server block that includes it
#
# Options:
#   --no-nginx   leave nginx entirely alone (you route /hot-tub/ yourself)

set -euo pipefail

SERVICE_USER=hottub
PROGRAM_DIR=/opt/hottub
CONFIG_DIR=/etc/hottub
STATE_DIR=/var/lib/hottub
HEALTH_PATH=/hot-tub/api/v1/healthz
SERVICE_URL="http://127.0.0.1:8100${HEALTH_PATH}"
VENVS_KEPT=2          # the one in use and the one before it, for going back

CONFIGURE_NGINX=yes
for argument in "$@"; do
    case "$argument" in
        --no-nginx) CONFIGURE_NGINX=no ;;
        *) echo "Unknown option: $argument" >&2; exit 2 ;;
    esac
done

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; WARNINGS+=("$*"); }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
WARNINGS=()

# --- 0. Preconditions ---------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "Run this with sudo."
command -v apt-get >/dev/null || die "This script is written for Raspberry Pi OS / Debian."
command -v systemctl >/dev/null || die "systemd is required."

RELEASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$RELEASE_DIR"

shopt -s nullglob
wheels=(hottub-*.whl)
shopt -u nullglob
[ "${#wheels[@]}" -eq 1 ] || die "Expected exactly one hottub-*.whl in $RELEASE_DIR."
WHEEL="${wheels[0]}"
for needed in requirements.txt hottub.service hottub-backup.service hottub-backup.timer \
              hottub.toml secrets.env nginx/hot-tub.conf nginx/james-brown.conf; do
    [ -f "$needed" ] || die "The release is incomplete: $needed is missing."
done
RELEASE_NAME="$(cat RELEASE 2>/dev/null || echo unknown)"

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "Python 3.11 or newer is required; this machine has $(python3 --version 2>&1)."

say "Installing hot tub controller, release ${RELEASE_NAME}"
note "Only one program can talk to the tub at a time. If the controller is"
note "also running somewhere else (on your own computer, say), stop it there."

# --- 1. Packages --------------------------------------------------------------
# python3-venv: to give the program its own Python environment.
# nginx:        the web server that passes /hot-tub/ on to the program.
# curl:         used at the end of this script to check that it answers.

say "Packages"
NGINX_WAS_INSTALLED=no
command -v nginx >/dev/null && NGINX_WAS_INSTALLED=yes

packages=()
python3 -c 'import ensurepip, venv' 2>/dev/null || packages+=(python3-venv)
command -v curl >/dev/null || packages+=(curl)
if [ "$CONFIGURE_NGINX" = yes ] && [ "$NGINX_WAS_INSTALLED" = no ]; then
    packages+=(nginx)
fi
if [ "${#packages[@]}" -gt 0 ]; then
    note "Installing: ${packages[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"
else
    note "Nothing to install."
fi

# --- 2. The service's user ----------------------------------------------------
# A system account that nobody can log in as: no password, no shell, and no
# group but its own. It needs no sudo rights and none of the Pi's hardware
# groups. The service runs as this user so that a fault in the program
# cannot touch anything else on the Pi (PLAN.md, section 6).

say "User '${SERVICE_USER}'"
if id "$SERVICE_USER" >/dev/null 2>&1; then
    note "Already exists."
else
    useradd --system --user-group --home-dir "$PROGRAM_DIR" --no-create-home \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    note "Created."
fi
groups_of_user="$(id -nG "$SERVICE_USER")"
[ "$groups_of_user" = "$SERVICE_USER" ] \
    || warn "'${SERVICE_USER}' is in extra groups (${groups_of_user}); it should be in its own only."

# --- 3. Directories -----------------------------------------------------------
# The program and its settings belong to root; the service may read them
# but not change them. Only /var/lib/hottub is the service's own.

say "Directories"
install -d -o root -g "$SERVICE_USER" -m 750 "$PROGRAM_DIR" "$PROGRAM_DIR/docs" "$CONFIG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$STATE_DIR"
note "$PROGRAM_DIR, $CONFIG_DIR, $STATE_DIR"

# --- 4. The program -----------------------------------------------------------
# Each release gets a fresh Python environment in its own directory, and
# /opt/hottub/venv is a link to the one in use. The running service is not
# disturbed while the new one is built, a build that fails changes nothing,
# and the previous one stays for going back (docs/operations.md).
#
# requirements.txt pins the exact version of every library, as tested.

say "Program"
stamp="$(date +%Y%m%d-%H%M%S)"
new_venv="$PROGRAM_DIR/venv-$stamp"
python3 -m venv "$new_venv"
note "Installing libraries (this takes a few minutes on a Pi) ..."
"$new_venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --no-warn-script-location -r requirements.txt
"$new_venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --no-deps "$WHEEL"
# The service cannot write beside the code, so compile it now.
"$new_venv/bin/python" -m compileall -q "$new_venv/lib" >/dev/null
"$new_venv/bin/python" -c 'import hottub.app, hottub.backup' \
    || die "The new program does not load; the old one is still in place."
echo "$RELEASE_NAME" > "$new_venv/RELEASE"
[ -f operations.md ] && install -m 640 operations.md "$PROGRAM_DIR/docs/operations.md"
chown -R root:"$SERVICE_USER" "$PROGRAM_DIR"
chmod -R u=rwX,g=rX,o= "$PROGRAM_DIR"
note "Built $new_venv"

# --- 5. Settings and secrets --------------------------------------------------
# Installed once and never overwritten: they are yours to edit.

say "Settings"
for file in hottub.toml secrets.env; do
    if [ -e "$CONFIG_DIR/$file" ]; then
        note "$CONFIG_DIR/$file kept as it is."
    else
        install -o root -g "$SERVICE_USER" -m 640 "$file" "$CONFIG_DIR/$file"
        note "$CONFIG_DIR/$file created."
    fi
done
# Tighten these even if someone loosened them: the keys are in there.
chown root:"$SERVICE_USER" "$CONFIG_DIR/hottub.toml" "$CONFIG_DIR/secrets.env"
chmod 640 "$CONFIG_DIR/hottub.toml" "$CONFIG_DIR/secrets.env"

# --- 6. Switch to the new program and start it --------------------------------

say "Service"
install -o root -g root -m 644 hottub.service hottub-backup.service hottub-backup.timer \
    /etc/systemd/system/
systemctl daemon-reload
ln -sfn "$new_venv" "$PROGRAM_DIR/venv"
systemctl enable --quiet hottub.service hottub-backup.timer
systemctl restart hottub.service
systemctl start hottub-backup.timer
note "Started; waiting for it to answer ..."

answered=no
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "$SERVICE_URL" >/dev/null 2>&1; then
        answered=yes
        break
    fi
    sleep 1
done
if [ "$answered" = no ]; then
    journalctl -u hottub.service -n 30 --no-pager >&2 || true
    die "The service did not answer at $SERVICE_URL. Its last log lines are above.
       To go back to the previous version, see docs/operations.md."
fi
note "Answering: $(curl -fsS --max-time 2 "$SERVICE_URL")"

# Remove environments older than the one in use and the one before it.
mapfile -t old_venvs < <(find "$PROGRAM_DIR" -maxdepth 1 -type d -name 'venv-*' | sort | head -n "-$VENVS_KEPT")
for old in "${old_venvs[@]}"; do
    rm -rf "$old"
    note "Removed old $old"
done

# --- 7. nginx -----------------------------------------------------------------
# The program listens on this machine only (127.0.0.1:8100). nginx is what
# the home network reaches, and it passes /hot-tub/ on.
#
# hot-tub.conf is a "location", which only works inside a server block.
# On a Pi whose nginx nobody has set up yet, this script provides that
# block and removes Debian's placeholder site. On a Pi where nginx was
# already set up it does not touch the existing sites; it tells you the
# one line to add.

# True if nginx has only Debian's placeholder site, unedited: nobody has
# set it up yet, so this script may.
nginx_is_as_installed() {
    local enabled
    enabled="$(ls /etc/nginx/sites-enabled 2>/dev/null | tr '\n' ' ')"
    [ "$enabled" = "default " ] || return 1
    ! dpkg --verify nginx-common 2>/dev/null | grep -q 'sites-available/default'
}

if [ "$CONFIGURE_NGINX" = yes ]; then
    say "nginx"
    install -d -o root -g root -m 755 /etc/nginx/apps.d
    install -o root -g root -m 644 nginx/hot-tub.conf /etc/nginx/apps.d/hot-tub.conf
    if nginx_is_as_installed; then
        install -o root -g root -m 644 nginx/james-brown.conf /etc/nginx/sites-available/james-brown
        ln -sfn /etc/nginx/sites-available/james-brown /etc/nginx/sites-enabled/james-brown
        rm -f /etc/nginx/sites-enabled/default
        note "Set up a server on port 80 for this Pi."
    elif [ -e /etc/nginx/sites-available/james-brown ]; then
        install -o root -g root -m 644 nginx/james-brown.conf /etc/nginx/sites-available/james-brown
    fi
    if nginx -t >/dev/null 2>&1; then
        systemctl enable --quiet nginx
        systemctl reload-or-restart nginx
        if nginx -T 2>/dev/null | grep -q 'apps\.d/'; then
            if curl -fsS --max-time 5 "http://127.0.0.1${HEALTH_PATH}" >/dev/null 2>&1; then
                note "Answering through nginx."
            else
                warn "nginx is running but http://127.0.0.1${HEALTH_PATH} does not answer."
            fi
        else
            warn "nginx was already set up here, so its sites were left alone. Add this line
             inside the server block that should serve the hot tub, then run
             'sudo nginx -t && sudo systemctl reload nginx':
                 include /etc/nginx/apps.d/*.conf;"
        fi
    else
        nginx -t || true
        warn "nginx reports a configuration error (above); it was not reloaded."
    fi
fi

# --- 8. Things this script cannot fix but you should know ---------------------

say "Checks"
if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; then
    note "Clock: synchronised; time zone $(timedatectl show -p Timezone --value)."
else
    warn "The clock is not synchronised (timedatectl). Schedules depend on the time."
fi
if grep -Eq '^[[:space:]]*HOTTUB_PUSHOVER_TOKEN=.+' "$CONFIG_DIR/secrets.env"; then
    note "Pushover keys: present."
else
    note "Pushover keys: not entered yet, so alerts appear on the web page only."
    note "  To enter them: sudoedit $CONFIG_DIR/secrets.env (instructions inside)."
fi
note "Confinement: $(systemd-analyze security hottub.service 2>/dev/null | tail -n 1 | sed 's/^→ //')"

say "Done: release ${RELEASE_NAME} is running"
note "Pages:   http://$(hostname).local/hot-tub/"
note "Logs:    journalctl -u hottub -f"
note "Runbook: $PROGRAM_DIR/docs/operations.md"
if [ "${#WARNINGS[@]}" -gt 0 ]; then
    printf '\n%d warning(s) above need your attention.\n' "${#WARNINGS[@]}"
fi
